from types import SimpleNamespace

import cli as cli_module
import pytest


class _FakeClient:
    def __init__(self, result=1):
        self.result = result
        self.closed = False

    async def query(self, sql):
        assert sql == "SELECT 1"
        return SimpleNamespace(first_row=(self.result,))

    async def close(self):
        self.closed = True


class _FakeChannel:
    def __init__(self, fail=False):
        self.fail = fail
        self.closed = False
        self.declare_calls = []

    async def declare_queue(self, name, durable=True, passive=True):
        if self.fail:
            raise RuntimeError("queue missing")
        self.declare_calls.append(
            {
                "name": name,
                "durable": durable,
                "passive": passive,
            },
        )

    async def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, channel):
        self._channel = channel
        self.closed = False

    async def channel(self):
        return self._channel

    async def close(self):
        self.closed = True


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_check_queue_worker_dependencies_returns_healthy_status(
    monkeypatch,
    anyio_backend,
):
    client = _FakeClient(result=1)
    channel = _FakeChannel()
    connection = _FakeConnection(channel)
    client_kwargs = {}

    async def _fake_get_async_client(**kwargs):
        client_kwargs.update(kwargs)
        return client

    async def _fake_connect_rabbitmq(config):
        return connection

    monkeypatch.setattr(cli_module, "get_async_client", _fake_get_async_client)
    monkeypatch.setattr(cli_module, "connect_rabbitmq", _fake_connect_rabbitmq)

    status = await cli_module._check_queue_worker_dependencies()

    assert status == {
        "clickhouse": True,
        "rabbitmq": True,
    }
    assert client_kwargs["query_limit"] == 0
    assert (
        client_kwargs["connector_limit"] == cli_module.settings.performance.db_pool_size
    )
    assert (
        client_kwargs["connector_limit_per_host"]
        == cli_module.settings.performance.db_pool_size
    )
    assert "pool_mgr" not in client_kwargs
    assert channel.declare_calls == [
        {
            "name": "evnt.ingest",
            "durable": True,
            "passive": True,
        },
        {
            "name": "evnt.ingest.failed",
            "durable": True,
            "passive": True,
        },
    ]
    assert channel.closed is True
    assert connection.closed is True
    assert client.closed is True


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_check_queue_worker_dependencies_reports_unhealthy_status(
    monkeypatch,
    anyio_backend,
):
    async def _fail_clickhouse(**kwargs):
        raise RuntimeError("clickhouse down")

    async def _fail_rabbitmq(config):
        raise RuntimeError("rabbitmq down")

    monkeypatch.setattr(cli_module, "get_async_client", _fail_clickhouse)
    monkeypatch.setattr(cli_module, "connect_rabbitmq", _fail_rabbitmq)

    status = await cli_module._check_queue_worker_dependencies()

    assert status == {
        "clickhouse": False,
        "rabbitmq": False,
    }


def test_queue_healthcheck_exits_nonzero_when_dependencies_are_unhealthy(monkeypatch):
    async def _fake_check():
        return {
            "clickhouse": True,
            "rabbitmq": False,
        }

    monkeypatch.setattr(cli_module, "init_logging", lambda *args: None)
    monkeypatch.setattr(cli_module, "_check_queue_worker_dependencies", _fake_check)

    with pytest.raises(SystemExit) as exc_info:
        cli_module.QueueCommands().healthcheck()

    assert exc_info.value.code == 1
