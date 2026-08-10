"""Bounded helpers for CPU-heavy synchronous work."""

import asyncio
from collections.abc import Callable

from .config import settings

_cpu_task_slots: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _cpu_task_semaphore() -> asyncio.Semaphore:
    """Return the offload semaphore that belongs to the running event loop.

    ``asyncio.Semaphore`` binds itself to a loop the first time it is contended,
    so a single module-level instance would start raising "bound to a different
    event loop" as soon as a second loop appears in the process (repeated
    ``asyncio.run`` from the CLI, test clients). Keep one semaphore per live
    loop instead and drop the entries of loops that have been closed.
    """

    loop = asyncio.get_running_loop()
    slots = _cpu_task_slots.get(loop)
    if slots is None:
        for closed in [known for known in _cpu_task_slots if known.is_closed()]:
            del _cpu_task_slots[closed]
        slots = asyncio.Semaphore(settings.performance.cpu_task_concurrency)
        _cpu_task_slots[loop] = slots
    return slots


async def run_cpu_task[**P, T](
    function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run synchronous CPU work without growing the executor queue unboundedly."""

    async with _cpu_task_semaphore():
        return await asyncio.to_thread(function, *args, **kwargs)
