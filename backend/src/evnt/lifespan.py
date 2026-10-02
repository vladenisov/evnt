"""Application startup and shutdown.

Startup resolves the encryption keyring, connects the active ingest backend
(ClickHouse directly, or RabbitMQ), builds the proxy HTTP client and warms the
Iglu validator cache. Every resource that needs closing registers its close
call on an ``AsyncExitStack``, which runs them in reverse order on shutdown, or
as soon as a later startup step fails.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx
import structlog
from fastapi import FastAPI

from evnt.concurrency import run_cpu_task
from evnt.config import settings
from evnt.constants import DEFAULT_PROXY_TIMEOUT
from evnt.exceptions import DatabaseConnectionError
from evnt.health import CachedHealthChecker, ClickHouseHealthChecker
from evnt.protocols import HealthChecker
from evnt.storage.clickhouse import client as clickhouse
from evnt.tracker.iglu import warm_iglu_schema_cache

logger = structlog.get_logger(__name__)

# Seconds a keepalive connection may remain idle before being closed. Well
# below most reverse-proxy idle timeouts (60-120 s) without aggressive churn.
_KEEPALIVE_EXPIRY_SECONDS = 30.0


def _close_on_exit(stack: AsyncExitStack, name: str, close: Callable[[], Awaitable[Any]]) -> None:
    """Register ``close`` to run on shutdown; a failure is logged, not raised.

    Logging keeps one resource that fails to close from leaving the others
    open, and from masking the exception that triggered the unwind.
    """

    async def _close() -> None:
        try:
            await close()
        except Exception as exc:
            logger.warning("Error closing resource", resource=name, error=str(exc))

    stack.push_async_callback(_close)


def _cache_health_checker(checker: HealthChecker) -> CachedHealthChecker:
    return CachedHealthChecker(
        checker,
        ttl_seconds=settings.performance.healthcheck_cache_ttl_seconds,
    )


async def _configure_direct_ingest(app: FastAPI, stack: AsyncExitStack) -> None:
    """Write events straight to ClickHouse."""
    from evnt.storage.clickhouse import ClickHouseConnector  # noqa: PLC0415

    ch_config = settings.clickhouse
    client = await clickhouse.connect(
        ch_config,
        settings.performance.db_pool_size,
        "direct_ingest_create",
    )
    _close_on_exit(stack, "clickhouse", client.close)
    app.state.connector = ClickHouseConnector(
        client,
        insert_settings=clickhouse.insert_settings(settings.ingest.direct),
        **ch_config.configuration.model_dump(),
    )
    app.state.health_checker = _cache_health_checker(ClickHouseHealthChecker(client))


async def _configure_rabbitmq_ingest(app: FastAPI, stack: AsyncExitStack) -> None:
    """Publish events to RabbitMQ; ``evnt queue worker`` writes them to ClickHouse."""
    from evnt.ingest import RabbitMQHealthChecker, RabbitMQPublisher  # noqa: PLC0415

    rabbitmq_config = settings.ingest.rabbitmq
    tables_config = settings.clickhouse.configuration

    publisher = await RabbitMQPublisher.create(
        config=rabbitmq_config,
        tables=tables_config.tables,
        database=tables_config.database,
        cluster_name=tables_config.cluster_name,
    )
    _close_on_exit(stack, "rabbitmq", publisher.close)
    app.state.connector = publisher
    app.state.health_checker = _cache_health_checker(
        RabbitMQHealthChecker(
            publisher.channel,
            rabbitmq_config.queue_name,
            rabbitmq_config.resolved_failed_queue_name,
        ),
    )


def _configure_proxy_http_client(app: FastAPI, stack: AsyncExitStack) -> None:
    """Create the shared outbound HTTP client used by the proxy route."""
    max_connections = settings.performance.max_concurrent_connections
    client = httpx.AsyncClient(
        # The host allowlist is only checked against the initial request URL.
        # Following redirects would let an allowed host bounce the request to
        # an arbitrary internal target (SSRF), so they are never followed.
        follow_redirects=False,
        timeout=DEFAULT_PROXY_TIMEOUT,
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
            keepalive_expiry=_KEEPALIVE_EXPIRY_SECONDS,
        ),
    )
    _close_on_exit(stack, "proxy_http_client", client.aclose)
    app.state.proxy_http_client = client
    app.state.proxy_allowed_hosts = frozenset(
        domain.rstrip(".").lower() for domain in settings.proxy.domains
    )
    app.state.proxy_allowed_ports = frozenset(settings.proxy.allowed_ports)


def _configure_encryption(app: FastAPI) -> None:
    """Resolve decryption keys before any traffic arrives.

    Loading here rather than per request means bad key material fails startup
    loudly instead of degrading into a stream of opaque 400s.
    """
    config = settings.encryption
    if not config.enabled:
        app.state.keyring = None
        return

    from evnt.crypto import Keyring  # noqa: PLC0415 - optional `crypto` extra

    keyring = Keyring.from_config(config)
    app.state.keyring = keyring
    logger.info(
        "Encrypted ingest enabled",
        endpoint=config.endpoint,
        key_ids=list(keyring.key_ids),
    )


def warm_known_iglu_schemas() -> None:
    """Preload the validators for the schemas trackers send most."""
    results = warm_iglu_schema_cache()
    counts = {"ok": 0, "warning": 0, "skipped": 0}

    for schema_uri, result in results.items():
        counts[result.status] = counts.get(result.status, 0) + 1
        if result.status == "warning":
            logger.warning(
                "Failed to warm Iglu schema cache",
                schema=schema_uri,
                schema_path=str(result.schema_path) if result.schema_path else None,
                error=result.error,
            )

    logger.info(
        "Iglu schema cache warmed",
        loaded_count=counts["ok"],
        warning_count=counts["warning"],
        skipped_count=counts["skipped"],
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the ingest backend for the lifetime of the app.

    Raises:
        DatabaseConnectionError: when the ingest backend cannot be initialized.
    """
    mode = settings.ingest.mode
    backend_host = (
        settings.ingest.rabbitmq.host if mode == "rabbitmq" else settings.clickhouse.connection.host
    )

    app.state.ingest_mode = mode
    # Outside the try below so a bad key is reported as a config error, not as
    # a backend failure.
    _configure_encryption(app)

    logger.info("Starting application", ingest_mode=mode, backend_host=backend_host)
    async with AsyncExitStack() as stack:
        try:
            if mode == "rabbitmq":
                await _configure_rabbitmq_ingest(app, stack)
            else:
                await _configure_direct_ingest(app, stack)
            _configure_proxy_http_client(app, stack)
            # Synchronous file I/O: keep it off the event loop.
            await run_cpu_task(warm_known_iglu_schemas)
        except Exception as exc:
            logger.error("Failed to initialize ingest backend", error=str(exc))
            raise DatabaseConnectionError(
                "Failed to initialize ingest backend",
                {"mode": mode, "host": backend_host, "error": str(exc)},
            ) from exc

        logger.info("Ingest backend initialized")
        try:
            yield
        finally:
            logger.info("Shutting down application")
