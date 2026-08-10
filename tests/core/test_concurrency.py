"""Tests for bounded synchronous CPU offloads."""

import asyncio

import pytest
from core import concurrency


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_run_cpu_task_bounds_submitted_work(monkeypatch, anyio_backend):
    release = asyncio.Event()
    saturated = asyncio.Event()
    submitted = 0
    peak_submitted = 0

    async def _fake_to_thread(function, *args, **kwargs):
        nonlocal submitted, peak_submitted
        submitted += 1
        peak_submitted = max(peak_submitted, submitted)
        if submitted == 2:
            saturated.set()
        await release.wait()
        submitted -= 1
        return function(*args, **kwargs)

    monkeypatch.setattr(concurrency, "_cpu_task_slots", {})
    monkeypatch.setattr(concurrency.settings.performance, "cpu_task_concurrency", 2)
    monkeypatch.setattr(concurrency.asyncio, "to_thread", _fake_to_thread)

    tasks = [asyncio.create_task(concurrency.run_cpu_task(int, "1")) for _ in range(3)]
    # Both slots are taken, and the third task has already been given its turn
    # on the loop (tasks are scheduled FIFO), so it is parked on the semaphore.
    await asyncio.wait_for(saturated.wait(), timeout=5)

    assert submitted == 2
    assert peak_submitted == 2

    release.set()
    assert await asyncio.gather(*tasks) == [1, 1, 1]
    assert peak_submitted == 2


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_run_cpu_task_semaphore_uses_configured_concurrency(
    monkeypatch,
    anyio_backend,
):
    monkeypatch.setattr(concurrency, "_cpu_task_slots", {})
    monkeypatch.setattr(concurrency.settings.performance, "cpu_task_concurrency", 3)

    assert await concurrency.run_cpu_task(int, "1") == 1

    (semaphore,) = concurrency._cpu_task_slots.values()
    for _ in range(3):
        await asyncio.wait_for(semaphore.acquire(), timeout=1)
    assert semaphore.locked()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_run_cpu_task_rebinds_after_the_owning_loop_is_gone(
    monkeypatch,
    anyio_backend,
):
    """A semaphore left over from a closed loop must not leak into a new one."""

    stale_loop = asyncio.new_event_loop()
    stale_loop.close()
    monkeypatch.setattr(
        concurrency,
        "_cpu_task_slots",
        {stale_loop: asyncio.Semaphore(1)},
    )

    assert await concurrency.run_cpu_task(int, "1") == 1

    assert stale_loop not in concurrency._cpu_task_slots
    assert list(concurrency._cpu_task_slots) == [asyncio.get_running_loop()]
