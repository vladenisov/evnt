import asyncio

import pytest

from evnt.config import RabbitMQConfig
from evnt.ingest import rabbitmq as rabbitmq_module
from evnt.ingest.rabbitmq import QueuedInsertPayload, RabbitMQBatchWorker


class _FakeExchange:
    async def publish(self, message, routing_key, mandatory=True):
        return None


class _FakeChannel:
    def __init__(self):
        self.default_exchange = _FakeExchange()


class _FakeMessage:
    def __init__(self, payload: QueuedInsertPayload):
        self.body = payload.model_dump_json().encode("utf-8")
        self.acked = False

    async def ack(self):
        self.acked = True

    async def nack(self, requeue=True):
        raise AssertionError("nack should not be called")

    async def reject(self, requeue=False):
        raise AssertionError("reject should not be called")


class _Iterator:
    def __init__(self, message):
        self._message = message
        self._event = asyncio.Event()
        self.cancelled = False
        self._delivered = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def __anext__(self):
        try:
            await self._event.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

        if self._delivered:
            raise StopAsyncIteration

        self._delivered = True
        return self._message

    def release(self):
        self._event.set()


class _Queue:
    def __init__(self, iterator):
        self._iterator = iterator

    def iterator(self):
        return self._iterator


class _SequenceIterator:
    def __init__(self, messages):
        self._messages = list(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _Sink:
    def __init__(self):
        self.calls = []

    async def insert_batch(self, rows, table_group="evnt"):
        self.calls.append((table_group, rows))

    async def insert_rows(self, rows, table_group="evnt"):
        raise AssertionError("worker should prefer insert_batch")


@pytest.mark.anyio
async def test_run_does_not_cancel_queue_iterator_on_flush_timeout(anyio_backend):
    message = _FakeMessage(QueuedInsertPayload(rows=[{"id": 1}]))
    iterator = _Iterator(message)
    sink = _Sink()
    worker = RabbitMQBatchWorker(
        connection=object(),
        channel=_FakeChannel(),
        queue=_Queue(iterator),
        sink=sink,
        config=RabbitMQConfig(
            batch_size=10,
            batch_timeout_ms=10,
            retry_delay_ms=1,
        ),
    )

    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.05)
    iterator.release()
    with pytest.raises(
        RuntimeError,
        match="RabbitMQ queue iterator stopped unexpectedly",
    ):
        await task

    assert iterator.cancelled is False
    assert sink.calls == [("evnt", [{"id": 1}])]
    assert message.acked is True


@pytest.mark.anyio
async def test_run_applies_backoff_once_per_failure_level(
    monkeypatch,
):
    messages = [
        _FakeMessage(QueuedInsertPayload(rows=[{"id": 1}])),
        _FakeMessage(QueuedInsertPayload(rows=[{"id": 2}])),
    ]
    sink = _Sink()
    worker = RabbitMQBatchWorker(
        connection=object(),
        channel=_FakeChannel(),
        queue=_Queue(_SequenceIterator(messages)),
        sink=sink,
        config=RabbitMQConfig(
            batch_size=10,
            batch_timeout_ms=1000,
            retry_delay_ms=1000,
        ),
    )
    worker.failure_counts["evnt"] = 3

    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    async def heartbeat_loop():
        await asyncio.Event().wait()

    monkeypatch.setattr(rabbitmq_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(worker, "_heartbeat_loop", heartbeat_loop)

    with pytest.raises(
        RuntimeError,
        match="RabbitMQ queue iterator stopped unexpectedly",
    ):
        await worker.run()

    assert sleep_calls == [4.0]
    assert sink.calls == [("evnt", [{"id": 1}, {"id": 2}])]
    assert all(message.acked for message in messages)
    assert "evnt" not in worker.failure_counts


class _HangingAckMessage(_FakeMessage):
    def __init__(self, payload: QueuedInsertPayload):
        super().__init__(payload)
        self.ack_started = asyncio.Event()

    async def ack(self):
        self.ack_started.set()
        await asyncio.Event().wait()


class _IdleIterator(_SequenceIterator):
    async def __anext__(self):
        if not self._messages:
            await asyncio.Event().wait()
        return self._messages.pop(0)


async def _idle_heartbeat():
    await asyncio.Event().wait()


def _run_worker(messages, sink, monkeypatch, **config):
    worker = RabbitMQBatchWorker(
        connection=object(),
        channel=_FakeChannel(),
        queue=_Queue(_IdleIterator(messages)),
        sink=sink,
        config=RabbitMQConfig(retry_delay_ms=1, **config),
    )
    monkeypatch.setattr(worker, "_heartbeat_loop", _idle_heartbeat)
    return worker


@pytest.mark.anyio
async def test_run_keeps_step_clock_running_while_ack_hangs(anyio_backend, monkeypatch):
    message = _HangingAckMessage(QueuedInsertPayload(rows=[{"id": 1}]))
    sink = _Sink()
    worker = _run_worker([message], sink, monkeypatch, batch_size=1, batch_timeout_ms=10)

    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(message.ack_started.wait(), timeout=1)
    await asyncio.sleep(0.05)

    try:
        assert sink.calls == [("evnt", [{"id": 1}])]
        assert worker._busy_since is not None
        assert worker._busy_seconds() > 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_run_clears_step_clock_while_waiting_for_messages(anyio_backend, monkeypatch):
    message = _FakeMessage(QueuedInsertPayload(rows=[{"id": 1}]))
    sink = _Sink()
    worker = _run_worker([message], sink, monkeypatch, batch_size=1, batch_timeout_ms=1000)

    task = asyncio.create_task(worker.run())
    for _ in range(100):
        if message.acked:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.01)

    try:
        assert message.acked is True
        assert worker._busy_since is None
        assert worker._busy_seconds() == 0.0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_insert_attempt_restarts_step_clock_even_on_failure(anyio_backend, monkeypatch):
    class _FailingSink:
        async def insert_batch(self, rows, table_group="evnt"):
            raise RuntimeError("boom")

    worker = _run_worker([], _FailingSink(), monkeypatch)
    worker._busy_since = 1.0

    with pytest.raises(RuntimeError, match="boom"):
        await worker._insert_rows([{"id": 1}], "evnt")

    assert worker._busy_since > 1.0
