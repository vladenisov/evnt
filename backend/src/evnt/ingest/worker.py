"""RabbitMQ batch worker: consumes queued rows and writes them to ClickHouse.

Holds ``RabbitMQBatchWorker`` with its flush, split, row-isolation and parking
logic, and the per-flush bookkeeping types it uses. The publisher and the
shared connection helpers live in ``evnt.ingest.rabbitmq``.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any

import structlog
from aio_pika import DeliveryMode, Message
from aio_pika.abc import (
    AbstractChannel,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)
from clickhouse_connect.driver.exceptions import DataError

from evnt.config import RabbitMQConfig
from evnt.constants import (
    WORKER_HEARTBEAT_SECONDS,
    WORKER_LIVENESS_PATH,
    WORKER_STALL_GRACE_SECONDS,
)
from evnt.ingest.insert_failures import (
    InsertFailure,
    _AttemptTracker,
    classify_insert_failure,
)
from evnt.ingest.rabbitmq import (
    _MS_PER_SECOND,
    QueuedInsertPayload,
    _declare_ingest_queues,
    _encode_queued_insert_payload,
    connect_rabbitmq,
    retry_rabbitmq_startup,
)
from evnt.protocols import RowSink

logger = structlog.get_logger(__name__)

# Bisection tries both halves of a failing batch before splitting either, so
# while some messages of a flush are good an insert succeeds within about
# 2 * log2(bad messages) + 1 attempts, wherever the bad ones sit. A failure that
# hits every insert never does; past this many failed inserts with no success
# in a flush the worker stops splitting and requeues the rest, instead of
# spending about two inserts per row on every retry. The driver's DataError and
# a message on its last attempt while other inserts succeed are not capped.
_MAX_FAILED_INSERTS_WITHOUT_SUCCESS = 16
# 2**32 times any retry delay is far beyond every sensible MAX_BACKOFF_SECONDS.
_MAX_BACKOFF_EXPONENT = 32


@dataclass(slots=True)
class PendingMessage:
    """Pending RabbitMQ message awaiting a batch flush."""

    message: AbstractIncomingMessage
    payload: QueuedInsertPayload


@dataclass(slots=True)
class _Rejected:
    """A message, or some rows of a merged one, that failed to insert on its own."""

    item: PendingMessage
    error: Exception
    kind: InsertFailure
    # Only these rows failed; ``None`` when the message is a single row.
    rows: list[dict[str, Any]] | None = None
    # Every failure was the driver's DataError (see ``_settle_flush``).
    driver_data_error: bool = False
    # Parked because it reached ``max_insert_attempts`` while inserts of other
    # flushes succeeded, rather than on the proof of its own flush.
    attempt_limit: bool = False


@dataclass(slots=True)
class _FlushOutcome:
    """What one flush of a table group learned and still has to settle."""

    rejected: list[_Rejected] = field(default_factory=list)
    requeued: list[tuple[PendingMessage, InsertFailure]] = field(default_factory=list)
    # Some insert in this flush went through, so ClickHouse is taking writes and
    # a failure that persists in isolation belongs to the rows that caused it.
    inserted: bool = False
    failed_inserts: int = 0


class _IsolationStoppedError(Exception):
    """Row isolation hit a failure it must not bisect; the message is requeued."""

    def __init__(self, error: Exception, kind: InsertFailure) -> None:
        super().__init__(str(error))
        self.error = error
        self.kind = kind


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
        try:
            insert_batch = getattr(self.sink, "insert_batch", None)
            if callable(insert_batch):
                await insert_batch(rows, table_group=table_group)
                return
            await self.sink.insert_rows(rows, table_group=table_group)
        finally:
            # Each insert attempt is bounded by ``insert_timeout``; restart the
            # step clock so a long data-error split is not read as a stall.
            self._busy_since = time.monotonic()

    async def _try_insert(
        self,
        rows: list[dict[str, Any]],
        table_group: str,
        outcome: _FlushOutcome,
    ) -> Exception | None:
        """Run one bounded insert attempt and return its failure, if any."""

        try:
            async with asyncio.timeout(self.insert_timeout):
                await self._insert_rows(rows, table_group)
        except Exception as exc:
            outcome.failed_inserts += 1
            return exc
        outcome.inserted = True
        self._attempts.note_insert_success()
        return None

    def _should_isolate(
        self,
        error: Exception,
        kind: InsertFailure,
        items: list[PendingMessage],
        outcome: _FlushOutcome,
    ) -> bool:
        """Return whether a failed insert is split to find the rows behind it."""

        if kind is InsertFailure.SYSTEMIC:
            return False
        # The driver raises DataError before sending anything and its rows are
        # parked without further proof, so isolating them is cheap and decisive.
        if isinstance(error, DataError):
            return True
        if not outcome.inserted and outcome.failed_inserts > _MAX_FAILED_INSERTS_WITHOUT_SUCCESS:
            # Only a message that may be parked on this attempt is worth the
            # inserts a full bisection costs when nothing in the flush succeeds.
            return any(self._attempts.may_park(item.message) for item in items)
        if kind is InsertFailure.DETERMINISTIC:
            return True
        return any(self._attempts.is_last_attempt(item.message) for item in items)

    async def _ack_inserted(self, items: list[PendingMessage]) -> None:
        for item in items:
            await item.message.ack()
            self._attempts.forget(item.message)

    async def _publish_failed_message(
        self,
        rejected: _Rejected,
        table_group: str,
    ) -> None:
        """Move a refused message, or a message holding only its refused rows, aside.

        Bounded by ``publish_timeout_seconds``: under a broker alarm the publish
        would otherwise hang until the stall detector restarts the worker. A
        timeout raises like any other publish failure, and the caller requeues.
        """

        item = rejected.item
        error = rejected.error
        headers = dict(getattr(item.message, "headers", {}) or {})
        headers["x-evnt-source-queue"] = self.config.queue_name
        headers["x-evnt-error"] = str(error) or repr(error)
        headers["x-evnt-error-type"] = type(error).__name__
        headers["x-evnt-table-group"] = table_group
        code = getattr(error, "code", None)
        if code is not None:
            headers["x-evnt-error-code"] = str(code)
        if rejected.kind is InsertFailure.UNKNOWN or rejected.attempt_limit:
            headers["x-evnt-failure"] = "max_insert_attempts"
            headers["x-evnt-insert-attempts"] = str(self._attempts.failures(item.message) + 1)
        else:
            headers["x-evnt-failure"] = rejected.kind.value

        if rejected.rows is None:
            body = item.message.body
        else:
            body = _encode_queued_insert_payload(table_group, rejected.rows)

        async with asyncio.timeout(self.config.publish_timeout_seconds):
            await self.channel.default_exchange.publish(
                Message(
                    body=body,
                    headers=headers,
                    content_type=getattr(item.message, "content_type", None) or "application/json",
                    delivery_mode=DeliveryMode.PERSISTENT,
                    type=getattr(item.message, "type", None) or "evnt.insert.failed",
                ),
                routing_key=self.failed_queue_name,
                mandatory=True,
            )

    async def _insert_messages(
        self,
        table_group: str,
        items: list[PendingMessage],
        outcome: _FlushOutcome,
    ) -> Exception | None:
        """Insert the rows of ``items`` and ack them; return the failure, if any."""

        rows = [row for item in items for row in item.payload.rows]
        error = await self._try_insert(rows, table_group, outcome)
        if error is None:
            await self._ack_inserted(items)
            logger.debug(
                "Flushed queued rows",
                table_group=table_group,
                rows_count=len(rows),
                messages_count=len(items),
            )
        return error

    async def _flush_pending_messages(
        self,
        table_group: str,
        pending: list[PendingMessage],
        outcome: _FlushOutcome,
    ) -> None:
        """Insert ``pending``, splitting a failing batch to find the messages behind it.

        Inserted messages are acked here. Failures are only recorded in
        ``outcome``: whether a refused message is parked depends on whether any
        other insert of the flush succeeds, which is known once the flush is
        over (see ``_settle_flush``).
        """

        if not pending:
            return
        error = await self._insert_messages(table_group, pending, outcome)
        if error is not None:
            await self._split_failed_messages(table_group, pending, error, outcome)

    async def _split_failed_messages(
        self,
        table_group: str,
        pending: list[PendingMessage],
        error: Exception,
        outcome: _FlushOutcome,
    ) -> None:
        """Find the messages behind ``error``, which an insert of all of ``pending`` raised.

        Both halves are inserted before either is split further: a requeued
        poison message returns to the head of the queue, so a left-first
        descent would fail on every insert down to it and hit the no-success
        cap before reaching it, while its sibling half usually goes through
        at once.
        """

        kind = classify_insert_failure(error)
        rows_count = sum(len(item.payload.rows) for item in pending)
        if not self._should_isolate(error, kind, pending, outcome):
            if isinstance(error, TimeoutError):
                logger.error(
                    "Batch insert timed out, requeueing messages",
                    table_group=table_group,
                    rows_count=rows_count,
                    messages_count=len(pending),
                    insert_timeout_seconds=self.insert_timeout,
                )
            else:
                logger.error(
                    "Batch insert failed, requeueing messages",
                    error=str(error),
                    error_type=type(error).__name__,
                    failure=kind.value,
                    table_group=table_group,
                    rows_count=rows_count,
                    messages_count=len(pending),
                )
            outcome.requeued.extend((item, kind) for item in pending)
            return

        if len(pending) > 1:
            logger.warning(
                "Batch insert failed, splitting batch to isolate the failing messages",
                error=str(error),
                error_type=type(error).__name__,
                failure=kind.value,
                table_group=table_group,
                rows_count=rows_count,
                messages_count=len(pending),
            )
            midpoint = len(pending) // 2
            halves = (pending[:midpoint], pending[midpoint:])
            errors = [await self._insert_messages(table_group, half, outcome) for half in halves]
            for half, half_error in zip(halves, errors, strict=True):
                if half_error is not None:
                    await self._split_failed_messages(table_group, half, half_error, outcome)
            return

        item = pending[0]
        if rows_count > 1:
            await self._isolate_failed_rows(item, table_group, error, outcome)
            return
        outcome.rejected.append(
            _Rejected(item, error, kind, driver_data_error=isinstance(error, DataError)),
        )

    async def _split_failed_rows(
        self,
        rows: list[dict[str, Any]],
        error: Exception,
        table_group: str,
        item: PendingMessage,
        outcome: _FlushOutcome,
    ) -> list[tuple[dict[str, Any], Exception, InsertFailure]]:
        """Return the rows that fail alone among ``rows``, whose insert raised ``error``.

        Inserts both halves before splitting either, as ``_split_failed_messages``
        does. Raises ``_IsolationStoppedError`` on a failure that must not be
        bisected.
        """

        kind = classify_insert_failure(error)
        if not self._should_isolate(error, kind, [item], outcome):
            raise _IsolationStoppedError(error, kind)
        if len(rows) == 1:
            return [(rows[0], error, kind)]
        midpoint = len(rows) // 2
        halves = (rows[:midpoint], rows[midpoint:])
        errors = [await self._try_insert(half, table_group, outcome) for half in halves]
        failed: list[tuple[dict[str, Any], Exception, InsertFailure]] = []
        for half, half_error in zip(halves, errors, strict=True):
            if half_error is not None:
                failed += await self._split_failed_rows(
                    half, half_error, table_group, item, outcome
                )
        return failed

    async def _isolate_failed_rows(
        self,
        item: PendingMessage,
        table_group: str,
        error: Exception,
        outcome: _FlushOutcome,
    ) -> None:
        """Insert the good rows of a failing message and record only the bad ones.

        The publisher merges rows from many requests into one message, so moving
        the whole message to the failed queue would park other clients' valid
        events with the one bad row. Bisecting costs O(bad rows * log rows)
        inserts. The good rows are inserted before the bad ones are parked,
        because only an insert tells them apart. So if the insert fails for
        another reason part way through, or parking the bad rows fails (broker
        unavailable; bounded by the publish timeout), the message is requeued
        and the good rows are inserted again on redelivery. That needs a row
        failure plus a second failure in the same flush and is the same
        at-least-once trade-off as a lost ack; the retry repeats the same
        splits, so ClickHouse insert deduplication on replicated tables drops
        the repeats.
        """

        rows = item.payload.rows
        try:
            failed = await self._split_failed_rows(rows, error, table_group, item, outcome)
        except _IsolationStoppedError as stop:
            logger.error(
                "Insert failed while isolating failing rows, requeueing message",
                error=str(stop.error),
                error_type=type(stop.error).__name__,
                failure=stop.kind.value,
                table_group=table_group,
                rows_count=len(rows),
            )
            outcome.requeued.append((item, stop.kind))
            return

        if not failed:
            await self._ack_inserted([item])
            return

        kinds = {kind for _, _, kind in failed}
        outcome.rejected.append(
            _Rejected(
                item,
                failed[0][1],
                InsertFailure.UNKNOWN
                if InsertFailure.UNKNOWN in kinds
                else InsertFailure.DETERMINISTIC,
                rows=[row for row, _, _ in failed],
                driver_data_error=all(isinstance(error, DataError) for _, error, _ in failed),
            ),
        )

    async def _park(self, rejected: _Rejected, table_group: str) -> bool:
        """Move a refused message aside and ack it; return ``False`` if it was not moved."""

        rows_count = len(rejected.item.payload.rows)
        log_fields: dict[str, Any] = {
            "error": str(rejected.error),
            "error_type": type(rejected.error).__name__,
            "failure": rejected.kind.value,
            "table_group": table_group,
            "rows_count": rows_count,
            "failed_rows_count": rows_count if rejected.rows is None else len(rejected.rows),
            "failed_queue_name": self.failed_queue_name,
        }
        if rejected.kind is InsertFailure.UNKNOWN or rejected.attempt_limit:
            log_fields["insert_attempts"] = self._attempts.failures(rejected.item.message) + 1
            logger.warning(
                "Rows kept failing while other inserts succeeded, moving them to failed queue",
                max_insert_attempts=self._attempts.limit,
                **log_fields,
            )
        else:
            logger.warning(
                "Rows were refused by ClickHouse, moving them to failed queue", **log_fields
            )

        try:
            await self._publish_failed_message(rejected, table_group)
        except Exception as publish_exc:
            logger.error(
                "Failed to move rows to failed queue, requeueing",
                **{
                    **log_fields,
                    "error": str(publish_exc),
                    "error_type": type(publish_exc).__name__,
                },
            )
            return False
        await rejected.item.message.ack()
        self._attempts.forget(rejected.item.message)
        return True

    async def _settle_flush(self, table_group: str, outcome: _FlushOutcome) -> bool:
        """Park or requeue what the flush could not insert; return whether all of it left the queue.

        A refused message is parked when some other insert of the same flush
        went through: a failure that also hits every other insert is the
        table's or the code's, not the rows', and parking for it would move the
        whole queue aside. The driver's DataError is parked regardless, since
        the driver raises it for a specific value before anything is sent. A
        message that reaches ``max_insert_attempts`` is parked when any insert
        of this worker, in any table group, succeeded since its previous
        failure: that covers a poison message flushed alone, or a table group
        the worker cannot insert at all, while still holding the queue during
        a failure that hits every insert. Every requeue that is not an outage
        counts toward the limit.
        """

        requeued = list(outcome.requeued)
        for rejected in outcome.rejected:
            if not (outcome.inserted or rejected.driver_data_error):
                if not self._attempts.may_park(rejected.item.message):
                    logger.warning(
                        "Insert failed and no other insert of the flush succeeded, requeueing",
                        error=str(rejected.error),
                        error_type=type(rejected.error).__name__,
                        failure=rejected.kind.value,
                        table_group=table_group,
                    )
                    requeued.append((rejected.item, rejected.kind))
                    continue
                rejected = replace(rejected, attempt_limit=True)
            if not await self._park(rejected, table_group):
                requeued.append((rejected.item, rejected.kind))

        for item, kind in requeued:
            if kind is not InsertFailure.SYSTEMIC:
                self._attempts.record_failure(item.message)
            await item.message.nack(requeue=True)
        return not requeued

    async def flush_table_group(self, table_group: str) -> None:
        """Flush a single table group and ack, park or requeue source messages.

        Updates the per-table-group consecutive-failure count that drives the
        capped exponential backoff applied between run-loop iterations: the
        count is reset when every message left the main queue (inserted or
        parked) and incremented when any was requeued.
        """

        pending = self.pending_batches.pop(table_group, [])
        self.pending_row_counts.pop(table_group, 0)
        if not pending:
            return
        outcome = _FlushOutcome()
        await self._flush_pending_messages(table_group, pending, outcome)
        succeeded = await self._settle_flush(table_group, outcome)
        if succeeded:
            self.failure_counts.pop(table_group, None)
            self._backoff_applied_counts.pop(table_group, None)
        else:
            self.failure_counts[table_group] += 1

    async def close(self) -> None:
        """Close RabbitMQ resources."""

        await self.channel.close()
        await self.connection.close()
