import importlib
from pathlib import Path
from types import SimpleNamespace

import core.lifespan as lifespan_module
import pytest
from core.config import ClickHouseConfig, ProxyConfig
from routers.tracker.parsers.iglu import ValidationResult

READY_AFTER_ATTEMPTS = 3


class _FakeClickHouseClient:
    def __init__(self, *, fail: bool):
        self.fail = fail
        self.closed = False

    async def query(self, sql: str):
        assert sql == "SELECT 1"
        if self.fail:
            raise RuntimeError("clickhouse is starting")
        return SimpleNamespace(first_row=(1,))

    async def close(self):
        self.closed = True


class _RecordingLogger:
    def __init__(self):
        self.infos = []
        self.warnings = []
        self.errors = []

    def info(self, *args, **kwargs):
        self.infos.append((args, kwargs))

    def warning(self, *args, **kwargs):
        self.warnings.append((args, kwargs))

    def error(self, *args, **kwargs):
        self.errors.append((args, kwargs))


class _FakeProxyClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeHealthChecker:
    async def check(self):
        return {"backend": True}


def test_cache_health_checker_uses_performance_config(monkeypatch):
    checker = _FakeHealthChecker()
    monkeypatch.setattr(
        lifespan_module,
        "PERFORMANCE_CONFIG",
        SimpleNamespace(healthcheck_cache_ttl_seconds=7.5),
    )

    cached = lifespan_module._cache_health_checker(checker)

    assert isinstance(cached, lifespan_module.CachedHealthChecker)
    assert cached.checker is checker
    assert cached.ttl_seconds == 7.5


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_retry_clickhouse_startup_waits_until_connection_is_ready(
    monkeypatch,
    anyio_backend,
):
    attempts = 0
    sleep_calls = []

    async def _fake_connect():
        nonlocal attempts
        attempts += 1
        if attempts < READY_AFTER_ATTEMPTS:
            raise OSError("clickhouse is starting")
        return "connected"

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(lifespan_module.asyncio, "sleep", _fake_sleep)

    connection = await lifespan_module.retry_clickhouse_startup(
        ClickHouseConfig(
            startup_timeout_seconds=10,
            startup_retry_interval_ms=250,
        ),
        "connect",
        _fake_connect,
    )

    assert connection == "connected"
    assert attempts == READY_AFTER_ATTEMPTS
    assert sleep_calls == [0.25, 0.25]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_create_ready_clickhouse_client_closes_client_when_probe_fails(
    monkeypatch,
    anyio_backend,
):
    client = _FakeClickHouseClient(fail=True)

    async def _fake_create_clickhouse_client():
        return client

    monkeypatch.setattr(
        lifespan_module,
        "_create_clickhouse_client",
        _fake_create_clickhouse_client,
    )

    with pytest.raises(RuntimeError, match="clickhouse is starting"):
        await lifespan_module._create_ready_clickhouse_client()

    assert client.closed is True


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_configure_proxy_http_client_reuses_lifespan_resources(
    monkeypatch,
    anyio_backend,
):
    created_clients = []

    def _fake_async_client(**kwargs):
        client = _FakeProxyClient(**kwargs)
        created_clients.append(client)
        return client

    monkeypatch.setattr(lifespan_module.httpx, "AsyncClient", _fake_async_client)
    application = SimpleNamespace(state=SimpleNamespace(_closeables=[]))

    await lifespan_module._configure_proxy_http_client(
        application,
        ProxyConfig(domains=["Example.com."]),
    )

    assert len(created_clients) == 1
    assert application.state.proxy_http_client is created_clients[0]
    assert application.state.proxy_allowed_hosts == frozenset({"example.com"})
    assert application.state._closeables == [created_clients[0]]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_lifespan_warms_iglu_schema_cache(monkeypatch, anyio_backend):
    logger = _RecordingLogger()
    warm_calls = []

    async def _fake_configure_direct_ingest(application):
        application.state.connector = object()

    def _fake_warm_iglu_schema_cache():
        warm_calls.append(True)
        return {
            "iglu:com.acme/example/jsonschema/1-0-0": ValidationResult(
                status="ok",
                schema_path=Path("/tmp/example"),
            ),
            "iglu:com.acme/missing/jsonschema/1-0-0": ValidationResult(
                status="warning",
                schema_path=Path("/tmp/missing"),
                error="schema file not found",
            ),
        }

    monkeypatch.setattr(lifespan_module, "logger", logger)
    monkeypatch.setattr(
        lifespan_module,
        "_configure_direct_ingest",
        _fake_configure_direct_ingest,
    )
    monkeypatch.setattr(
        lifespan_module,
        "warm_iglu_schema_cache",
        _fake_warm_iglu_schema_cache,
    )
    monkeypatch.setattr(
        lifespan_module,
        "INGEST_CONFIG",
        SimpleNamespace(
            mode="direct",
            rabbitmq=SimpleNamespace(host="rabbitmq"),
        ),
    )
    monkeypatch.setattr(
        lifespan_module,
        "CLICKHOUSE_CONFIG",
        ClickHouseConfig(),
    )

    application = SimpleNamespace(state=SimpleNamespace())

    async with lifespan_module.lifespan(application):
        pass

    assert warm_calls == [True]
    assert any(
        args[0] == "Failed to warm Iglu schema cache"
        and kwargs["schema"] == "iglu:com.acme/missing/jsonschema/1-0-0"
        for args, kwargs in logger.warnings
    )
    assert any(
        args[0] == "Iglu schema cache warmed"
        and kwargs["loaded_count"] == 1
        and kwargs["warning_count"] == 1
        and kwargs["skipped_count"] == 0
        for args, kwargs in logger.infos
    )


