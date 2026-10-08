"""Backend health checks used by the ``GET /`` probe and the CLI."""

import asyncio
from collections.abc import Callable

import structlog
from clickhouse_connect.driver.asyncclient import AsyncClient

from evnt.protocols import HealthChecker
from evnt.storage.clickhouse.client import is_ready

logger = structlog.get_logger(__name__)


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

    def __init__(self, client: AsyncClient) -> None:
        self.client = client

    async def check(self) -> dict[str, bool]:
        """Check that ClickHouse answers queries."""
        try:
            healthy = await is_ready(self.client)
        except Exception as exc:
            logger.warning("ClickHouse health check failed", error=str(exc))
            healthy = False
        return {"clickhouse": healthy}
