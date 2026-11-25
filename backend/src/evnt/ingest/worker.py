"""RabbitMQ batch worker: consumes queued rows and writes them to ClickHouse.

Holds ``RabbitMQBatchWorker`` with its run loop, backoff and liveness. Its
flush, split, row-isolation and parking logic lives in ``evnt.ingest.flush``;
the publisher and the shared connection helpers live in
``evnt.ingest.rabbitmq``.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict
from contextlib import suppress

import structlog
from aio_pika.abc import (
    AbstractChannel,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)

from evnt.config import RabbitMQConfig
from evnt.constants import (
    WORKER_HEARTBEAT_SECONDS,
    WORKER_LIVENESS_PATH,
    WORKER_STALL_GRACE_SECONDS,
)
from evnt.ingest.flush import (
    _MAX_FAILED_INSERTS_WITHOUT_SUCCESS,
    PendingMessage,
    _BatchFlusher,
    _FlushOutcome,
    _IsolationStoppedError,
    _Rejected,
)
from evnt.ingest.insert_failures import _AttemptTracker
from evnt.ingest.rabbitmq import (
    _MS_PER_SECOND,
    QueuedInsertPayload,
    _declare_ingest_queues,
    connect_rabbitmq,
    retry_rabbitmq_startup,
)
from evnt.protocols import RowSink

__all__ = [
    "_MAX_FAILED_INSERTS_WITHOUT_SUCCESS",
    "PendingMessage",
    "RabbitMQBatchWorker",
    "_FlushOutcome",
    "_IsolationStoppedError",
    "_Rejected",
]

logger = structlog.get_logger(__name__)

# 2**32 times any retry delay is far beyond every sensible MAX_BACKOFF_SECONDS.
_MAX_BACKOFF_EXPONENT = 32


class RabbitMQBatchWorker(_BatchFlusher):
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
        self.insert_timeout = config.insert_timeout_seconds
        # A step longer than this means the loop is stuck on an await that
        # never returns (ack/nack/publish have no timeout of their own).
        self.stall_threshold = self.insert_timeout + WORKER_STALL_GRACE_SECONDS
        self.pending_batches: dict[str, list[PendingMessage]] = defaultdict(list)
        self.pending_row_counts: dict[str, int] = defaultdict(int)
        # Per-table-group consecutive insert-failure counts, used to drive a
        # capped exponential backoff applied in ``run`` BETWEEN retry attempts
        # (never while holding/awaiting an in-flight message).
        self.failure_counts: dict[str, int] = defaultdict(int)
        self._backoff_applied_counts: dict[str, int] = {}
        self._attempts = _AttemptTracker(config.max_insert_attempts)
        # Monotonic start of the batch-loop step in progress, ``None`` while the
        # loop is idle (waiting for messages or backing off).
        self._busy_since: float | None = None

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
                # on_return_raises: a mandatory publish to the failed queue the
                # broker cannot route raises, so the message is requeued, not acked.
                channel = await connection.channel(
                    publisher_confirms=True,
                    on_return_raises=True,
                )
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
                    self._busy_since = None
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
                        self._busy_since = time.monotonic()
                        await self.flush_all()
                        continue
                    except StopAsyncIteration as exc:
                        logger.error(
                            "RabbitMQ queue iterator stopped unexpectedly",
                            queue_name=self.config.queue_name,
                        )
                        raise RuntimeError(
                            "RabbitMQ queue iterator stopped unexpectedly",
                        ) from exc

                    next_message_task = None
                    self._busy_since = time.monotonic()
                    await self.add_message(message)
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            if next_message_task is not None and not next_message_task.done():
                next_message_task.cancel()
                with suppress(asyncio.CancelledError):
                    await next_message_task
            await self.flush_all()

    def _exponential_delay(self, failures: int) -> float:
        """Return ``retry_delay * 2 ** (failures - 1)`` without overflowing.

        Failure counts keep growing for as long as an outage lasts, and
        ``2.0 ** 1024`` raises OverflowError (after about 17 hours at 60 s), so
        the exponent is clamped; the caller caps the delay anyway.
        """

        exponent = min(failures - 1, _MAX_BACKOFF_EXPONENT)
        return float(self.retry_delay * 2.0**exponent)

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
        delay = self._exponential_delay(max_failures)
        return min(delay, self.MAX_BACKOFF_SECONDS)

    def _pending_backoff_seconds(self) -> float:
        """Return delay for failures that have not already backed off."""

        unapplied_failures = (
            failure_count
            for table_group, failure_count in self.failure_counts.items()
            if failure_count > self._backoff_applied_counts.get(table_group, 0)
        )
        max_failures = max(unapplied_failures, default=0)
        if max_failures <= 0:
            return 0.0
        delay = self._exponential_delay(max_failures)
        return min(delay, self.MAX_BACKOFF_SECONDS)

    async def _apply_backoff(self) -> None:
        """Sleep once for each newly observed capped backoff level."""

        delay = self._pending_backoff_seconds()
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
        self._backoff_applied_counts = dict(self.failure_counts)

    def _mark_alive(self) -> None:
        """Atomically record worker liveness without exposing a partial file."""

        temporary_path = WORKER_LIVENESS_PATH.with_name(
            f".{WORKER_LIVENESS_PATH.name}.{os.getpid()}.tmp",
        )
        try:
            temporary_path.write_text(str(time.time()))
            os.replace(temporary_path, WORKER_LIVENESS_PATH)
        except OSError as exc:
            with suppress(OSError):
                temporary_path.unlink()
            logger.warning(
                "Failed to write worker liveness file",
                error=str(exc),
                path=str(WORKER_LIVENESS_PATH),
            )

    def _busy_seconds(self) -> float:
        """Return how long the current batch-loop step has been running."""

        if self._busy_since is None:
            return 0.0
        return time.monotonic() - self._busy_since

    async def _heartbeat_loop(self) -> None:
        """Refresh the liveness file on a fixed cadence while the loop progresses.

        Runs as a background task for the worker's lifetime so liveness is
        decoupled from message flow, the batch-flush timeout, and backoff. This
        keeps an idle worker (or one configured with a large batch timeout) from
        being mistaken for dead by ``queue healthcheck``. Liveness is withheld
        while a single step runs past ``stall_threshold`` so a loop stuck on a
        hung await goes stale and gets restarted.
        """

        while True:
            busy_seconds = self._busy_seconds()
            if busy_seconds <= self.stall_threshold:
                self._mark_alive()
            else:
                logger.error(
                    "RabbitMQ batch worker loop stalled, withholding liveness",
                    busy_seconds=round(busy_seconds, 1),
                    stall_threshold_seconds=self.stall_threshold,
                )
            await asyncio.sleep(WORKER_HEARTBEAT_SECONDS)

    async def add_message(self, message: AbstractIncomingMessage) -> None:
        """Decode and buffer a message."""

        try:
            payload = QueuedInsertPayload.model_validate_json(message.body)
        except Exception as exc:
            await self._park_undecodable(message, exc)
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

    async def close(self) -> None:
        """Close RabbitMQ resources."""

        await self.channel.close()
        await self.connection.close()
