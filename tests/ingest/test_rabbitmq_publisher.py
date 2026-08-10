"""Behavioral tests for ``RabbitMQPublisher`` and worker backoff/flush paths.

All RabbitMQ interaction is faked: a recording exchange/channel captures
published messages and a fake message records ack/nack/reject so no broker or
network is required. These cover the publisher's enqueue contract and the
worker's per-table-group backoff, table-name resolution, and failed-queue
publish-failure fallback that ``test_rabbitmq.py`` does not.
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from clickhouse_connect.driver.exceptions import DataError
from core.config import RabbitMQConfig
from ingest.rabbitmq import (
    QueuedInsertPayload,
    RabbitMQBatchWorker,
    RabbitMQPublisher,
)


class _RecordingExchange:
    def __init__(self, fail=False):
        self.fail = fail
        self.published = []

    async def publish(self, message, routing_key, mandatory=True):
        if self.fail:
            raise RuntimeError("exchange down")
        self.published.append(
            {"message": message, "routing_key": routing_key, "mandatory": mandatory},
        )


class _RecordingChannel:
    def __init__(self, exchange=None):
        self.default_exchange = exchange or _RecordingExchange()
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeMessage:
    def __init__(self, payload: QueuedInsertPayload, *, headers=None):
        self.body = payload.model_dump_json().encode("utf-8")
        self.headers = headers or {}
        self.content_type = "application/json"
        self.type = "evnt.insert"
        self.acked = False
        self.nacked = False
        self.rejected = False

    async def ack(self):
        self.acked = True

    async def nack(self, requeue=True):
        self.nacked = requeue

    async def reject(self, requeue=False):
        self.rejected = not requeue


def _publisher(channel=None, *, cluster_name=None):
    return RabbitMQPublisher(
        connection=_FakeConnection(),
        channel=channel or _RecordingChannel(),
        config=RabbitMQConfig(queue_name="evnt.ingest"),
        tables={
            "evnt": {
                "local": {"name": "events_local"},
                "distributed": {"name": "events_distributed"},
            },
        },
        database="evnt",
        cluster_name=cluster_name,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_publisher_insert_rows_publishes_persistent_message(anyio_backend):
    channel = _RecordingChannel()
    publisher = _publisher(channel)

    await publisher.insert_rows([{"aid": "a"}, {"aid": "b"}], table_group="evnt")

    assert len(channel.default_exchange.published) == 1
    published = channel.default_exchange.published[0]
    assert published["routing_key"] == "evnt.ingest"
    assert published["mandatory"] is True

    decoded = QueuedInsertPayload.model_validate_json(published["message"].body)
    assert decoded.table_group == "evnt"
    assert decoded.rows == [{"aid": "a"}, {"aid": "b"}]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_publisher_serializes_typed_values_without_intermediate_copy(
    anyio_backend,
):
    channel = _RecordingChannel()
    publisher = _publisher(channel)
    event_id = UUID("5a402e39-1997-4842-a9b0-0b59287052b6")
    event_time = datetime(2026, 8, 10, 13, 27, 56, tzinfo=UTC)

    await publisher.insert_rows(
        [{"event_id": event_id, "time": event_time}],
        table_group="evnt",
    )

    published = channel.default_exchange.published[0]["message"]
    decoded = QueuedInsertPayload.model_validate_json(published.body)
    assert decoded.rows == [
        {
            "event_id": str(event_id),
            "time": "2026-08-10T13:27:56Z",
        },
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_publisher_insert_rows_noop_for_empty_rows(anyio_backend):
    channel = _RecordingChannel()
    publisher = _publisher(channel)

    await publisher.insert_rows([], table_group="evnt")

    assert channel.default_exchange.published == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_publisher_get_table_name_local_and_distributed(anyio_backend):
    local_pub = _publisher(cluster_name=None)
    cluster_pub = _publisher(cluster_name="prod")

    assert await local_pub.get_table_name() == "events_local"
    assert await cluster_pub.get_table_name() == "events_distributed"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_publisher_close_closes_channel_and_connection(anyio_backend):
    channel = _RecordingChannel()
    publisher = _publisher(channel)

    await publisher.close()

    assert channel.closed is True
    assert publisher.connection.closed is True


# ---------------------------------------------------------------------------
# Worker backoff / failed-queue fallback paths
# ---------------------------------------------------------------------------


class _BatchSink:
    def __init__(self, fail=False):
        self.fail = fail
        self.batch_calls = []

    async def insert_batch(self, rows, table_group="evnt"):
        if self.fail:
            raise RuntimeError("boom")
        self.batch_calls.append((table_group, rows))


class _InsertRowsOnlySink:
    """Sink lacking ``insert_batch`` so the worker falls back to insert_rows."""

    def __init__(self):
        self.rows_calls = []

    async def insert_rows(self, rows, table_group="evnt"):
        self.rows_calls.append((table_group, rows))


class _HangingSink:
    async def insert_batch(self, rows, table_group="evnt"):
        await asyncio.Event().wait()


def _worker(sink, channel=None, **overrides):
    config_data = {"batch_size": 10, "batch_timeout_ms": 1000, "retry_delay_ms": 100}
    config_data.update(overrides)
    return RabbitMQBatchWorker(
        connection=_FakeConnection(),
        channel=channel or _RecordingChannel(),
        queue=object(),
        sink=sink,
        config=RabbitMQConfig(**config_data),
    )


def test_backoff_seconds_grows_exponentially_and_caps():
    worker = _worker(_BatchSink(), retry_delay_ms=1000)  # retry_delay = 1.0s

    assert worker._backoff_seconds() == 0.0

    worker.failure_counts["evnt"] = 1
    assert worker._backoff_seconds() == 1.0  # 1.0 * 2**0

    worker.failure_counts["evnt"] = 3
    assert worker._backoff_seconds() == 4.0  # 1.0 * 2**2

    worker.failure_counts["evnt"] = 100
    assert worker._backoff_seconds() == RabbitMQBatchWorker.MAX_BACKOFF_SECONDS


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_flush_failure_increments_failure_count_then_resets(anyio_backend):
    sink = _BatchSink(fail=True)
    worker = _worker(sink)
    msg = _FakeMessage(QueuedInsertPayload(rows=[{"id": 1}]))
    await worker.add_message(msg)

    await worker.flush_table_group("evnt")

    assert worker.failure_counts["evnt"] == 1
    assert msg.nacked is True

    # Now succeed: failure count for the group is cleared.
    sink.fail = False
    msg2 = _FakeMessage(QueuedInsertPayload(rows=[{"id": 2}]))
    await worker.add_message(msg2)
    await worker.flush_table_group("evnt")

    assert "evnt" not in worker.failure_counts
    assert msg2.acked is True


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_flush_timeout_requeues_messages_and_records_failure(anyio_backend):
    worker = _worker(_HangingSink(), insert_timeout_seconds=0.01)
    msg = _FakeMessage(QueuedInsertPayload(rows=[{"id": 1}]))
    await worker.add_message(msg)

    await worker.flush_table_group("evnt")

    assert msg.nacked is True
    assert msg.acked is False
    assert worker.failure_counts["evnt"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_worker_falls_back_to_insert_rows_when_no_insert_batch(anyio_backend):
    sink = _InsertRowsOnlySink()
    worker = _worker(sink)
    msg = _FakeMessage(QueuedInsertPayload(rows=[{"id": 9}]))

    await worker.add_message(msg)
    await worker.flush_all()

    assert sink.rows_calls == [("evnt", [{"id": 9}])]
    assert msg.acked is True


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_isolated_data_error_publishes_to_failed_queue_with_headers(
    anyio_backend,
):
    class _DataErrorSink:
        async def insert_batch(self, rows, table_group="evnt"):
            raise DataError("bad row")

    channel = _RecordingChannel()
    worker = _worker(_DataErrorSink(), channel=channel)
    msg = _FakeMessage(QueuedInsertPayload(rows=[{"id": 1}]))

    await worker.add_message(msg)
    await worker.flush_table_group("evnt")

    published = channel.default_exchange.published
    assert len(published) == 1
    assert published[0]["routing_key"] == "evnt.ingest.failed"
    headers = published[0]["message"].headers
    assert headers["x-evnt-source-queue"] == "evnt.ingest"
    assert headers["x-evnt-table-group"] == "evnt"
    assert "x-evnt-error" in headers
    assert msg.acked is True


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_isolated_data_error_requeues_on_failed_queue_publish_error(
    anyio_backend,
):
    class _DataErrorSink:
        async def insert_batch(self, rows, table_group="evnt"):
            raise DataError("bad row")

    channel = _RecordingChannel(exchange=_RecordingExchange(fail=True))
    worker = _worker(_DataErrorSink(), channel=channel)
    msg = _FakeMessage(QueuedInsertPayload(rows=[{"id": 1}]))

    await worker.add_message(msg)
    await worker.flush_table_group("evnt")

    # Could not move to failed queue, so the message is requeued and the group
    # is marked failed (for backoff).
    assert msg.nacked is True
    assert msg.acked is False
    assert worker.failure_counts["evnt"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_add_message_rejects_invalid_payload(anyio_backend):
    worker = _worker(_BatchSink())

    class _BadMessage(_FakeMessage):
        def __init__(self):
            self.body = b"not-json{"
            self.acked = False
            self.nacked = False
            self.rejected = False

    msg = _BadMessage()
    await worker.add_message(msg)

    assert msg.rejected is True
    assert worker.pending_batches == {}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_add_message_acks_empty_rows_without_buffering(anyio_backend):
    worker = _worker(_BatchSink())
    msg = _FakeMessage(QueuedInsertPayload(rows=[]))

    await worker.add_message(msg)

    assert msg.acked is True
    assert worker.pending_batches == {}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_worker_close_closes_channel_and_connection(anyio_backend):
    channel = _RecordingChannel()
    worker = _worker(_BatchSink(), channel=channel)

    await worker.close()

    assert channel.closed is True
    assert worker.connection.closed is True
