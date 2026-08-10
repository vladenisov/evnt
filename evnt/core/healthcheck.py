"""
Health check endpoint for evnt.

Provides health probes for the active ingest backend.
"""

import asyncio
from collections.abc import Callable

import structlog
from fastapi import Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.status import HTTP_200_OK, HTTP_204_NO_CONTENT, HTTP_502_BAD_GATEWAY

from .protocols import HealthChecker

logger = structlog.get_logger(__name__)

# SQL used to verify ClickHouse connectivity during health checks.
_CLICKHOUSE_HEALTH_QUERY: str = "SELECT 1"


class HealthProbeResponse(BaseModel):
    """JSON shape returned by the health probe endpoint."""

    status: dict[str, bool]
    healthy: bool
    ingest_mode: str
    table: str | None = None


class CachedHealthChecker:
    """TTL cache for backend health checks."""

    def __init__(
        self,
        checker: HealthChecker,
        ttl_seconds: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.checker = checker
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._lock = asyncio.Lock()
        self._cached_status: dict[str, bool] | None = None
        self._expires_at = 0.0

    async def check(self) -> dict[str, bool]:
        """Return cached health status while the TTL is still valid."""

        if self.ttl_seconds <= 0:
            return await self.checker.check()

        now = self._now()
        if self._cached_status is not None and now < self._expires_at:
            return self._cached_status.copy()

        async with self._lock:
            now = self._now()
            if self._cached_status is not None and now < self._expires_at:
                return self._cached_status.copy()

            status = await self.checker.check()
            self._cached_status = status.copy()
            self._expires_at = self._now() + self.ttl_seconds
            return status.copy()

    def _now(self) -> float:
        """Return monotonic time for cache expiry."""

        if self.clock is not None:
            return self.clock()
        return asyncio.get_running_loop().time()


class ClickHouseHealthChecker:
    """Health checker for direct ClickHouse ingest."""

    def __init__(self, client) -> None:
        self.client = client

    async def check(self) -> dict[str, bool]:
        """Check ClickHouse connectivity."""

        healthy = True
        try:
            query = await self.client.query(_CLICKHOUSE_HEALTH_QUERY)
            healthy = query.first_row[0] == 1
        except Exception as exc:
            logger.warning("ClickHouse health check failed", error=str(exc))
            healthy = False

        return {"clickhouse": healthy}


async def liveness() -> Response:
    """Report that the application event loop is responsive."""

    return Response(status_code=HTTP_204_NO_CONTENT)


async def probe(request: Request) -> HealthProbeResponse:
    """
    Health check probe endpoint.

    Checks the active ingest backend and returns status information.

    The declared return type lets FastAPI document the 200 body in OpenAPI.
    A ``JSONResponse`` is still returned directly so the HTTP status code can
    reflect backend health; FastAPI skips re-serialization for ``Response``
    subclasses.

    Args:
        request: The incoming request

    Returns:
        JSON response with health status and table information
    """
    health_checker = request.app.state.health_checker
    status = await health_checker.check()

    # Determine overall health
    is_healthy = all(status.values())
    status_code = HTTP_200_OK if is_healthy else HTTP_502_BAD_GATEWAY

    # Build response
    response_data = {
        "status": jsonable_encoder(status),
        "healthy": is_healthy,
        "ingest_mode": request.app.state.ingest_mode,
    }

    # Add table info if healthy
    if is_healthy:
        try:
            response_data["table"] = await request.app.state.connector.get_table_name()
        except Exception as exc:
            logger.warning("Failed to get table name", error=str(exc))

    return JSONResponse(content=response_data, status_code=status_code)
