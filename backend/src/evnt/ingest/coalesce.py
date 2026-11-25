"""Merge rows from concurrent requests into fewer broker messages.

A RabbitMQ publish costs a few hundred microseconds of CPU in aio-pika, more than
processing the events of a typical request. Under load many requests publish at
the same moment, so collecting their rows for a few milliseconds and publishing
them as one message cuts that cost by the number of requests merged, while each
request still returns only after the broker confirmed the message holding its
rows.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from evnt.exceptions import IngestUnavailableError

Rows = list[dict[str, Any]]
FlushRows = Callable[[str, Rows], Awaitable[None]]


@dataclass(slots=True)
class _Buffer:
    rows: Rows = field(default_factory=list)
    # One entry per call, in arrival order: its rows and its waiter.
    calls: list[Rows] = field(default_factory=list)
    waiters: list[asyncio.Future[None]] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None


class RowCoalescer:
    """Collect rows per table group and hand each batch to ``flush`` once.

    A batch goes out when it holds ``max_rows`` rows or ``window_seconds`` after
    its first rows arrived, whichever comes first. Rows that would push a batch
    past ``max_rows`` start a new one, so a batch holds at most ``max_rows``
    rows or the rows of one call, whichever is more. Every caller waits for the
    batch its rows went into and gets that flush's outcome: ``None`` once it
    succeeded, or the exception it raised. With ``window_seconds == 0`` each call
    is flushed on its own, which is the behaviour without merging.

    ``IngestUnavailableError`` (the backend took nothing, retry later) is the
    outcome of every caller of the batch. Any other error may come from the
    rows of one caller, so the batch is then flushed again call by call and
    only the caller whose rows fail gets the error.
    """

    def __init__(self, flush: FlushRows, *, window_seconds: float, max_rows: int) -> None:
        self._flush = flush
        self.window_seconds = window_seconds
        self.max_rows = max_rows
        self._buffers: dict[str, _Buffer] = {}
        # Strong references, so a running flush is not garbage-collected.
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def add(self, table_group: str, rows: Rows) -> None:
        """Queue ``rows`` and wait until the batch holding them is flushed."""

        if self._closed:
            raise IngestUnavailableError("the publisher is shutting down")
        if self.window_seconds <= 0:
            await self._flush(table_group, rows)
            return

        loop = asyncio.get_running_loop()
        buffer = self._buffers.get(table_group)
        if buffer is not None and len(buffer.rows) + len(rows) > self.max_rows:
            # Merging must never make a message larger than ``max_rows``: send
            # what is buffered and start a new batch with these rows. A single
            # call larger than that still goes out alone, as without merging.
            self._flush_buffer(table_group)
            buffer = None
        if buffer is None:
            buffer = self._buffers[table_group] = _Buffer()
            # A fresh context: the flush serves many requests, so it must not
            # carry the first caller's request id or APM span into its logs.
            buffer.timer = loop.call_later(
                self.window_seconds,
                self._flush_buffer,
                table_group,
                context=contextvars.Context(),
            )

        waiter: asyncio.Future[None] = loop.create_future()
        buffer.rows.extend(rows)
        buffer.calls.append(rows)
        buffer.waiters.append(waiter)
        if len(buffer.rows) >= self.max_rows:
            self._flush_buffer(table_group)

        # A caller that goes away (client disconnect) cancels only its own wait:
        # its rows are already in the batch and are published with it.
        await waiter

    def _flush_buffer(self, table_group: str) -> None:
        buffer = self._buffers.pop(table_group, None)
        if buffer is None:
            return
        if buffer.timer is not None:
            buffer.timer.cancel()
        task = asyncio.get_running_loop().create_task(
            self._run(table_group, buffer),
            context=contextvars.Context(),
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, table_group: str, buffer: _Buffer) -> None:
        try:
            await self._flush_batch(table_group, buffer)
        except asyncio.CancelledError:
            # The flush was cancelled (loop teardown), not the callers: they get
            # a retryable error rather than a cancellation and no response.
            _answer(
                buffer.waiters,
                IngestUnavailableError("the publish was cancelled at shutdown"),
            )
            raise

    async def _flush_batch(self, table_group: str, buffer: _Buffer) -> None:
        try:
            await self._flush(table_group, buffer.rows)
        except IngestUnavailableError as exc:
            _answer(buffer.waiters, exc)
        except Exception as exc:
            if len(buffer.calls) > 1:
                await self._flush_call_by_call(table_group, buffer)
            else:
                _answer(buffer.waiters, exc)
        else:
            _answer(buffer.waiters, None)

    async def _flush_call_by_call(self, table_group: str, buffer: _Buffer) -> None:
        """Flush each call of a failed batch alone, in arrival order.

        The merged flush failed as a whole, so no call's rows went through yet
        and none is sent twice. The rows of a caller that went away are still
        flushed.
        """

        for index, rows in enumerate(buffer.calls):
            try:
                await self._flush(table_group, rows)
            except IngestUnavailableError as exc:
                # The backend stopped taking rows: the calls left would each
                # wait out the same failure, so they all get it now.
                _answer(buffer.waiters[index:], exc)
                return
            except Exception as exc:
                _answer(buffer.waiters[index : index + 1], exc)
            else:
                _answer(buffer.waiters[index : index + 1], None)

    async def close(self) -> None:
        """Flush what is buffered and wait for every flush still running.

        Rows offered after this answer ``IngestUnavailableError`` (503), so the
        tracker retries against another instance instead of publishing onto a
        closing channel.
        """

        self._closed = True
        for table_group in list(self._buffers):
            self._flush_buffer(table_group)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


def _answer(waiters: list[asyncio.Future[None]], exc: BaseException | None) -> None:
    """Give every waiter still waiting the outcome of its flush."""

    for waiter in waiters:
        if waiter.done():
            continue
        if exc is None:
            waiter.set_result(None)
        else:
            waiter.set_exception(exc)
