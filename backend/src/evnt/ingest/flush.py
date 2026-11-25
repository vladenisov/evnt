"""Flush side of the RabbitMQ batch worker: insert, split, isolate rows and park.

Holds the flush, split, row-isolation and parking logic of
``RabbitMQBatchWorker`` as ``_BatchFlusher``, with the per-flush bookkeeping
types it uses. The run loop, backoff and liveness live in
``evnt.ingest.worker``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Any

import structlog
from aio_pika import DeliveryMode, Message
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage
from aio_pika.exceptions import DeliveryError
from aiormq.abc import DeliveredMessage
from clickhouse_connect.driver.exceptions import DataError
from pydantic import ValidationError

from evnt.config import RabbitMQConfig
from evnt.ingest.insert_failures import (
    InsertFailure,
    _AttemptTracker,
    classify_insert_failure,
)
from evnt.ingest.rabbitmq import QueuedInsertPayload, _encode_queued_insert_payload
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
# ``x-evnt-failure`` of a parked message whose body is not a queued insert.
_UNDECODABLE_FAILURE = "undecodable"
# Backoff key for failures to park such a message; not a table group name.
_UNDECODABLE_BACKOFF_KEY = "<undecodable>"
# ``x-evnt-error`` and the logged error are cut to this many UTF-8 bytes. The
# headers of a message must fit one frame (``frame_max``, 131072 by default):
# the broker closes the connection on a larger one, and an error text grows
# with the payload (pydantic reports every bad element).
_MAX_ERROR_TEXT_BYTES = 2048
_ERROR_TEXT_TRUNCATED = "... [truncated]"
# A validation error with more entries than this is summarized by its count,
# so the full report (one entry per bad element) is never built.
_MAX_VALIDATION_ERRORS_SHOWN = 8


def _error_text(error: Exception) -> str:
    """Return a description of ``error`` short enough for a header or a log field."""

    if isinstance(error, ValidationError) and error.error_count() > _MAX_VALIDATION_ERRORS_SHOWN:
        text = f"{error.error_count()} validation errors for {error.title}"
    else:
        text = str(error) or repr(error)
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= _MAX_ERROR_TEXT_BYTES:
        return text
    kept = encoded[: _MAX_ERROR_TEXT_BYTES - len(_ERROR_TEXT_TRUNCATED)]
    return kept.decode("utf-8", "ignore") + _ERROR_TEXT_TRUNCATED


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


class _BatchFlusher:
    """Flushes buffered messages to the sink and settles each one with the broker.

    The state is owned and initialized by ``RabbitMQBatchWorker``.
    """

    channel: AbstractChannel
    sink: RowSink
    config: RabbitMQConfig
    failed_queue_name: str
    insert_timeout: float
    pending_batches: dict[str, list[PendingMessage]]
    pending_row_counts: dict[str, int]
    failure_counts: dict[str, int]
    _backoff_applied_counts: dict[str, int]
    _attempts: _AttemptTracker
    _busy_since: float | None

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
        headers["x-evnt-error"] = _error_text(error)
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
        await self._publish_to_failed_queue(item.message, body, headers)

    async def _publish_to_failed_queue(
        self,
        message: AbstractIncomingMessage,
        body: bytes,
        headers: dict[str, Any],
    ) -> None:
        async with asyncio.timeout(self.config.publish_timeout_seconds):
            result = await self.channel.default_exchange.publish(
                Message(
                    body=body,
                    headers=headers,
                    content_type=getattr(message, "content_type", None) or "application/json",
                    delivery_mode=DeliveryMode.PERSISTENT,
                    type=getattr(message, "type", None) or "evnt.insert.failed",
                ),
                routing_key=self.failed_queue_name,
                mandatory=True,
            )
        # The worker channel raises on a returned (unroutable) publish; a
        # channel opened without ``on_return_raises`` hands the message back.
        if isinstance(result, DeliveredMessage):
            raise DeliveryError(result, result.delivery)

    async def _park_undecodable(
        self,
        message: AbstractIncomingMessage,
        error: Exception,
    ) -> None:
        """Move a message whose body is not a queued insert aside, unchanged.

        The queue has no dead-letter exchange, so rejecting it would drop it.
        If it cannot be moved it is requeued, and the failure backs the loop
        off like a failed flush so the redelivery does not spin.
        """

        headers = dict(getattr(message, "headers", {}) or {})
        headers["x-evnt-source-queue"] = self.config.queue_name
        headers["x-evnt-error"] = _error_text(error)
        headers["x-evnt-error-type"] = type(error).__name__
        headers["x-evnt-failure"] = _UNDECODABLE_FAILURE
        log_fields: dict[str, Any] = {
            "failure": _UNDECODABLE_FAILURE,
            "body_bytes": len(message.body),
            "failed_queue_name": self.failed_queue_name,
        }
        logger.error(
            "Queue payload could not be decoded, moving it to failed queue",
            error=_error_text(error),
            error_type=type(error).__name__,
            **log_fields,
        )
        try:
            await self._publish_to_failed_queue(message, message.body, headers)
        except Exception as publish_exc:
            logger.error(
                "Failed to move undecodable payload to failed queue, requeueing",
                error=_error_text(publish_exc),
                error_type=type(publish_exc).__name__,
                **log_fields,
            )
            self.failure_counts[_UNDECODABLE_BACKOFF_KEY] += 1
            await message.nack(requeue=True)
            return
        await message.ack()
        self.failure_counts.pop(_UNDECODABLE_BACKOFF_KEY, None)
        self._backoff_applied_counts.pop(_UNDECODABLE_BACKOFF_KEY, None)

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
                    error=_error_text(error),
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
                error=_error_text(error),
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
                error=_error_text(stop.error),
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
            "error": _error_text(rejected.error),
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
                    "error": _error_text(publish_exc),
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
                        error=_error_text(rejected.error),
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
