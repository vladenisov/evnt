"""Readiness check for RabbitMQ-backed ingest."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import structlog
from aio_pika.abc import AbstractRobustChannel, AbstractRobustConnection

logger = structlog.get_logger(__name__)


class RabbitMQHealthChecker:
    """Health checker for RabbitMQ-backed ingest.

    It runs on a connection and channel of its own:

    * On the publisher's robust channel a passive declare of a queue declared
      there at startup is answered from aio-pika's cache without a broker round
      trip, so a deleted queue went unnoticed. On a dedicated channel nothing is
      ever cached (``robust=False``), so every check asks the broker.
    * A passive declare that fails makes the broker close the channel, which
      must not take the publisher's in-flight confirms with it.
    * During a memory or disk alarm the broker stops reading publishing
      connections. Sharing the publisher's connection, the check's frames would
      queue behind stalled publishes and could hang ``/health`` until the alarm
      cleared; a connection that never publishes is not blocked.
    """

    def __init__(
        self,
        connection: AbstractRobustConnection,
        *queue_names: str,
        timeout_seconds: float,
    ) -> None:
        self.connection = connection
        self.queue_names = queue_names
        self.timeout_seconds = timeout_seconds
        self._channel: AbstractRobustChannel | None = None

    async def _get_channel(self) -> AbstractRobustChannel:
        """Return the check channel, reopening it after the broker closed it.

        Kept open between checks: opening a channel per check leaks an observer
        future on the connection with aio-pika 10, so only a closed channel is
        replaced.
        """

        if self._channel is not None and not self._channel.is_closed:
            return self._channel
        if self._channel is not None:
            # A robust channel restores itself after a reconnect unless it was
            # closed explicitly; closing it stops that, so the replacement does
            # not leave a second channel open on the broker.
            with suppress(Exception):
                await self._channel.close()
        self._channel = await self.connection.channel(publisher_confirms=False)
        return self._channel

    async def close(self) -> None:
        """Close the check connection (and with it the check channel)."""

        await self.connection.close()

    async def check(self) -> dict[str, bool]:
        """Check that every ingest queue exists on the broker."""

        healthy = True
        try:
            async with asyncio.timeout(self.timeout_seconds):
                channel = await self._get_channel()
                for queue_name in self.queue_names:
                    await channel.declare_queue(
                        queue_name,
                        durable=True,
                        passive=True,
                        robust=False,
                    )
        except Exception as exc:
            logger.warning(
                "RabbitMQ health check failed",
                error=str(exc),
                queue_names=self.queue_names,
            )
            healthy = False

        return {"rabbitmq": healthy}
