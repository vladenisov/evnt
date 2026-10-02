import pytest
from clickhouse_connect.driver.exceptions import DataError

from evnt.config import RabbitMQConfig
from evnt.ingest import rabbitmq as rabbitmq_module
from evnt.ingest.rabbitmq import QueuedInsertPayload, RabbitMQBatchWorker


def test_failed_queue_name_must_differ_from_main_queue():
    with pytest.raises(ValueError, match="failed_queue_name must differ"):
        RabbitMQConfig(
            queue_name="evnt.ingest",
            failed_queue_name="evnt.ingest",
        )


def test_default_prefetch_can_fill_default_batch():
    config = RabbitMQConfig()

    assert config.prefetch_count >= config.batch_size


class _FakeExchange:
    def __init__(self):
        self.published = []

    async def publish(self, message, routing_key, mandatory=True):
        self.published.append(
            {
                "message": message,
                "routing_key": routing_key,
                "mandatory": mandatory,
            },
        )


class _FakeChannel:
    def __init__(self):
        self.default_exchange = _FakeExchange()


class _FakeMessage:
    def __init__(self, payload: QueuedInsertPayload):
        self.body = payload.model_dump_json().encode("utf-8")
        self.acked = False
        self.nacked = False
        self.rejected = False

    async def ack(self):
        self.acked = True

    async def nack(self, requeue=True):
        self.nacked = requeue

    async def reject(self, requeue=False):
        self.rejected = not requeue


class _BatchSink:
    def __init__(self, fail=False):
        self.fail = fail
        self.batch_calls = []

    async def insert_batch(self, rows, table_group="evnt"):
        if self.fail:
            raise RuntimeError("boom")
        self.batch_calls.append((table_group, rows))

    async def insert_rows(self, rows, table_group="evnt"):
        raise AssertionError("worker should prefer insert_batch when available")


class _SelectiveBatchSink(_BatchSink):
    def __init__(self, bad_ids):
        super().__init__()
        self.bad_ids = set(bad_ids)

    async def insert_batch(self, rows, table_group="evnt"):
        if any(row.get("id") in self.bad_ids for row in rows):
            raise DataError("invalid row")
        self.batch_calls.append((table_group, rows))


def _worker(sink, **overrides):
    config_data = {
        "batch_size": 3,
        "batch_timeout_ms": 1000,
        "retry_delay_ms": 1,
    }
    config_data.update(overrides)
    config = RabbitMQConfig(**config_data)
    return RabbitMQBatchWorker(
        connection=object(),
        channel=_FakeChannel(),
        queue=object(),
        sink=sink,
        config=config,
    )


@pytest.mark.anyio
async def test_worker_batches_messages_before_insert(anyio_backend):
    sink = _BatchSink()
    worker = _worker(sink)
    messages = [
        _FakeMessage(QueuedInsertPayload(rows=[{"id": 1}])),
        _FakeMessage(QueuedInsertPayload(rows=[{"id": 2}])),
        _FakeMessage(QueuedInsertPayload(rows=[{"id": 3}])),
    ]

    for message in messages:
        await worker.add_message(message)

    assert sink.batch_calls == [
        ("evnt", [{"id": 1}, {"id": 2}, {"id": 3}]),
    ]
    assert all(message.acked for message in messages)
    assert not any(message.nacked for message in messages)


@pytest.mark.anyio
async def test_worker_requeues_messages_when_batch_insert_fails(anyio_backend):
    sink = _BatchSink(fail=True)
    worker = _worker(sink, batch_size=10)
    messages = [
        _FakeMessage(QueuedInsertPayload(rows=[{"id": 1}])),
        _FakeMessage(QueuedInsertPayload(rows=[{"id": 2}])),
    ]

    for message in messages:
        await worker.add_message(message)

    await worker.flush_all()

    assert sink.batch_calls == []
    assert all(message.nacked for message in messages)
    assert not any(message.acked for message in messages)


@pytest.mark.anyio
async def test_worker_moves_only_failed_message_to_failed_queue_for_clickhouse_data_error(
    anyio_backend,
):
    sink = _SelectiveBatchSink(bad_ids={2})
    worker = _worker(sink, batch_size=10)
    messages = [
        _FakeMessage(QueuedInsertPayload(rows=[{"id": 1}])),
        _FakeMessage(QueuedInsertPayload(rows=[{"id": 2}])),
        _FakeMessage(QueuedInsertPayload(rows=[{"id": 3}])),
    ]

    for message in messages:
        await worker.add_message(message)

    await worker.flush_all()

    assert sink.batch_calls == [
        ("evnt", [{"id": 1}]),
        ("evnt", [{"id": 3}]),
    ]
    assert messages[0].acked is True
    assert messages[1].acked is True
    assert messages[2].acked is True
    assert messages[1].rejected is False
    assert messages[1].nacked is False
    assert worker.channel.default_exchange.published[0]["routing_key"] == "evnt.ingest.failed"
    assert worker.channel.default_exchange.published[0]["message"].body == messages[1].body


@pytest.mark.anyio
async def test_retry_rabbitmq_startup_waits_until_connection_is_ready(
    monkeypatch,
):
    attempts = 0
    sleep_calls = []

    async def _fake_connect(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("rabbitmq is starting")
        return "connected"

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(rabbitmq_module, "connect_robust", _fake_connect)
    monkeypatch.setattr(rabbitmq_module.asyncio, "sleep", _fake_sleep)

    connection = await rabbitmq_module.retry_rabbitmq_startup(
        RabbitMQConfig(
            connect_timeout_seconds=1,
            startup_timeout_seconds=10,
            startup_retry_interval_ms=250,
        ),
        "connect",
        lambda: rabbitmq_module.connect_rabbitmq(
            RabbitMQConfig(
                connect_timeout_seconds=1,
                startup_timeout_seconds=10,
                startup_retry_interval_ms=250,
            ),
        ),
    )

    assert connection == "connected"
    assert attempts == 3
    assert sleep_calls == [0.25, 0.25]
