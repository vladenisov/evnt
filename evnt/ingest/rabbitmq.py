"""RabbitMQ-backed ingest pipeline."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import structlog
from aio_pika import DeliveryMode, Message, connect_robust
from aio_pika.abc import (
    AbstractChannel,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)
from aio_pika.exceptions import AuthenticationError, ProbableAuthenticationError
from clickhouse_connect.driver.exceptions import DataError
from core.config import RabbitMQConfig
from core.constants import WORKER_HEARTBEAT_SECONDS, WORKER_LIVENESS_PATH
from core.protocols import RowSink
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

# Milliseconds per second — used when converting ms config values to seconds.
_MS_PER_SECOND = 1000

# Assigned to a name so ruff-format (PEP 758 preview) does not rewrite the
# `except (A, B):` literal into the bare `except A, B:` form.
_AUTH_ERRORS = (AuthenticationError, ProbableAuthenticationError)


async def _declare_ingest_queues(
    channel: AbstractChannel,
    config: RabbitMQConfig,
) -> None:
    """Ensure the main and failed queues both exist."""

    await channel.declare_queue(config.queue_name, durable=True)
    await channel.declare_queue(config.resolved_failed_queue_name, durable=True)


def _resolve_table_name(
    tables: dict[str, Any],
    cluster_name: str | None,
    table_group: str,
) -> str:
    table_config = tables[table_group]
    if cluster_name:
        return table_config["distributed"]["name"]
    return table_config["local"]["name"]


class QueuedInsertPayload(BaseModel):
    """Serialized payload stored in RabbitMQ."""

    table_group: str = "evnt"
    rows: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(slots=True)
class PendingMessage:
    """Pending RabbitMQ message awaiting a batch flush."""

    message: AbstractIncomingMessage
    payload: QueuedInsertPayload


async def connect_rabbitmq(config: RabbitMQConfig) -> AbstractRobustConnection:
    """Create a single robust RabbitMQ connection attempt."""

    return await connect_robust(
        host=config.host,
        port=config.port,
        login=config.username,
        password=config.password.get_secret_value(),
        virtualhost=config.virtualhost,
        timeout=config.connect_timeout_seconds,
    )


async def retry_rabbitmq_startup[T](
    config: RabbitMQConfig,
    operation: str,
    callback: Callable[[], Awaitable[T]],
) -> T:
    """Retry RabbitMQ startup operations until the configured wait expires."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.startup_timeout_seconds
    attempt = 0

    while True:
        attempt += 1
        try:
            return await callback()
        except _AUTH_ERRORS:
            raise
        except Exception as exc:
            remaining_seconds = deadline - loop.time()
            if remaining_seconds <= 0:
                logger.error(
                    "RabbitMQ startup wait expired",
                    operation=operation,
                    attempt=attempt,
                    host=config.host,
                    port=config.port,
                    startup_timeout_seconds=config.startup_timeout_seconds,
                    error=str(exc),
                )
                raise

            retry_in_seconds = min(
                config.startup_retry_interval_ms / _MS_PER_SECOND,
                remaining_seconds,
            )
            logger.warning(
                "RabbitMQ is not ready yet, retrying",
                operation=operation,
                attempt=attempt,
                host=config.host,
                port=config.port,
                retry_in_seconds=retry_in_seconds,
                remaining_seconds=remaining_seconds,
                error=str(exc),
            )
            await asyncio.sleep(retry_in_seconds)


