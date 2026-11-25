"""RabbitMQ ingest: the publisher and the shared connection helpers.

Holds ``RabbitMQPublisher``, the queued-insert payload format, and the
connect/startup-retry helpers the publisher and the batch worker share. The
worker lives in ``evnt.ingest.worker``, its flush and failed-queue half in
``evnt.ingest.flush``; insert-failure classification lives in
``evnt.ingest.insert_failures``.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aio_pika import DeliveryMode, Message, connect_robust
from aio_pika.abc import (
    AbstractChannel,
    AbstractRobustConnection,
)
from aio_pika.exceptions import (
    AMQPError,
    AuthenticationError,
    ChannelInvalidStateError,
    ProbableAuthenticationError,
)
from pydantic import BaseModel, Field

from evnt.config import RabbitMQConfig
from evnt.exceptions import IngestUnavailableError
from evnt.ingest.coalesce import RowCoalescer

logger = structlog.get_logger(__name__)

# Milliseconds per second — used when converting ms config values to seconds.
_MS_PER_SECOND = 1000
# During a broker alarm every request fails the same way; one line per interval
# (with a count of the rest) is enough to see it.
_UNAVAILABLE_LOG_INTERVAL_SECONDS = 10.0
# Publish failures that mean "the broker did not store it, retry later": no
# confirm in time, a closed channel or connection (AMQPError covers the
# connection errors, nacks and unroutable returns), or a socket error.
_PUBLISH_UNAVAILABLE_ERRORS = (
    TimeoutError,
    AMQPError,
    ChannelInvalidStateError,
    ConnectionError,
)

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
    kind = "distributed" if cluster_name else "local"
    return str(tables[table_group][kind]["name"])


class QueuedInsertPayload(BaseModel):
    """Serialized payload stored in RabbitMQ."""

    table_group: str = "evnt"
    rows: list[dict[str, Any]] = Field(default_factory=list)


def _encode_queued_insert_payload(
    table_group: str,
    rows: list[dict[str, Any]],
) -> bytes:
    """Serialize a queued insert without copying the rows first.

    ``model_construct`` skips validation, which for ``list[dict[str, Any]]``
    would only rebuild the list and every row dict. Rows must therefore already
    hold values pydantic can serialize: JSON scalars, ``UUID``, ``datetime``,
    IP addresses and plain containers of those. Anything else raises
    ``PydanticSerializationError`` rather than being coerced.
    """

    payload = QueuedInsertPayload.model_construct(
        table_group=table_group,
        rows=rows,
    )
    return payload.model_dump_json().encode("utf-8")


class _RateLimitedLog:
    """Lets one log line through per interval and counts the ones it held back."""

    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = interval_seconds
        self._next_at = 0.0
        self._suppressed = 0

    def take(self) -> int | None:
        """Return how many were suppressed if this one may log, else ``None``."""

        now = time.monotonic()
        if now < self._next_at:
            self._suppressed += 1
            return None
        self._next_at = now + self.interval_seconds
        suppressed, self._suppressed = self._suppressed, 0
        return suppressed


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
        # Bounds unconfirmed publishes, so a broker that stops confirming holds at
        # most this many request bodies instead of every request that arrives.
        self._inflight = asyncio.Semaphore(config.max_inflight_publishes)
        self._retry_after_seconds = max(1, math.ceil(config.publish_timeout_seconds))
        self._unavailable_log = _RateLimitedLog(_UNAVAILABLE_LOG_INTERVAL_SECONDS)
        self._coalescer = RowCoalescer(
            self._publish_rows,
            window_seconds=config.coalesce_window_ms / _MS_PER_SECOND,
            max_rows=config.coalesce_max_rows,
        )

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
                # on_return_raises: a mandatory publish the broker cannot route
                # (the queue was deleted) fails instead of confirming normally,
                # so the request is retried rather than answered 204 and lost.
                channel = await connection.channel(
                    publisher_confirms=True,
                    on_return_raises=True,
                )
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
        """Queue rows in RabbitMQ, returning once the broker confirmed them.

        Rows from requests arriving within ``coalesce_window_ms`` of each other
        travel in one persistent message (see ``RowCoalescer``).
        """

        if not rows:
            logger.debug("No rows to enqueue", table_group=table_group)
            return

        await self._coalescer.add(table_group, rows)

    async def _publish_rows(self, table_group: str, rows: list[dict[str, Any]]) -> None:
        """Publish one message holding ``rows`` and wait for its confirm."""

        # One deadline covers waiting for a window slot and the confirm. Under a
        # broker memory or disk alarm confirms stop entirely; the request then fails
        # fast with a 503 the tracker retries, instead of waiting out the alarm. A
        # closed channel or connection (broker restart, reconnect in progress), a
        # nack and an unroutable return are the same "not stored, retry later". A
        # publish cut off after it was written may still be stored, so a retry can
        # duplicate it; events carry ``eid`` for deduplication.
        #
        # The body is encoded only once a slot is held: while confirms stall,
        # publishes waiting for a slot then keep no encoded copy of their rows,
        # so ``max_inflight_publishes`` bounds the encoded bodies in memory.
        try:
            async with asyncio.timeout(self.config.publish_timeout_seconds):
                async with self._inflight:
                    message = Message(
                        body=_encode_queued_insert_payload(table_group, rows),
                        content_type="application/json",
                        delivery_mode=DeliveryMode.PERSISTENT,
                        type="evnt.insert",
                    )
                    await self.channel.default_exchange.publish(
                        message,
                        routing_key=self.config.queue_name,
                        mandatory=True,
                    )
        except _PUBLISH_UNAVAILABLE_ERRORS as exc:
            suppressed = self._unavailable_log.take()
            if suppressed is not None:
                logger.warning(
                    "RabbitMQ did not take the publish; answering 503",
                    error=repr(exc),
                    queue_name=self.config.queue_name,
                    publish_timeout_seconds=self.config.publish_timeout_seconds,
                    max_inflight_publishes=self.config.max_inflight_publishes,
                    suppressed_since_last_log=suppressed,
                )
            raise IngestUnavailableError(
                "RabbitMQ did not take the publish",
                {"queue_name": self.config.queue_name},
                retry_after_seconds=self._retry_after_seconds,
            ) from exc
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
        """Publish what is still buffered, then close RabbitMQ resources."""

        await self._coalescer.close()
        await self.channel.close()
        await self.connection.close()
