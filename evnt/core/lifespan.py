"""
Application lifespan management for evnt.

Handles startup and shutdown events, including database connection
pooling and resource cleanup.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
import structlog
from clickhouse_connect import get_async_client
from fastapi import FastAPI
from routers.tracker.parsers.iglu import warm_iglu_schema_cache

from .config import ClickHouseConfig, ProxyConfig, settings
from .constants import DEFAULT_PROXY_TIMEOUT
from .exceptions import DatabaseConnectionError
from .healthcheck import CachedHealthChecker, ClickHouseHealthChecker
from .protocols import HealthChecker

logger = structlog.get_logger(__name__)

# Seconds a keepalive connection may remain idle before being closed.
# 30 s is a pragmatic default that sits well below most reverse-proxy idle
# timeouts (typically 60–120 s) while avoiding aggressive churn.
_KEEPALIVE_EXPIRY_SECONDS: float = 30.0

# SQL used to verify ClickHouse connectivity at startup.
_CLICKHOUSE_READINESS_QUERY: str = "SELECT 1"

PERFORMANCE_CONFIG = settings.performance
CLICKHOUSE_CONFIG = settings.clickhouse
INGEST_CONFIG = settings.ingest
PROXY_CONFIG = settings.proxy


def _cache_health_checker(checker: HealthChecker) -> CachedHealthChecker:
    """Apply configured TTL caching to backend health checks."""

    return CachedHealthChecker(
        checker,
        ttl_seconds=PERFORMANCE_CONFIG.healthcheck_cache_ttl_seconds,
    )


def _build_clickhouse_insert_settings() -> dict[str, int]:
    """Build ClickHouse insert settings for direct writes."""

    if not INGEST_CONFIG.direct.async_insert:
        return {}

    return {
        "async_insert": 1,
        "wait_for_async_insert": int(INGEST_CONFIG.direct.wait_for_async_insert),
    }


async def _create_clickhouse_client():
    """Create the shared ClickHouse client."""

    return await get_async_client(
        **CLICKHOUSE_CONFIG.connection.as_client_kwargs(),
        query_limit=0,
        connector_limit=PERFORMANCE_CONFIG.db_pool_size,
        connector_limit_per_host=PERFORMANCE_CONFIG.db_pool_size,
    )


async def _create_ready_clickhouse_client():
    """Create a ClickHouse client and verify the server is ready."""

    client = await _create_clickhouse_client()
    try:
        query = await client.query(_CLICKHOUSE_READINESS_QUERY)
        if query.first_row[0] != 1:
            raise RuntimeError("ClickHouse readiness query returned unexpected result")
        return client
    except Exception:
        await client.close()
        raise


async def retry_clickhouse_startup[T](
    config: ClickHouseConfig,
    operation: str,
    callback: Callable[[], Awaitable[T]],
) -> T:
    """Retry ClickHouse startup operations until the configured wait expires."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.startup_timeout_seconds
    attempt = 0

    while True:
        attempt += 1
        try:
            return await callback()
        except Exception as exc:
            remaining_seconds = deadline - loop.time()
            if remaining_seconds <= 0:
                logger.error(
                    "ClickHouse startup wait expired",
                    operation=operation,
                    attempt=attempt,
                    host=config.connection.host,
                    port=config.connection.port,
                    startup_timeout_seconds=config.startup_timeout_seconds,
                    error=str(exc),
                )
                raise

            retry_in_seconds = min(
                config.startup_retry_interval_ms / 1000,
                remaining_seconds,
            )
            logger.warning(
                "ClickHouse is not ready yet, retrying",
                operation=operation,
                attempt=attempt,
                host=config.connection.host,
                port=config.connection.port,
                retry_in_seconds=retry_in_seconds,
                remaining_seconds=remaining_seconds,
                error=str(exc),
            )
            await asyncio.sleep(retry_in_seconds)


async def _configure_direct_ingest(application: FastAPI) -> None:
    """Initialize direct ClickHouse ingest."""

    from routers.tracker.db.clickhouse import ClickHouseConnector  # noqa: PLC0415

    application.state.ch_client = await retry_clickhouse_startup(
        CLICKHOUSE_CONFIG,
        "direct_ingest_create",
        _create_ready_clickhouse_client,
    )
    application.state.connector = ClickHouseConnector(
        application.state.ch_client,
        insert_settings=_build_clickhouse_insert_settings(),
        **CLICKHOUSE_CONFIG.configuration.model_dump(),
    )
    application.state.health_checker = _cache_health_checker(
        ClickHouseHealthChecker(application.state.ch_client),
    )
    application.state._closeables.append(application.state.ch_client)


