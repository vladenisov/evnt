"""``evnt queue worker`` driven end to end over fake backends.

The command, the batch worker and the ClickHouse connector are the real ones;
only the aio-pika connection and the ClickHouse client are faked. Shutdown is
triggered by a real ``SIGTERM`` sent to this process while the worker waits for
its next message.
"""

import asyncio
import os
import signal
from types import SimpleNamespace

import pytest

import evnt.cli as cli_module
from evnt.ingest import worker as worker_module
from evnt.ingest.rabbitmq import QueuedInsertPayload
from evnt.storage.clickhouse import client as clickhouse_client


class _FakeClickHouse:
    def __init__(self, events, *, close_error=None):
        self.events = events
        self.close_error = close_error
        self.inserts = []

    async def query(self, sql):
        return SimpleNamespace(first_row=(1,))

    async def insert(self, table, data, **kwargs):
        self.events.append("clickhouse.insert")
        self.inserts.append((table, data, kwargs))

    async def close(self):
        self.events.append("clickhouse.close")
        if self.close_error is not None:
            raise self.close_error


class _FakeMessage:
    def __init__(self, events, rows):
        self.events = events
        self.body = QueuedInsertPayload(rows=rows).model_dump_json().encode("utf-8")
        self.headers = {}
        self.acked = False
        self.nacked = False

    async def ack(self):
        self.events.append("message.ack")
        self.acked = True

    async def nack(self, requeue=True):
        self.events.append("message.nack")
        self.nacked = True


class _Iterator:
    """Delivers ``messages``, then runs ``when_drained`` once and waits like an idle queue."""

    def __init__(self, messages, when_drained):
        self._messages = list(messages)
        self._when_drained = when_drained

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        when_drained, self._when_drained = self._when_drained, None
        if when_drained is not None:
            when_drained()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _FakeQueue:
    def __init__(self, iterator):
        self._iterator = iterator

    def iterator(self):
        return self._iterator


class _FakeChannel:
    def __init__(self, events, queue, *, close_error=None):
        self.events = events
        self.queue = queue
        self.close_error = close_error
        self.prefetch_count = None
        self.declared = []

    async def set_qos(self, prefetch_count):
        self.prefetch_count = prefetch_count

    async def declare_queue(self, name, durable=True):
        self.declared.append(name)
        return self.queue

    async def close(self):
        self.events.append("channel.close")
        if self.close_error is not None:
            raise self.close_error


class _FakeConnection:
    def __init__(self, events, channel):
        self.events = events
        self._channel = channel
        self.channel_options = None

    async def channel(self, publisher_confirms=True, on_return_raises=False):
        self.channel_options = {
            "publisher_confirms": publisher_confirms,
            "on_return_raises": on_return_raises,
        }
        return self._channel

    async def close(self):
        self.events.append("connection.close")


def _send_sigterm():
    os.kill(os.getpid(), signal.SIGTERM)


def _stop_iteration():
    raise StopAsyncIteration


class _Harness:
    def __init__(self, monkeypatch, tmp_path, *, when_drained, **errors):
        self.events = []
        self.message = _FakeMessage(self.events, [{"aid": "shutdown-flush"}])
        self.clickhouse = _FakeClickHouse(
            self.events, close_error=errors.get("clickhouse_close_error")
        )
        self.channel = _FakeChannel(
            self.events,
            _FakeQueue(_Iterator([self.message], when_drained)),
            close_error=errors.get("channel_close_error"),
        )
        self.connection = connection = _FakeConnection(self.events, self.channel)

        async def _get_async_client(**kwargs):
            return self.clickhouse

        async def _connect_rabbitmq(config):
            return connection

        monkeypatch.setattr(cli_module, "init_logging", lambda *args: None)
        monkeypatch.setattr(clickhouse_client, "get_async_client", _get_async_client)
        monkeypatch.setattr(worker_module, "connect_rabbitmq", _connect_rabbitmq)
        monkeypatch.setattr(worker_module, "WORKER_LIVENESS_PATH", tmp_path / "worker.alive")


