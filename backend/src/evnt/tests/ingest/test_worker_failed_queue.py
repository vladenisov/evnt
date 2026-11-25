"""What the worker's copy for the failed queue must survive.

Two things only a broker enforces: a header larger than ``frame_max`` closes
the connection, and a mandatory publish with no queue behind it is returned
rather than refused. The unit tests pin the worker's side of both over a fake
exchange; the ``integration`` tests run the real worker against RabbitMQ
(``EVNT_IT_RABBITMQ_*``, see ``tests/integration/conftest.py``).
"""

import asyncio
import json
import os
import socket
import uuid
from contextlib import suppress

import aio_pika
import pytest
from aio_pika import DeliveryMode, Message
from aiormq.abc import DeliveredMessage
from clickhouse_connect.driver.exceptions import DataError
from pamqp.commands import Basic
from pamqp.header import ContentHeader
from pydantic import SecretStr

import evnt.ingest.flush as flush_module
from evnt.config import RabbitMQConfig
from evnt.ingest import worker as worker_module
from evnt.ingest.rabbitmq import QueuedInsertPayload
from evnt.ingest.worker import RabbitMQBatchWorker


class _RecordingExchange:
    def __init__(self):
        self.published = []

    async def publish(self, message, routing_key, mandatory=True):
        self.published.append(message)


class _ReturningExchange:
    """An exchange on a channel without ``on_return_raises``: hands the message back."""

    async def publish(self, message, routing_key, mandatory=True):
        return DeliveredMessage(
            delivery=Basic.Return(reply_code=312, reply_text="NO_ROUTE", routing_key=routing_key),
            header=ContentHeader(),
            body=message.body,
            channel=None,
        )


class _FakeChannel:
    def __init__(self, exchange):
        self.default_exchange = exchange


class _FakeMessage:
    def __init__(self, body):
        self.body = body
        self.headers = {}
        self.content_type = "application/json"
        self.type = "evnt.insert"
        self.acked = False
        self.nacked = False

    async def ack(self):
        self.acked = True

    async def nack(self, requeue=True):
        self.nacked = requeue


class _Sink:
    def __init__(self, error=None):
        self.error = error
        self.rows = []

    async def insert_rows(self, rows, table_group="evnt"):
        if self.error is not None:
            raise self.error
        self.rows.extend(rows)


def _worker(sink, exchange):
    return RabbitMQBatchWorker(
        connection=object(),
        channel=_FakeChannel(exchange),
        queue=object(),
        sink=sink,
        config=RabbitMQConfig(batch_size=10, batch_timeout_ms=1000, retry_delay_ms=100),
    )


def _wrong_shape_body(rows_count):
    # Rows that are not objects: pydantic reports each of them.
    return json.dumps({"rows": list(range(rows_count))}).encode()


def _row_message(row_id=1):
    return _FakeMessage(QueuedInsertPayload(rows=[{"id": row_id}]).model_dump_json().encode())


@pytest.mark.anyio
@pytest.mark.parametrize("rows_count", [2000, 20000])
async def test_undecodable_payload_with_many_errors_gets_a_bounded_error_header(
    anyio_backend, rows_count
):
    exchange = _RecordingExchange()
    worker = _worker(_Sink(), exchange)
    body = _wrong_shape_body(rows_count)
    msg = _FakeMessage(body)

    await worker.add_message(msg)

    parked = exchange.published[0]
    assert parked.body == body
    error = parked.headers["x-evnt-error"]
    assert error.startswith(f"{rows_count} validation errors")
    assert len(error.encode()) <= flush_module._MAX_ERROR_TEXT_BYTES
    assert msg.acked is True


@pytest.mark.anyio
async def test_refused_rows_get_a_bounded_error_header(anyio_backend):
    exchange = _RecordingExchange()
    worker = _worker(_Sink(DataError("x" * 500_000)), exchange)
    msg = _row_message()

    await worker.add_message(msg)
    await worker.flush_table_group("evnt")

    error = exchange.published[0].headers["x-evnt-error"]
    assert len(error.encode()) == flush_module._MAX_ERROR_TEXT_BYTES
    assert error.endswith(flush_module._ERROR_TEXT_TRUNCATED)
    assert msg.acked is True


