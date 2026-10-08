"""ClickHouse client construction shared by the app, the worker and the CLI."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from clickhouse_connect import get_async_client
from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.httputil import get_pool_manager

from evnt.config import ClickHouseConfig, DirectInsertConfig

logger = structlog.get_logger(__name__)

READINESS_QUERY = "SELECT 1"


def insert_settings(config: DirectInsertConfig, *, require_wait: bool = False) -> dict[str, int]:
    """ClickHouse settings applied to every insert.

    ``require_wait`` forces ``wait_for_async_insert``: the RabbitMQ worker acks
    a batch only after the insert returns, so it must not ack rows ClickHouse
    has merely buffered.
    """
    if not config.async_insert:
        # Override a server/user default too: leaving this unset could make
        # the worker acknowledge an insert that ClickHouse only buffered.
        return {"async_insert": 0}
    return {
        "async_insert": 1,
        "wait_for_async_insert": int(config.wait_for_async_insert or require_wait),
    }


async def create_client(config: ClickHouseConfig, pool_size: int) -> AsyncClient:
    """Open a pooled async client without checking the server.

    clickhouse-connect's async client wraps the sync HTTP client in a thread
    pool, so the connection pool is sized through urllib3's ``maxsize``.
    """
    client: AsyncClient = await get_async_client(
        **config.connection.as_client_kwargs(),
        query_limit=0,
        pool_mgr=get_pool_manager(maxsize=pool_size),
    )
    return client


async def is_ready(client: AsyncClient) -> bool:
    """Run the readiness query; any exception propagates to the caller."""
    result = await client.query(READINESS_QUERY)
    return bool(result.first_row[0] == 1)


async def create_ready_client(config: ClickHouseConfig, pool_size: int) -> AsyncClient:
    """Open a client and prove the server answers queries before returning it."""
    client = await create_client(config, pool_size)
    try:
        if not await is_ready(client):
            raise RuntimeError("ClickHouse readiness query returned an unexpected result")
    except BaseException:
        await client.close()
        raise
    return client


async def retry_startup[T](
    config: ClickHouseConfig,
    operation: str,
    callback: Callable[[], Awaitable[T]],
) -> T:
    """Retry ``callback`` until it succeeds or ``startup_timeout_seconds`` runs out.

    Compose and Kubernetes start ClickHouse alongside the collector, so the
    first attempts routinely fail while it boots.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.startup_timeout_seconds
    attempt = 0
    log_context: dict[str, Any] = {
        "operation": operation,
        "host": config.connection.host,
        "port": config.connection.port,
    }

    while True:
        attempt += 1
        try:
            return await callback()
        except Exception as exc:
            remaining_seconds = deadline - loop.time()
            if remaining_seconds <= 0:
                logger.error(
                    "ClickHouse startup wait expired",
                    attempt=attempt,
                    startup_timeout_seconds=config.startup_timeout_seconds,
                    error=str(exc),
                    **log_context,
                )
                raise

            retry_in_seconds = min(config.startup_retry_interval_ms / 1000, remaining_seconds)
            logger.warning(
                "ClickHouse is not ready yet, retrying",
                attempt=attempt,
                retry_in_seconds=retry_in_seconds,
                remaining_seconds=remaining_seconds,
                error=str(exc),
                **log_context,
            )
            await asyncio.sleep(retry_in_seconds)


async def connect(config: ClickHouseConfig, pool_size: int, operation: str) -> AsyncClient:
    """Open a ready client, waiting out a ClickHouse that is still starting."""
    return await retry_startup(
        config,
        operation,
        lambda: create_ready_client(config, pool_size),
    )
