"""Behaviour of ``RowCoalescer``: merging concurrent rows into shared flushes."""

import asyncio

import pytest

from evnt.exceptions import IngestUnavailableError
from evnt.ingest.coalesce import RowCoalescer


class _RecordingFlush:
    def __init__(self, *, fail: Exception | None = None, delay: float = 0.0):
        self.calls: list[tuple[str, list[dict]]] = []
        self.fail = fail
        self.delay = delay

    async def __call__(self, table_group, rows):
        self.calls.append((table_group, list(rows)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail


@pytest.mark.anyio
async def test_concurrent_calls_share_one_flush(anyio_backend):
    flush = _RecordingFlush()
    coalescer = RowCoalescer(flush, window_seconds=0.01, max_rows=100)

    await asyncio.gather(*(coalescer.add("evnt", [{"n": i}]) for i in range(5)))

    assert flush.calls == [("evnt", [{"n": i} for i in range(5)])]


@pytest.mark.anyio
async def test_a_full_batch_goes_out_without_waiting_for_the_window(anyio_backend):
    flush = _RecordingFlush()
    coalescer = RowCoalescer(flush, window_seconds=60, max_rows=4)

    await asyncio.wait_for(
        asyncio.gather(*(coalescer.add("evnt", [{"n": i}, {"n": i}]) for i in range(2))),
        timeout=1,
    )

    assert len(flush.calls) == 1
    assert len(flush.calls[0][1]) == 4


@pytest.mark.anyio
async def test_table_groups_are_flushed_separately(anyio_backend):
    flush = _RecordingFlush()
    coalescer = RowCoalescer(flush, window_seconds=0.01, max_rows=100)

    await asyncio.gather(
        coalescer.add("evnt", [{"a": 1}]),
        coalescer.add("other", [{"b": 1}]),
    )

    assert sorted(flush.calls) == [("evnt", [{"a": 1}]), ("other", [{"b": 1}])]


@pytest.mark.anyio
async def test_zero_window_flushes_each_call_on_its_own(anyio_backend):
    flush = _RecordingFlush()
    coalescer = RowCoalescer(flush, window_seconds=0, max_rows=100)

    await asyncio.gather(*(coalescer.add("evnt", [{"n": i}]) for i in range(3)))

    assert len(flush.calls) == 3


@pytest.mark.anyio
async def test_every_caller_of_a_failed_flush_gets_the_error(anyio_backend):
    flush = _RecordingFlush(fail=RuntimeError("broker gone"))
    coalescer = RowCoalescer(flush, window_seconds=0.01, max_rows=100)

    results = await asyncio.gather(
        *(coalescer.add("evnt", [{"n": i}]) for i in range(3)),
        return_exceptions=True,
    )

    assert len(flush.calls) == 1
    assert all(isinstance(r, RuntimeError) for r in results)


@pytest.mark.anyio
async def test_a_caller_that_goes_away_does_not_drop_its_rows(anyio_backend):
    flush = _RecordingFlush()
    coalescer = RowCoalescer(flush, window_seconds=0.05, max_rows=100)

    leaving = asyncio.create_task(coalescer.add("evnt", [{"n": "gone"}]))
    await asyncio.sleep(0)
    leaving.cancel()
    await coalescer.add("evnt", [{"n": "stays"}])

    assert flush.calls == [("evnt", [{"n": "gone"}, {"n": "stays"}])]


@pytest.mark.anyio
async def test_close_flushes_buffered_rows(anyio_backend):
    flush = _RecordingFlush()
    coalescer = RowCoalescer(flush, window_seconds=60, max_rows=100)

    waiting = asyncio.create_task(coalescer.add("evnt", [{"n": 1}]))
    await asyncio.sleep(0)
    await coalescer.close()

    await asyncio.wait_for(waiting, timeout=1)
    assert flush.calls == [("evnt", [{"n": 1}])]


@pytest.mark.anyio
async def test_rows_offered_after_close_are_refused_as_retryable(anyio_backend):
    flush = _RecordingFlush()
    coalescer = RowCoalescer(flush, window_seconds=0.01, max_rows=100)
    await coalescer.close()

    with pytest.raises(IngestUnavailableError):
        await coalescer.add("evnt", [{"n": 1}])
    assert flush.calls == []


@pytest.mark.anyio
async def test_a_cancelled_flush_answers_waiters_with_a_retryable_error(anyio_backend):
    flush = _RecordingFlush(delay=60)
    coalescer = RowCoalescer(flush, window_seconds=60, max_rows=1)

    waiting = asyncio.create_task(coalescer.add("evnt", [{"n": 1}]))
    await asyncio.sleep(0.01)
    for task in list(coalescer._tasks):
        task.cancel()

    with pytest.raises(IngestUnavailableError):
        await asyncio.wait_for(waiting, timeout=1)


@pytest.mark.anyio
async def test_the_window_timer_does_not_flush_twice_after_a_full_batch(anyio_backend):
    flush = _RecordingFlush()
    coalescer = RowCoalescer(flush, window_seconds=0.02, max_rows=2)

    await coalescer.add("evnt", [{"n": 1}, {"n": 2}])
    await asyncio.sleep(0.05)

    assert len(flush.calls) == 1


@pytest.mark.anyio
async def test_rows_that_would_overflow_a_batch_start_a_new_one(anyio_backend):
    flush = _RecordingFlush()
    coalescer = RowCoalescer(flush, window_seconds=0.01, max_rows=100)

    await asyncio.gather(
        coalescer.add("evnt", [{"n": i} for i in range(99)]),
        coalescer.add("evnt", [{"m": i} for i in range(100)]),
    )

    assert [len(rows) for _, rows in flush.calls] == [99, 100]


@pytest.mark.anyio
async def test_a_call_larger_than_max_rows_is_flushed_alone(anyio_backend):
    flush = _RecordingFlush()
    coalescer = RowCoalescer(flush, window_seconds=0.01, max_rows=10)

    await asyncio.gather(
        coalescer.add("evnt", [{"a": 1}]),
        coalescer.add("evnt", [{"big": i} for i in range(25)]),
        coalescer.add("evnt", [{"b": 1}]),
    )

    assert [len(rows) for _, rows in flush.calls] == [1, 25, 1]
