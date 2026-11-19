"""Behavioral tests for ``RabbitMQPublisher`` and worker backoff/flush paths.

All RabbitMQ interaction is faked: a recording exchange/channel captures
published messages and a fake message records ack/nack/reject so no broker or
network is required. These cover the publisher's enqueue contract and the
worker's per-table-group backoff, table-name resolution, and failed-queue
publish-failure fallback that ``test_rabbitmq.py`` does not.
"""

import asyncio
from datetime import UTC, datetime
from ipaddress import IPv4Address
from uuid import UUID

import pytest
from aio_pika.exceptions import ChannelInvalidStateError, ConnectionClosed, DeliveryError
from clickhouse_connect.driver.exceptions import DataError
from pamqp.commands import Basic

import evnt.ingest.rabbitmq as publisher_module
from evnt.config import RabbitMQConfig
from evnt.exceptions import IngestUnavailableError
from evnt.ingest.health import RabbitMQHealthChecker
from evnt.ingest.rabbitmq import (
    QueuedInsertPayload,
    RabbitMQPublisher,
)
from evnt.ingest.worker import RabbitMQBatchWorker
from evnt.observability.logging import ingest_unavailable_handler


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
async def test_publisher_preserves_original_client_metadata(anyio_backend):
    channel = _RecordingChannel()
    publisher = _publisher(channel)
    metadata = {"client_ip": "2001:db8::7", "ip_country": "CN"}
    await publisher.insert_rows(
        [
            {
                "user_ip": IPv4Address("0.0.0.0"),
                "extra": {"collector": metadata},
            }
        ]
    )
    payload = QueuedInsertPayload.model_validate_json(
        channel.default_exchange.published[0]["message"].body,
    )
    assert payload.rows[0]["user_ip"] == "0.0.0.0"
    assert payload.rows[0]["extra"]["collector"] == metadata


@pytest.mark.anyio
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
async def test_publisher_insert_rows_noop_for_empty_rows(anyio_backend):
    channel = _RecordingChannel()
    publisher = _publisher(channel)

    await publisher.insert_rows([], table_group="evnt")

    assert channel.default_exchange.published == []


@pytest.mark.anyio
async def test_publisher_get_table_name_local_and_distributed(anyio_backend):
    local_pub = _publisher(cluster_name=None)
    cluster_pub = _publisher(cluster_name="prod")

    assert await local_pub.get_table_name() == "events_local"
    assert await cluster_pub.get_table_name() == "events_distributed"


@pytest.mark.anyio
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


def test_backoff_survives_a_failure_count_beyond_float_range():
    # About 17 hours of a 60 s backoff; 2.0 ** 1024 would raise OverflowError
    # and stop the worker in the middle of a long outage.
    worker = _worker(_BatchSink(), retry_delay_ms=1000)
    worker.failure_counts["evnt"] = 5000

    assert worker._backoff_seconds() == RabbitMQBatchWorker.MAX_BACKOFF_SECONDS
    assert worker._pending_backoff_seconds() == RabbitMQBatchWorker.MAX_BACKOFF_SECONDS


@pytest.mark.anyio
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
async def test_flush_timeout_requeues_messages_and_records_failure(anyio_backend):
    worker = _worker(_HangingSink(), insert_timeout_seconds=0.01)
    msg = _FakeMessage(QueuedInsertPayload(rows=[{"id": 1}]))
    await worker.add_message(msg)

    await worker.flush_table_group("evnt")

    assert msg.nacked is True
    assert msg.acked is False
    assert worker.failure_counts["evnt"] == 1


@pytest.mark.anyio
async def test_worker_falls_back_to_insert_rows_when_no_insert_batch(anyio_backend):
    sink = _InsertRowsOnlySink()
    worker = _worker(sink)
    msg = _FakeMessage(QueuedInsertPayload(rows=[{"id": 9}]))

    await worker.add_message(msg)
    await worker.flush_all()

    assert sink.rows_calls == [("evnt", [{"id": 9}])]
    assert msg.acked is True


@pytest.mark.anyio
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
async def test_add_message_acks_empty_rows_without_buffering(anyio_backend):
    worker = _worker(_BatchSink())
    msg = _FakeMessage(QueuedInsertPayload(rows=[]))

    await worker.add_message(msg)

    assert msg.acked is True
    assert worker.pending_batches == {}


