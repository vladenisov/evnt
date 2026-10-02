import asyncio

import pytest
from evnt.health import CachedHealthChecker


class _FakeHealthChecker:
    def __init__(self, *statuses: dict[str, bool]) -> None:
        self.statuses = list(statuses)
        self.calls = 0

    async def check(self) -> dict[str, bool]:
        self.calls += 1
        return self.statuses.pop(0).copy()


class _SlowHealthChecker:
    def __init__(self) -> None:
        self.calls = 0
        self.release = asyncio.Event()

    async def check(self) -> dict[str, bool]:
        self.calls += 1
        await self.release.wait()
        return {"backend": True}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_cached_health_checker_reuses_status_until_ttl_expires(anyio_backend):
    current_time = 0.0
    checker = _FakeHealthChecker({"backend": True}, {"backend": False})
    cached = CachedHealthChecker(
        checker,
        ttl_seconds=2.0,
        clock=lambda: current_time,
    )

    first_status = await cached.check()
    first_status["backend"] = False

    assert await cached.check() == {"backend": True}
    assert checker.calls == 1

    current_time = 2.01

    assert await cached.check() == {"backend": False}
    assert checker.calls == 2


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_cached_health_checker_can_be_disabled(anyio_backend):
    checker = _FakeHealthChecker({"backend": True}, {"backend": False})
    cached = CachedHealthChecker(checker, ttl_seconds=0, clock=lambda: 0.0)

    assert await cached.check() == {"backend": True}
    assert await cached.check() == {"backend": False}
    assert checker.calls == 2


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_cached_health_checker_coalesces_concurrent_misses(anyio_backend):
    checker = _SlowHealthChecker()
    cached = CachedHealthChecker(checker, ttl_seconds=2.0, clock=lambda: 0.0)

    first_check = asyncio.create_task(cached.check())
    await asyncio.sleep(0)
    second_check = asyncio.create_task(cached.check())
    await asyncio.sleep(0)

    assert checker.calls == 1

    checker.release.set()

    assert await first_check == {"backend": True}
    assert await second_check == {"backend": True}
    assert checker.calls == 1
