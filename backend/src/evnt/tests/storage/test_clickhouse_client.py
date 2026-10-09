from types import SimpleNamespace

import pytest

from evnt.config import ClickHouseConfig, DirectInsertConfig
from evnt.storage.clickhouse import client as clickhouse


class _FakeClient:
    def __init__(self, *, ready: bool = True, error: Exception | None = None):
        self.ready = ready
        self.error = error
        self.closed = False

    async def query(self, sql: str):
        assert sql == clickhouse.READINESS_QUERY
        if self.error is not None:
            raise self.error
        return SimpleNamespace(first_row=(1 if self.ready else 0,))

    async def close(self):
        self.closed = True


@pytest.mark.parametrize(
    ("config", "require_wait", "expected"),
    [
        (DirectInsertConfig(async_insert=False), True, {"async_insert": 0}),
        (
            DirectInsertConfig(async_insert=True, wait_for_async_insert=False),
            False,
            {"async_insert": 1, "wait_for_async_insert": 0},
        ),
        (
            DirectInsertConfig(async_insert=True, wait_for_async_insert=False),
            True,
            {"async_insert": 1, "wait_for_async_insert": 1},
        ),
    ],
)
def test_insert_settings(config, require_wait, expected):
    assert clickhouse.insert_settings(config, require_wait=require_wait) == expected


@pytest.mark.anyio
@pytest.mark.parametrize("pool_size", [7, 32])
async def test_create_client_sizes_the_connection_pool(monkeypatch, pool_size):
    seen = {}

    async def fake_get_async_client(**kwargs):
        seen.update(kwargs)
        return _FakeClient()

    monkeypatch.setattr(clickhouse, "get_async_client", fake_get_async_client)
    config = ClickHouseConfig()

    await clickhouse.create_client(config, pool_size=pool_size)

    assert seen["connector_limit"] == pool_size
    assert seen["connector_limit_per_host"] == pool_size
    assert "pool_mgr" not in seen
    assert seen["query_limit"] == 0
    assert seen["host"] == config.connection.host
    # The secret is unwrapped for the driver, never passed as a SecretStr.
    assert seen["password"] == config.connection.password.get_secret_value()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fake",
    [_FakeClient(ready=False), _FakeClient(error=ConnectionError("refused"))],
    ids=["unexpected-result", "query-error"],
)
async def test_create_ready_client_closes_the_client_when_the_probe_fails(monkeypatch, fake):
    async def fake_create_client(config, pool_size):
        return fake

    monkeypatch.setattr(clickhouse, "create_client", fake_create_client)

    with pytest.raises((RuntimeError, ConnectionError)):
        await clickhouse.create_ready_client(ClickHouseConfig(), pool_size=1)
    assert fake.closed


@pytest.mark.anyio
async def test_retry_startup_retries_until_the_callback_succeeds(monkeypatch):
    attempts = 0
    sleeps: list[float] = []

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("not yet")
        return "client"

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(clickhouse.asyncio, "sleep", fake_sleep)
    config = ClickHouseConfig(startup_timeout_seconds=60, startup_retry_interval_ms=250)

    assert await clickhouse.retry_startup(config, "test", flaky) == "client"
    assert attempts == 3
    assert sleeps == [0.25, 0.25]


@pytest.mark.anyio
async def test_retry_startup_reraises_once_the_deadline_passes(monkeypatch):
    clock = iter(range(0, 1000, 10))

    class _Loop:
        def time(self):
            return next(clock)

    async def always_fails():
        raise ConnectionError("down")

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(clickhouse.asyncio, "get_running_loop", lambda: _Loop())
    monkeypatch.setattr(clickhouse.asyncio, "sleep", fake_sleep)
    config = ClickHouseConfig(startup_timeout_seconds=25, startup_retry_interval_ms=1000)

    with pytest.raises(ConnectionError, match="down"):
        await clickhouse.retry_startup(config, "test", always_fails)