class _FakeRabbitMQConnector:
    """Stand-in for RabbitMQPublisher returned by ``create``."""

    def __init__(self):
        self.channel = SimpleNamespace(name="fake-channel")
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeRabbitMQHealthChecker:
    def __init__(self, channel, *queue_names):
        self.channel = channel
        self.queue_names = queue_names

    async def check(self):
        return {"rabbitmq": True}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_lifespan_configures_rabbitmq_ingest(monkeypatch, anyio_backend):
    # The RabbitMQ publisher/health-checker are imported lazily inside
    # ``_configure_rabbitmq_ingest`` from the ``ingest`` package, so patch them
    # on that module to avoid touching a real broker.
    ingest_module = importlib.import_module("ingest")

    connector = _FakeRabbitMQConnector()
    create_calls = []

    async def _fake_create(*, config, tables, database, cluster_name):
        create_calls.append(
            {
                "config": config,
                "tables": tables,
                "database": database,
                "cluster_name": cluster_name,
            },
        )
        return connector

    monkeypatch.setattr(
        ingest_module.RabbitMQPublisher,
        "create",
        classmethod(lambda cls, **kwargs: _fake_create(**kwargs)),
    )
    monkeypatch.setattr(
        ingest_module,
        "RabbitMQHealthChecker",
        _FakeRabbitMQHealthChecker,
    )

    async def _no_op_configure_proxy_http_client(application):
        application.state.proxy_http_client = object()

    monkeypatch.setattr(
        lifespan_module,
        "_configure_proxy_http_client",
        _no_op_configure_proxy_http_client,
    )
    monkeypatch.setattr(
        lifespan_module,
        "warm_known_iglu_schemas",
        lambda: None,
    )

    rabbitmq_config = SimpleNamespace(
        host="rabbitmq-host",
        queue_name="evnt.ingest",
        resolved_failed_queue_name="evnt.ingest.failed",
    )
    clickhouse_config = ClickHouseConfig()

    monkeypatch.setattr(
        lifespan_module,
        "INGEST_CONFIG",
        SimpleNamespace(mode="rabbitmq", rabbitmq=rabbitmq_config),
    )
    monkeypatch.setattr(
        lifespan_module,
        "CLICKHOUSE_CONFIG",
        clickhouse_config,
    )

    application = SimpleNamespace(state=SimpleNamespace())

    async with lifespan_module.lifespan(application):
        # While the app is running, the RabbitMQ connector and health checker
        # must be wired onto application state and registered as closeable.
        assert application.state.ingest_mode == "rabbitmq"
        assert application.state.ch_client is None
        assert application.state.connector is connector
        assert isinstance(
            application.state.health_checker,
            lifespan_module.CachedHealthChecker,
        )
        assert isinstance(
            application.state.health_checker.checker,
            _FakeRabbitMQHealthChecker,
        )
        assert connector in application.state._closeables
        assert connector.closed is False

    # Publisher.create received the configured tables/database/cluster.
    assert len(create_calls) == 1
    call = create_calls[0]
    assert call["config"] is rabbitmq_config
    assert call["tables"] == clickhouse_config.configuration.tables
    assert call["database"] == clickhouse_config.configuration.database
    assert call["cluster_name"] == clickhouse_config.configuration.cluster_name

    # The health checker was built from the connector's channel + queue names.
    assert application.state.health_checker.checker.channel is connector.channel
    assert application.state.health_checker.checker.queue_names == (
        rabbitmq_config.queue_name,
        rabbitmq_config.resolved_failed_queue_name,
    )

    # On shutdown the connector must be closed.
    assert connector.closed is True