@pytest.mark.anyio
async def test_worker_close_closes_channel_and_connection(anyio_backend):
    channel = _RecordingChannel()
    worker = _worker(_BatchSink(), channel=channel)

    await worker.close()

    assert channel.closed is True
    assert worker.connection.closed is True


class _StalledExchange:
    """Accepts publishes but never confirms them, like a broker in a memory alarm."""

    def __init__(self):
        self.started = 0

    async def publish(self, message, routing_key, mandatory=True):
        self.started += 1
        await asyncio.Event().wait()


def _publisher_with(config: RabbitMQConfig, channel):
    return RabbitMQPublisher(
        connection=_FakeConnection(),
        channel=channel,
        config=config,
        tables={"evnt": {"local": {"name": "l"}, "distributed": {"name": "d"}}},
        database="evnt",
    )


@pytest.mark.anyio
async def test_publisher_fails_fast_when_the_broker_stops_confirming(anyio_backend):
    channel = _RecordingChannel(exchange=_StalledExchange())
    publisher = _publisher_with(
        RabbitMQConfig(publish_timeout_seconds=0.05),
        channel,
    )

    with pytest.raises(IngestUnavailableError) as excinfo:
        await publisher.insert_rows([{"aid": "a"}])

    assert excinfo.value.retry_after_seconds == 1


@pytest.mark.anyio
async def test_publisher_caps_unconfirmed_publishes(anyio_backend):
    exchange = _StalledExchange()
    publisher = _publisher_with(
        RabbitMQConfig(
            publish_timeout_seconds=0.1,
            max_inflight_publishes=3,
            coalesce_window_ms=0,
        ),
        _RecordingChannel(exchange=exchange),
    )

    results = await asyncio.gather(
        *(publisher.insert_rows([{"aid": "a"}]) for _ in range(10)),
        return_exceptions=True,
    )

    # Only the window's worth reached the broker; the rest timed out waiting for
    # a slot instead of piling more unconfirmed messages onto a stalled channel.
    assert exchange.started == 3
    assert all(isinstance(result, IngestUnavailableError) for result in results)


@pytest.mark.anyio
async def test_publishes_waiting_for_a_slot_hold_no_encoded_body(anyio_backend, monkeypatch):
    encoded: list[int] = []
    real_encode = publisher_module._encode_queued_insert_payload

    def _counting_encode(table_group, rows):
        encoded.append(len(rows))
        return real_encode(table_group, rows)

    monkeypatch.setattr(publisher_module, "_encode_queued_insert_payload", _counting_encode)
    publisher = _publisher_with(
        RabbitMQConfig(
            publish_timeout_seconds=0.1,
            max_inflight_publishes=2,
            coalesce_window_ms=0,
        ),
        _RecordingChannel(exchange=_StalledExchange()),
    )

    await asyncio.gather(
        *(publisher.insert_rows([{"aid": "a"}]) for _ in range(10)),
        return_exceptions=True,
    )

    # Only the publishes that got a slot built a message body.
    assert len(encoded) == 2


