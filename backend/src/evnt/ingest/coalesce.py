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
    waiters: list[asyncio.Future[None]] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None


class RowCoalescer:
    """Collect rows per table group and hand each batch to ``flush`` once.

    A batch goes out when it holds ``max_rows`` rows or ``window_seconds`` after
    its first rows arrived, whichever comes first. Every caller waits for the
    batch its rows went into and gets that flush's outcome: ``None`` once it
    succeeded, or the exception it raised. With ``window_seconds == 0`` each call
    is flushed on its own, which is the behaviour without merging.
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
            await self._flush(table_group, buffer.rows)
        except asyncio.CancelledError:
            # The flush was cancelled (loop teardown), not the callers: they get
            # a retryable error rather than a cancellation and no response.
            for waiter in buffer.waiters:
                if not waiter.done():
                    waiter.set_exception(
                        IngestUnavailableError("the publish was cancelled at shutdown"),
                    )
            raise
        except Exception as exc:
            for waiter in buffer.waiters:
                if not waiter.done():
                    waiter.set_exception(exc)
        else:
            for waiter in buffer.waiters:
                if not waiter.done():
                    waiter.set_result(None)

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