class RabbitMQPublisher:
    """Queues rows in RabbitMQ instead of writing directly to ClickHouse."""

    def __init__(
        self,
        connection: AbstractRobustConnection,
        channel: AbstractChannel,
        config: RabbitMQConfig,
        tables: dict[str, Any],
        database: str,
        cluster_name: str | None = None,
    ) -> None:
        self.connection = connection
        self.channel = channel
        self.config = config
        self.tables = tables
        self.database = database
        self.cluster_name = cluster_name

    @classmethod
    async def create(
        cls,
        config: RabbitMQConfig,
        tables: dict[str, Any],
        database: str,
        cluster_name: str | None = None,
    ) -> RabbitMQPublisher:
        """Create a publisher and ensure the queue exists."""

        async def _create() -> RabbitMQPublisher:
            connection = await connect_rabbitmq(config)
            try:
                channel = await connection.channel(publisher_confirms=True)
                await _declare_ingest_queues(channel, config)
            except Exception:
                await connection.close()
                raise

            return cls(connection, channel, config, tables, database, cluster_name)

        return await retry_rabbitmq_startup(config, "publisher_create", _create)

    async def insert_rows(
        self,
        rows: list[dict[str, Any]],
        table_group: str = "evnt",
    ) -> None:
        """Publish rows to RabbitMQ as a persistent message."""

        if not rows:
            logger.debug("No rows to enqueue", table_group=table_group)
            return

        encoded_rows = await asyncio.to_thread(jsonable_encoder, rows)
        payload = QueuedInsertPayload(
            table_group=table_group,
            rows=encoded_rows,
        )
        message = Message(
            body=payload.model_dump_json().encode("utf-8"),
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
            type="evnt.insert",
        )

        await self.channel.default_exchange.publish(
            message,
            routing_key=self.config.queue_name,
            mandatory=True,
        )
        logger.debug(
            "Queued rows for ingestion",
            table_group=table_group,
            rows_count=len(rows),
            queue_name=self.config.queue_name,
        )

    async def get_table_name(self, table_group: str = "evnt") -> str:
        """Return the eventual ClickHouse table name for the table group."""

        return _resolve_table_name(self.tables, self.cluster_name, table_group)

    async def close(self) -> None:
        """Close RabbitMQ resources."""

        await self.channel.close()
        await self.connection.close()


class RabbitMQHealthChecker:
    """Health checker for RabbitMQ-backed ingest."""

    def __init__(self, channel: AbstractChannel, *queue_names: str) -> None:
        self.channel = channel
        self.queue_names = queue_names

    async def check(self) -> dict[str, bool]:
        """Check that the queue is reachable."""

        healthy = True
        try:
            for queue_name in self.queue_names:
                await self.channel.declare_queue(
                    queue_name,
                    durable=True,
                    passive=True,
                )
        except Exception as exc:
            logger.warning(
                "RabbitMQ health check failed",
                error=str(exc),
                queue_names=self.queue_names,
            )
            healthy = False

        return {"rabbitmq": healthy}