@pytest.mark.anyio
async def test_ingest_unavailable_handler_answers_503_with_retry_after(anyio_backend):
    response = await ingest_unavailable_handler(
        None,
        IngestUnavailableError("down", retry_after_seconds=5),
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


class _HealthChannel:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.is_closed = False
        self.declared = []

    async def declare_queue(self, name, *, durable, passive, robust):
        self.declared.append((name, passive, robust))
        if name in self.missing:
            # The broker answers a failed passive declare by closing the channel.
            self.is_closed = True
            raise RuntimeError(f"NOT_FOUND - no queue '{name}'")


class _HealthConnection:
    def __init__(self, channels):
        self.channels = list(channels)
        self.opened = 0

    async def channel(self, publisher_confirms=True):
        self.opened += 1
        return self.channels.pop(0)


@pytest.mark.anyio
async def test_health_check_asks_the_broker_on_its_own_channel(anyio_backend):
    channel = _HealthChannel()
    connection = _HealthConnection([channel])
    checker = RabbitMQHealthChecker(connection, "q", "q.failed", timeout_seconds=1)

    assert await checker.check() == {"rabbitmq": True}
    assert await checker.check() == {"rabbitmq": True}

    # robust=False keeps the queues out of aio-pika's cache, so each check is a
    # real passive declare, and one channel is reused across checks.
    assert channel.declared == [("q", True, False), ("q.failed", True, False)] * 2
    assert connection.opened == 1


@pytest.mark.anyio
async def test_health_check_reports_a_missing_queue_and_reopens_its_channel(anyio_backend):
    broken = _HealthChannel(missing={"q.failed"})
    healthy = _HealthChannel()
    connection = _HealthConnection([broken, healthy])
    checker = RabbitMQHealthChecker(connection, "q", "q.failed", timeout_seconds=1)

    assert await checker.check() == {"rabbitmq": False}
    assert await checker.check() == {"rabbitmq": True}
    assert connection.opened == 2


@pytest.mark.anyio
async def test_health_check_times_out_instead_of_hanging(anyio_backend):
    class _HangingConnection:
        async def channel(self, publisher_confirms=True):
            await asyncio.Event().wait()

    checker = RabbitMQHealthChecker(_HangingConnection(), "q", timeout_seconds=0.05)

    assert await checker.check() == {"rabbitmq": False}


class _FailingExchange:
    def __init__(self, error):
        self.error = error

    async def publish(self, message, routing_key, mandatory=True):
        raise self.error


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        ChannelInvalidStateError("channel closed"),
        ConnectionClosed(320, "CONNECTION_FORCED"),
        DeliveryError(None, Basic.Nack(delivery_tag=1)),
    ],
    ids=["closed-channel", "connection-lost", "nack-or-return"],
)
async def test_publisher_answers_503_when_the_broker_drops_the_publish(anyio_backend, error):
    publisher = _publisher_with(
        RabbitMQConfig(coalesce_window_ms=0),
        _RecordingChannel(exchange=_FailingExchange(error)),
    )

    with pytest.raises(IngestUnavailableError):
        await publisher.insert_rows([{"aid": "a"}])


@pytest.mark.anyio
async def test_a_timed_out_publish_frees_its_window_slot(anyio_backend):
    class _StallOnce:
        def __init__(self):
            self.calls = 0

        async def publish(self, message, routing_key, mandatory=True):
            self.calls += 1
            if self.calls == 1:
                await asyncio.Event().wait()

    exchange = _StallOnce()
    publisher = _publisher_with(
        RabbitMQConfig(
            publish_timeout_seconds=0.05,
            max_inflight_publishes=1,
            coalesce_window_ms=0,
        ),
        _RecordingChannel(exchange=exchange),
    )

    with pytest.raises(IngestUnavailableError):
        await publisher.insert_rows([{"aid": "a"}])
    await publisher.insert_rows([{"aid": "b"}])

    assert exchange.calls == 2


@pytest.mark.anyio
async def test_health_check_closes_a_closed_channel_before_replacing_it(anyio_backend):
    class _Channel(_HealthChannel):
        def __init__(self):
            super().__init__()
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    first, second = _Channel(), _Channel()
    connection = _HealthConnection([first, second])
    checker = RabbitMQHealthChecker(connection, "q", timeout_seconds=1)

    await checker.check()
    first.is_closed = True
    await checker.check()

    # Closing it stops a robust channel from restoring itself after a
    # reconnect, which would leave a second channel open on the broker.
    assert first.close_calls == 1
    assert connection.opened == 2


@pytest.mark.anyio
async def test_merged_requests_all_get_503_when_their_publish_stalls(anyio_backend):
    """The default path end to end: coalescer, then publish timeout, then 503 for all."""
    exchange = _StalledExchange()
    publisher = _publisher_with(
        RabbitMQConfig(publish_timeout_seconds=0.05, coalesce_window_ms=5),
        _RecordingChannel(exchange=exchange),
    )

    results = await asyncio.gather(
        *(publisher.insert_rows([{"aid": str(i)}]) for i in range(5)),
        return_exceptions=True,
    )

    assert exchange.started == 1
    assert all(isinstance(result, IngestUnavailableError) for result in results)


def test_create_app_answers_ingest_unavailable_with_503():
    from evnt.main import create_app
    from evnt.observability.logging import ingest_unavailable_handler as handler

    app = create_app()

    assert app.exception_handlers[IngestUnavailableError] is handler