async def _configure_rabbitmq_ingest(application: FastAPI) -> None:
    """Initialize RabbitMQ-backed ingest."""

    from ingest import RabbitMQHealthChecker, RabbitMQPublisher  # noqa: PLC0415

    rabbitmq_config = INGEST_CONFIG.rabbitmq
    clickhouse_config = CLICKHOUSE_CONFIG.configuration

    application.state.ch_client = None
    application.state.connector = await RabbitMQPublisher.create(
        config=rabbitmq_config,
        tables=clickhouse_config.tables,
        database=clickhouse_config.database,
        cluster_name=clickhouse_config.cluster_name,
    )
    application.state.health_checker = _cache_health_checker(
        RabbitMQHealthChecker(
            application.state.connector.channel,
            rabbitmq_config.queue_name,
            rabbitmq_config.resolved_failed_queue_name,
        ),
    )
    application.state._closeables.append(application.state.connector)


async def _configure_proxy_http_client(
    application: FastAPI,
    config: ProxyConfig = PROXY_CONFIG,
) -> None:
    """Create the shared outbound HTTP client used by the proxy route."""

    limits = httpx.Limits(
        max_connections=PERFORMANCE_CONFIG.max_concurrent_connections,
        max_keepalive_connections=PERFORMANCE_CONFIG.max_concurrent_connections,
        keepalive_expiry=_KEEPALIVE_EXPIRY_SECONDS,
    )
    application.state.proxy_http_client = httpx.AsyncClient(
        # The allowlist (proxy_allowed_hosts) is only validated against the
        # initial request URL. Auto-following redirects would let a permitted
        # host bounce the request to an arbitrary internal target (SSRF), so
        # redirects must not be followed automatically.
        follow_redirects=False,
        timeout=DEFAULT_PROXY_TIMEOUT,
        limits=limits,
    )
    application.state.proxy_allowed_hosts = frozenset(
        domain.rstrip(".").lower() for domain in config.domains
    )
    application.state.proxy_allowed_ports = frozenset(config.allowed_ports)
    application.state._closeables.append(application.state.proxy_http_client)


async def _close_lifespan_resources(application: FastAPI) -> None:
    """Close resources registered during application lifespan startup."""

    for resource in reversed(application.state._closeables):
        try:
            await resource.close()
        except Exception as e:
            logger.warning("Error closing resource", error=str(e))


def warm_known_iglu_schemas() -> None:
    """Preload supported Iglu validators into memory during startup."""

    results = warm_iglu_schema_cache()
    loaded_count = 0
    warning_count = 0
    skipped_count = 0

    for schema_uri, result in results.items():
        if result.status == "ok":
            loaded_count += 1
            continue
        if result.status == "warning":
            warning_count += 1
            logger.warning(
                "Failed to warm Iglu schema cache",
                schema=schema_uri,
                schema_path=str(result.schema_path) if result.schema_path else None,
                error=result.error,
            )
            continue
        skipped_count += 1

    logger.info(
        "Iglu schema cache warmed",
        loaded_count=loaded_count,
        warning_count=warning_count,
        skipped_count=skipped_count,
    )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None]:
    """
    Manage application lifecycle.

    Initializes database connections on startup and cleans up on shutdown.

    Args:
        application: The FastAPI application instance

    Yields:
        None during application runtime

    Raises:
        DatabaseConnectionError: If database connection fails
    """
    backend_host = (
        INGEST_CONFIG.rabbitmq.host
        if INGEST_CONFIG.mode == "rabbitmq"
        else CLICKHOUSE_CONFIG.connection.host
    )

    application.state._closeables = []
    application.state.ingest_mode = INGEST_CONFIG.mode

    logger.info(
        "Starting application",
        ingest_mode=INGEST_CONFIG.mode,
        backend_host=backend_host,
    )

    try:
        if INGEST_CONFIG.mode == "rabbitmq":
            await _configure_rabbitmq_ingest(application)
        else:
            await _configure_direct_ingest(application)

        await _configure_proxy_http_client(application)
        # warm_known_iglu_schemas performs synchronous file I/O; run it in a
        # thread so it does not block the event loop during async startup.
        await asyncio.to_thread(warm_known_iglu_schemas)
        logger.info("Ingest backend initialized")

    except Exception as e:
        logger.error("Failed to initialize ingest backend", error=str(e))
        await _close_lifespan_resources(application)
        raise DatabaseConnectionError(
            "Failed to initialize ingest backend",
            {
                "mode": INGEST_CONFIG.mode,
                "host": backend_host,
                "error": str(e),
            },
        ) from e

    yield

    # Cleanup on shutdown
    logger.info("Shutting down application")

    await _close_lifespan_resources(application)
