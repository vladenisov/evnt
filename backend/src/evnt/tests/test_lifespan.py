from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

import evnt.ingest
import evnt.lifespan as lifespan_module
import evnt.storage.clickhouse
from evnt.exceptions import DatabaseConnectionError
from evnt.tracker.iglu import ValidationResult


class _Closeable:
    def __init__(self, *, fail_close: bool = False):
        self.closed = False
        self.fail_close = fail_close
        self.channel = SimpleNamespace(name="channel")

    async def close(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close failed")


class _RecordingLogger:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def __getattr__(self, level):
        return lambda event, **kw: self.calls.append((level, event, kw))


@pytest.fixture
def no_iglu(monkeypatch):
    monkeypatch.setattr(lifespan_module, "warm_known_iglu_schemas", lambda: None)


@pytest.fixture
def direct_backend(monkeypatch):
    """Patch the ClickHouse connection so direct mode starts without a server."""
    client = _Closeable()
    connects = []

    async def fake_connect(config, pool_size, operation):
        connects.append((pool_size, operation))
        return client

    monkeypatch.setattr(lifespan_module.clickhouse, "connect", fake_connect)
    monkeypatch.setattr(lifespan_module.settings.ingest, "mode", "direct")
    return SimpleNamespace(client=client, connects=connects)


@pytest.mark.anyio
async def test_direct_mode_wires_connector_and_closes_on_shutdown(direct_backend, no_iglu):
    app = FastAPI()

    async with lifespan_module.lifespan(app):
        assert isinstance(app.state.connector, evnt.storage.clickhouse.ClickHouseConnector)
        assert app.state.ingest_mode == "direct"
        assert app.state.keyring is None
        assert direct_backend.connects == [
            (lifespan_module.settings.performance.db_pool_size, "direct_ingest_create"),
        ]
        proxy_client = app.state.proxy_http_client
        # Redirects would let an allowed host bounce the proxy to an internal target.
        assert proxy_client.follow_redirects is False

    assert direct_backend.client.closed
    assert proxy_client.is_closed


@pytest.mark.anyio
async def test_health_checker_caches_for_the_configured_ttl(direct_backend, no_iglu, monkeypatch):
    monkeypatch.setattr(lifespan_module.settings.performance, "healthcheck_cache_ttl_seconds", 7.5)
    app = FastAPI()

    async with lifespan_module.lifespan(app):
        assert app.state.health_checker.ttl_seconds == 7.5


@pytest.mark.anyio
async def test_rabbitmq_mode_uses_the_publisher(monkeypatch, no_iglu):
    publisher = _Closeable()
    created = {}

    async def fake_create(**kwargs):
        created.update(kwargs)
        return publisher

    class _FakeHealthChecker:
        def __init__(self, channel, *queues):
            self.channel = channel
            self.queues = queues

        async def check(self):
            return {"rabbitmq": True}

    monkeypatch.setattr(evnt.ingest.RabbitMQPublisher, "create", staticmethod(fake_create))
    monkeypatch.setattr(evnt.ingest, "RabbitMQHealthChecker", _FakeHealthChecker)
    monkeypatch.setattr(lifespan_module.settings.ingest, "mode", "rabbitmq")
    app = FastAPI()

    async with lifespan_module.lifespan(app):
        assert app.state.connector is publisher
        assert app.state.ingest_mode == "rabbitmq"
        assert created["config"] is lifespan_module.settings.ingest.rabbitmq
        assert app.state.health_checker.checker.channel is publisher.channel

    assert publisher.closed


@pytest.mark.anyio
async def test_startup_failure_closes_what_was_opened_and_raises(direct_backend, monkeypatch):
    def fail_warm():
        raise OSError("disk gone")

    monkeypatch.setattr(lifespan_module, "warm_known_iglu_schemas", fail_warm)
    app = FastAPI()

    with pytest.raises(DatabaseConnectionError) as excinfo:
        async with lifespan_module.lifespan(app):
            pytest.fail("startup should not have completed")

    assert excinfo.value.details["mode"] == "direct"
    assert direct_backend.client.closed
    assert app.state.proxy_http_client.is_closed


@pytest.mark.anyio
async def test_a_failing_close_does_not_stop_the_others():
    first, failing = _Closeable(), _Closeable(fail_close=True)

    async with AsyncExitStack() as stack:
        lifespan_module._close_on_exit(stack, "first", first.close)
        lifespan_module._close_on_exit(stack, "failing", failing.close)

    assert failing.closed
    assert first.closed


def test_warm_known_iglu_schemas_logs_a_summary(monkeypatch):
    logger = _RecordingLogger()
    results = {
        "iglu:a/b/jsonschema/1-0-0": ValidationResult(status="ok"),
        "iglu:a/c/jsonschema/1-0-0": ValidationResult(
            status="warning",
            schema_path=Path("/schemas/a/c"),
            error="bad schema",
        ),
        "iglu:a/d/jsonschema/1-0-0": ValidationResult(status="skipped"),
    }
    monkeypatch.setattr(lifespan_module, "logger", logger)
    monkeypatch.setattr(lifespan_module, "warm_iglu_schema_cache", lambda: results)

    lifespan_module.warm_known_iglu_schemas()

    warnings = [call for call in logger.calls if call[0] == "warning"]
    assert len(warnings) == 1
    assert warnings[0][2]["schema"] == "iglu:a/c/jsonschema/1-0-0"
    summary = next(call for call in logger.calls if call[1] == "Iglu schema cache warmed")
    assert summary[2] == {"loaded_count": 1, "warning_count": 1, "skipped_count": 1}