def test_error_text_keeps_short_errors_and_cuts_long_ones_between_characters():
    limit = flush_module._MAX_ERROR_TEXT_BYTES
    suffix = flush_module._ERROR_TEXT_TRUNCATED
    assert flush_module._error_text(RuntimeError("short")) == "short"
    assert flush_module._error_text(RuntimeError()) == "RuntimeError()"
    assert flush_module._error_text(RuntimeError("x" * limit)) == "x" * limit

    text = flush_module._error_text(RuntimeError("é" * limit))

    assert text == "é" * ((limit - len(suffix)) // 2) + suffix
    assert len(text.encode()) <= limit


@pytest.mark.anyio
async def test_undecodable_payload_is_requeued_when_the_broker_returns_its_copy(anyio_backend):
    worker = _worker(_Sink(), _ReturningExchange())
    msg = _FakeMessage(b"not-json{")

    await worker.add_message(msg)

    # The failed queue is gone, so the copy went nowhere: keep the original.
    assert msg.nacked is True
    assert msg.acked is False
    assert worker.failure_counts == {"<undecodable>": 1}


@pytest.mark.anyio
async def test_refused_rows_are_requeued_when_the_broker_returns_their_copy(anyio_backend):
    worker = _worker(_Sink(DataError("bad row")), _ReturningExchange())
    msg = _row_message()

    await worker.add_message(msg)
    await worker.flush_table_group("evnt")

    assert msg.nacked is True
    assert msg.acked is False
    assert worker.failure_counts == {"evnt": 1}


@pytest.fixture
def broker_config(monkeypatch, tmp_path):
    """A worker config for the test RabbitMQ, on a queue of its own."""
    host = os.environ.get("EVNT_IT_RABBITMQ_HOST", "localhost")
    port = int(os.environ.get("EVNT_IT_RABBITMQ_PORT", "5672"))
    try:
        socket.create_connection((host, port), timeout=3).close()
    except OSError as exc:
        reason = f"RabbitMQ at {host}:{port}: {exc}"
        if os.environ.get("EVNT_IT_REQUIRED") == "1":
            pytest.fail(f"EVNT_IT_REQUIRED=1 but {reason}")
        pytest.skip(reason)
    monkeypatch.setattr(worker_module, "WORKER_LIVENESS_PATH", tmp_path / "worker.alive")
    return RabbitMQConfig(
        host=host,
        port=port,
        username=os.environ.get("EVNT_IT_RABBITMQ_USER", "guest"),
        password=SecretStr(os.environ.get("EVNT_IT_RABBITMQ_PASSWORD", "guest")),
        queue_name=f"evnt.it.{uuid.uuid4().hex[:8]}",
        batch_timeout_ms=200,
        retry_delay_ms=50,
        startup_timeout_seconds=5,
        publish_timeout_seconds=5,
    )


class _Broker:
    """A second connection that publishes to the worker and inspects its queues."""

    def __init__(self, connection, config):
        self.connection = connection
        self.config = config

    async def publish(self, body):
        async with self.connection.channel() as channel:
            await channel.default_exchange.publish(
                Message(body, delivery_mode=DeliveryMode.PERSISTENT),
                routing_key=self.config.queue_name,
            )

    async def depth(self, queue_name):
        async with self.connection.channel() as channel:
            queue = await channel.declare_queue(queue_name, durable=True, passive=True)
            return queue.declaration_result.message_count

    async def take(self, queue_name):
        async with self.connection.channel() as channel:
            queue = await channel.get_queue(queue_name)
            return await queue.get(no_ack=True, fail=False)

    async def delete(self, queue_name):
        async with self.connection.channel() as channel:
            await channel.queue_delete(queue_name)


async def _eventually(check, timeout=10.0):
    async with asyncio.timeout(timeout):
        while not await check():
            await asyncio.sleep(0.05)


class _RunningWorker:
    """The real worker consuming ``config.queue_name`` for the length of a test."""

    def __init__(self, config):
        self.config = config
        self.sink = _Sink()

    async def __aenter__(self):
        self.worker = await RabbitMQBatchWorker.create(self.sink, self.config)
        self.broker = _Broker(
            await aio_pika.connect(
                host=self.config.host,
                port=self.config.port,
                login=self.config.username,
                password=self.config.password.get_secret_value(),
            ),
            self.config,
        )
        self.task = asyncio.create_task(self.worker.run())
        return self

    async def __aexit__(self, *exc_info):
        self.task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await self.task
        with suppress(Exception):
            await self.worker.close()
        for queue_name in (self.config.queue_name, self.config.resolved_failed_queue_name):
            await self.broker.delete(queue_name)
        await self.broker.connection.close()

    async def stop(self):
        """Stop consuming so unacked messages go back to the queue to be counted."""
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        await self.worker.close()


@pytest.mark.integration
@pytest.mark.anyio
@pytest.mark.parametrize("rows_count", [50, 20000])
async def test_broker_takes_the_parked_copy_of_a_many_error_payload(
    anyio_backend, broker_config, rows_count
):
    failed_queue = broker_config.resolved_failed_queue_name
    body = _wrong_shape_body(rows_count)

    async with _RunningWorker(broker_config) as running:
        await running.broker.publish(body)

        async def _parked():
            return await running.broker.depth(failed_queue) == 1

        await _eventually(_parked)
        parked = await running.broker.take(failed_queue)
        assert parked.body == body
        assert parked.headers["x-evnt-failure"] == "undecodable"
        assert parked.headers["x-evnt-source-queue"] == broker_config.queue_name
        assert len(parked.headers["x-evnt-error"].encode()) <= flush_module._MAX_ERROR_TEXT_BYTES

        # The connection survived the copy: the worker still delivers.
        await running.broker.publish(
            QueuedInsertPayload(rows=[{"id": 7}]).model_dump_json().encode()
        )

        async def _delivered():
            return running.sink.rows == [{"id": 7}]

        await _eventually(_delivered)
        assert not running.task.done()
        assert await running.broker.depth(broker_config.queue_name) == 0


@pytest.mark.integration
@pytest.mark.anyio
async def test_undecodable_payload_stays_queued_while_the_failed_queue_is_missing(
    anyio_backend, broker_config
):
    failed_queue = broker_config.resolved_failed_queue_name

    async with _RunningWorker(broker_config) as running:
        await running.broker.delete(failed_queue)
        await running.broker.publish(b"not-json{")

        async def _backing_off():
            return running.worker.failure_counts.get("<undecodable>", 0) >= 2

        # Returned by the broker, requeued, redelivered, returned again.
        await _eventually(_backing_off)
        assert not running.task.done()

        await running.stop()
        assert await running.broker.depth(broker_config.queue_name) == 1