class RabbitMQBatchWorker:
    """Consumes RabbitMQ messages and writes batched rows to ClickHouse."""

    # Cap exponential backoff so the consumer never stalls indefinitely
    # between iterations when ClickHouse is persistently unavailable.
    MAX_BACKOFF_SECONDS = 60.0

    def __init__(
        self,
        connection: AbstractRobustConnection,
        channel: AbstractChannel,
        queue: AbstractQueue,
        sink: RowSink,
        config: RabbitMQConfig,
    ) -> None:
        self.connection = connection
        self.channel = channel
        self.queue = queue
        self.sink = sink
        self.config = config
        self.failed_queue_name = config.resolved_failed_queue_name
        self.flush_interval = config.batch_timeout_ms / _MS_PER_SECOND
        self.retry_delay = config.retry_delay_ms / _MS_PER_SECOND
        self.pending_batches: dict[str, list[PendingMessage]] = defaultdict(list)
        self.pending_row_counts: dict[str, int] = defaultdict(int)
        # Per-table-group consecutive insert-failure counts, used to drive a
        # capped exponential backoff applied in ``run`` BETWEEN iterations
        # (never while holding/awaiting an in-flight message).
        self.failure_counts: dict[str, int] = defaultdict(int)

    @classmethod
    async def create(
        cls,
        sink: RowSink,
        config: RabbitMQConfig,
    ) -> RabbitMQBatchWorker:
        """Create a worker bound to the configured queue."""

        async def _create() -> RabbitMQBatchWorker:
            connection = await connect_rabbitmq(config)
            try:
                channel = await connection.channel(publisher_confirms=True)
                await channel.set_qos(prefetch_count=config.prefetch_count)
                await _declare_ingest_queues(channel, config)
                queue = await channel.declare_queue(config.queue_name, durable=True)
            except Exception:
                await connection.close()
                raise

            return cls(connection, channel, queue, sink, config)

        return await retry_rabbitmq_startup(config, "worker_create", _create)

    async def run(self) -> None:
        """Start consuming and flushing batches."""

        logger.info(
            "Starting RabbitMQ batch worker",
            queue_name=self.config.queue_name,
            batch_size=self.config.batch_size,
            batch_timeout_ms=self.config.batch_timeout_ms,
        )

        iterator = self.queue.iterator()
        next_message_task: asyncio.Task[AbstractIncomingMessage] | None = None
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            async with iterator:
                while True:
                    # Apply any pending capped exponential backoff BEFORE
                    # fetching the next message so failing inserts do not stall
                    # the consumer mid-flush (which would hold prefetch and
                    # block ack/nack of already-received messages).
                    await self._apply_backoff()

                    if next_message_task is None:
                        next_message_task = asyncio.create_task(iterator.__anext__())
                    try:
                        message = await asyncio.wait_for(
                            asyncio.shield(next_message_task),
                            timeout=self.flush_interval,
                        )
                    except TimeoutError:
                        await self.flush_all()
                        self._mark_alive()
                        continue
                    except StopAsyncIteration:
                        break

                    next_message_task = None
                    await self.add_message(message)
                    self._mark_alive()
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            if next_message_task is not None and not next_message_task.done():
                next_message_task.cancel()
                with suppress(asyncio.CancelledError):
                    await next_message_task
            await self.flush_all()

    def _backoff_seconds(self) -> float:
        """Return the current backoff delay derived from failure counts.

        Uses the worst (highest) consecutive-failure count across all table
        groups so a single persistently failing destination throttles the
        loop. Returns ``0`` when there have been no recent failures.
        """

        max_failures = max(self.failure_counts.values(), default=0)
        if max_failures <= 0:
            return 0.0
        # base * 2**(n-1), capped, so the first failure waits ``retry_delay``.
        delay = self.retry_delay * (2 ** (max_failures - 1))
        return min(delay, self.MAX_BACKOFF_SECONDS)

    async def _apply_backoff(self) -> None:
        """Sleep for the capped exponential backoff between run iterations."""

        delay = self._backoff_seconds()
        if delay <= 0:
            return
        logger.warning(
            "Backing off before next RabbitMQ batch iteration",
            backoff_seconds=delay,
            failure_counts=dict(self.failure_counts),
        )
        await asyncio.sleep(delay)
        # Refresh liveness right after a long backoff so a sustained backend
        # outage cannot make the healthcheck see a stale file and trigger a
        # spurious restart while the worker is healthy and retrying.
        self._mark_alive()

    def _mark_alive(self) -> None:
        """Record worker liveness; never crash the worker on I/O errors."""

        try:
            WORKER_LIVENESS_PATH.write_text(str(time.time()))
        except OSError as exc:
            logger.warning(
                "Failed to write worker liveness file",
                error=str(exc),
                path=str(WORKER_LIVENESS_PATH),
            )

    async def _heartbeat_loop(self) -> None:
        """Refresh the liveness file on a fixed cadence.

        Runs as a background task for the worker's lifetime so liveness is
        decoupled from message flow, the batch-flush timeout, and backoff. This
        keeps an idle worker (or one configured with a large batch timeout) from
        being mistaken for dead by ``queue healthcheck``.
        """

        while True:
            self._mark_alive()
            await asyncio.sleep(WORKER_HEARTBEAT_SECONDS)

    async def add_message(self, message: AbstractIncomingMessage) -> None:
        """Decode and buffer a message."""

        try:
            payload = QueuedInsertPayload.model_validate_json(message.body)
        except Exception as exc:
            logger.error("Dropping invalid queue payload", error=str(exc))
            await message.reject(requeue=False)
            return

        if not payload.rows:
            await message.ack()
            return

        self.pending_batches[payload.table_group].append(
            PendingMessage(message=message, payload=payload),
        )
        self.pending_row_counts[payload.table_group] += len(payload.rows)

        if self.pending_row_counts[payload.table_group] >= self.config.batch_size:
            await self.flush_table_group(payload.table_group)

    async def flush_all(self) -> None:
        """Flush all pending batches."""

        for table_group in list(self.pending_batches):
            await self.flush_table_group(table_group)

    async def _insert_rows(
        self,
        rows: list[dict[str, Any]],
        table_group: str,
    ) -> None:
        insert_batch = getattr(self.sink, "insert_batch", None)
        if callable(insert_batch):
            await insert_batch(rows, table_group=table_group)
            return
        await self.sink.insert_rows(rows, table_group=table_group)

    async def _publish_failed_message(
        self,
        item: PendingMessage,
        table_group: str,
        error: Exception,
    ) -> None:
        """Move an isolated invalid message out of the main ingest queue."""

        headers = dict(getattr(item.message, "headers", {}) or {})
        headers["x-evnt-source-queue"] = self.config.queue_name
        headers["x-evnt-error"] = str(error)
        headers["x-evnt-table-group"] = table_group

        await self.channel.default_exchange.publish(
            Message(
                body=item.message.body,
                headers=headers,
                content_type=getattr(item.message, "content_type", None)
                or "application/json",
                delivery_mode=DeliveryMode.PERSISTENT,
                type=getattr(item.message, "type", None) or "evnt.insert.failed",
            ),
            routing_key=self.failed_queue_name,
            mandatory=True,
        )

    async def _flush_pending_messages(
        self,
        table_group: str,
        pending: list[PendingMessage],
    ) -> bool:
        """Flush a set of pending messages.

        Returns ``True`` when the messages made progress off the main queue
        (inserted or routed to the failed queue) and ``False`` when a transient
        failure caused them to be nack'd/requeued. Backoff (applied between run
        iterations) is driven by this result so the consumer is never stalled
        mid-flush while holding prefetch or before ack/nack of in-flight
        messages.
        """

        if not pending:
            return True

        rows = [row for item in pending for row in item.payload.rows]
        rows_count = len(rows)
        try:
            await self._insert_rows(rows, table_group)
        except DataError as exc:
            if len(pending) == 1:
                logger.warning(
                    "Isolated queue message failed ClickHouse validation, "
                    "moving to failed queue",
                    error=str(exc),
                    table_group=table_group,
                    rows_count=rows_count,
                    messages_count=1,
                    failed_queue_name=self.failed_queue_name,
                )
                try:
                    await self._publish_failed_message(pending[0], table_group, exc)
                except Exception as publish_exc:
                    logger.error(
                        "Failed to move invalid queue message to failed queue,"
                        " requeueing",
                        error=str(publish_exc),
                        table_group=table_group,
                        rows_count=rows_count,
                        messages_count=1,
                        failed_queue_name=self.failed_queue_name,
                    )
                    await pending[0].message.nack(requeue=True)
                    return False
                await pending[0].message.ack()
                return True

            midpoint = max(1, len(pending) // 2)
            logger.warning(
                "Batch insert failed with ClickHouse data error, splitting batch",
                error=str(exc),
                table_group=table_group,
                rows_count=rows_count,
                messages_count=len(pending),
            )
            first = await self._flush_pending_messages(table_group, pending[:midpoint])
            second = await self._flush_pending_messages(table_group, pending[midpoint:])
            return first and second
        except Exception as exc:
            logger.error(
                "Batch insert failed, requeueing messages",
                error=str(exc),
                table_group=table_group,
                rows_count=rows_count,
                messages_count=len(pending),
            )
            for item in pending:
                await item.message.nack(requeue=True)
            return False

        for item in pending:
            await item.message.ack()

        logger.debug(
            "Flushed queued rows",
            table_group=table_group,
            rows_count=rows_count,
            messages_count=len(pending),
        )
        return True

    async def flush_table_group(self, table_group: str) -> None:
        """Flush a single table group and ack or requeue source messages.

        Updates the per-table-group consecutive-failure count that drives the
        capped exponential backoff applied between run-loop iterations: the
        count is reset on success and incremented on a transient failure.
        """

        pending = self.pending_batches.pop(table_group, [])
        self.pending_row_counts.pop(table_group, 0)
        if not pending:
            return
        succeeded = await self._flush_pending_messages(table_group, pending)
        if succeeded:
            self.failure_counts.pop(table_group, None)
        else:
            self.failure_counts[table_group] += 1

    async def close(self) -> None:
        """Close RabbitMQ resources."""

        await self.channel.close()
        await self.connection.close()