@pytest.fixture
def restore_sigterm():
    """A handler leaking out of a failed test must not turn SIGTERM into a no-op."""
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def test_queue_worker_flushes_and_closes_in_order_on_sigterm(
    monkeypatch, tmp_path, restore_sigterm
):
    harness = _Harness(monkeypatch, tmp_path, when_drained=_send_sigterm)

    result = cli_module.QueueCommands().worker()

    assert result == "RabbitMQ worker stopped"
    # The batch in hand is inserted and acked before either connection closes,
    # and ClickHouse outlives RabbitMQ so the final flush has somewhere to go.
    assert harness.events == [
        "clickhouse.insert",
        "message.ack",
        "channel.close",
        "connection.close",
        "clickhouse.close",
    ]
    assert harness.message.nacked is False
    # A copy for the failed queue the broker cannot route must raise, not pass.
    assert harness.connection.channel_options == {
        "publisher_confirms": True,
        "on_return_raises": True,
    }
    queue_conf = cli_module.settings.ingest.rabbitmq
    assert harness.channel.prefetch_count == queue_conf.prefetch_count
    assert queue_conf.queue_name in harness.channel.declared
    assert queue_conf.resolved_failed_queue_name in harness.channel.declared
    assert (tmp_path / "worker.alive").exists()

    table, data, kwargs = harness.clickhouse.inserts[0]
    assert table.startswith(f"{cli_module.settings.clickhouse.configuration.database}.")
    assert len(data) == 1
    assert "shutdown-flush" in data[0]
    # The worker acks only what ClickHouse has written, never what it buffered.
    insert_settings = kwargs["settings"]
    assert insert_settings.get("async_insert") == 0 or insert_settings["wait_for_async_insert"] == 1


def test_queue_worker_closes_clickhouse_when_worker_close_fails_on_sigterm(
    monkeypatch, tmp_path, restore_sigterm
):
    harness = _Harness(
        monkeypatch,
        tmp_path,
        when_drained=_send_sigterm,
        channel_close_error=ConnectionError("channel close failed"),
    )

    with pytest.raises(ConnectionError, match="channel close failed"):
        cli_module.QueueCommands().worker()

    assert harness.message.acked is True
    assert harness.events[-2:] == ["channel.close", "clickhouse.close"]


def test_queue_worker_reports_clickhouse_close_failure_after_closing_rabbitmq(
    monkeypatch, tmp_path, restore_sigterm
):
    harness = _Harness(
        monkeypatch,
        tmp_path,
        when_drained=_send_sigterm,
        clickhouse_close_error=OSError("clickhouse close failed"),
    )

    with pytest.raises(OSError, match="clickhouse close failed"):
        cli_module.QueueCommands().worker()

    assert harness.events[-3:] == ["channel.close", "connection.close", "clickhouse.close"]


def test_queue_worker_keeps_the_run_error_when_closing_fails_too(
    monkeypatch, tmp_path, restore_sigterm
):
    harness = _Harness(
        monkeypatch,
        tmp_path,
        when_drained=_stop_iteration,
        channel_close_error=ConnectionError("channel close failed"),
        clickhouse_close_error=OSError("clickhouse close failed"),
    )

    with pytest.raises(RuntimeError, match="RabbitMQ queue iterator stopped unexpectedly"):
        cli_module.QueueCommands().worker()

    # The worker still flushed what it held, and both closes were attempted.
    assert harness.events == [
        "clickhouse.insert",
        "message.ack",
        "channel.close",
        "clickhouse.close",
    ]


def test_queue_worker_closes_clickhouse_when_rabbitmq_setup_fails(
    monkeypatch, tmp_path, restore_sigterm
):
    harness = _Harness(monkeypatch, tmp_path, when_drained=_send_sigterm)

    async def _fail_create(sink, config):
        raise PermissionError("rabbitmq refused the credentials")

    monkeypatch.setattr(worker_module.RabbitMQBatchWorker, "create", _fail_create)

    with pytest.raises(PermissionError, match="refused the credentials"):
        cli_module.QueueCommands().worker()

    assert harness.events == ["clickhouse.close"]
