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
async def test_every_caller_of_an_unavailable_flush_gets_the_error(anyio_backend):
    error = IngestUnavailableError("broker gone", retry_after_seconds=7)
    flush = _RecordingFlush(fail=error)
    coalescer = RowCoalescer(flush, window_seconds=0.01, max_rows=100)

    results = await asyncio.gather(
        *(coalescer.add("evnt", [{"n": i}]) for i in range(3)),
        return_exceptions=True,
    )

    # Fanned out as it is, with no flush per caller: the backend takes nothing.
    assert flush.calls == [("evnt", [{"n": i} for i in range(3)])]
    assert results == [error, error, error]


class _PoisonAwareFlush:
    """Fails like an encoder would: only when the rows hold a poisoned one."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict]]] = []
        self.stored: list[dict] = []
        self.unavailable_from_call: int | None = None

    async def __call__(self, table_group: str, rows: list[dict]) -> None:
        self.calls.append((table_group, list(rows)))
        await asyncio.sleep(0)
        if self.unavailable_from_call is not None and len(self.calls) >= self.unavailable_from_call:
            raise IngestUnavailableError("broker gone")
        if any(row.get("poison") for row in rows):
            raise ValueError("row cannot be encoded")
        self.stored.extend(rows)


@pytest.mark.anyio
async def test_a_poisoned_caller_fails_alone(anyio_backend):
    flush = _PoisonAwareFlush()
    coalescer = RowCoalescer(flush, window_seconds=0.01, max_rows=100)

    results = await asyncio.gather(
        coalescer.add("evnt", [{"n": 0}, {"n": 1}]),
        coalescer.add("evnt", [{"n": 2}, {"poison": True}]),
        coalescer.add("evnt", [{"n": 3}]),
        return_exceptions=True,
    )

    assert results[0] is None
    assert isinstance(results[1], ValueError)
    assert results[2] is None
    # The merged flush, then one flush per caller in arrival order.
    assert [rows for _, rows in flush.calls] == [
        [{"n": 0}, {"n": 1}, {"n": 2}, {"poison": True}, {"n": 3}],
        [{"n": 0}, {"n": 1}],
        [{"n": 2}, {"poison": True}],
        [{"n": 3}],
    ]
    # Nothing of the poisoned call is stored, and nothing is stored twice.
    assert flush.stored == [{"n": 0}, {"n": 1}, {"n": 3}]


@pytest.mark.anyio
async def test_a_failed_flush_of_one_caller_is_not_repeated(anyio_backend):
    flush = _PoisonAwareFlush()
    coalescer = RowCoalescer(flush, window_seconds=0.01, max_rows=100)

    with pytest.raises(ValueError, match="cannot be encoded"):
        await coalescer.add("evnt", [{"poison": True}])

    assert len(flush.calls) == 1


@pytest.mark.anyio
async def test_unavailability_during_the_fallback_fans_out_to_the_callers_left(anyio_backend):
    flush = _PoisonAwareFlush()
    # Call 1 is the merged flush, call 2 the first caller; the third call fails.
    flush.unavailable_from_call = 3
    coalescer = RowCoalescer(flush, window_seconds=0.01, max_rows=100)

    results = await asyncio.gather(
        coalescer.add("evnt", [{"n": 0}]),
        coalescer.add("evnt", [{"poison": True}]),
        coalescer.add("evnt", [{"n": 2}]),
        coalescer.add("evnt", [{"n": 3}]),
        return_exceptions=True,
    )

    assert results[0] is None
    assert all(isinstance(result, IngestUnavailableError) for result in results[1:])
    # The callers left are answered at once, not flushed one after another.
    assert len(flush.calls) == 3
    assert flush.stored == [{"n": 0}]


@pytest.mark.anyio
async def test_the_fallback_still_flushes_a_caller_that_went_away(anyio_backend):
    flush = _PoisonAwareFlush()
    coalescer = RowCoalescer(flush, window_seconds=0.05, max_rows=100)

    leaving = asyncio.create_task(coalescer.add("evnt", [{"n": "gone"}]))
    await asyncio.sleep(0)
    leaving.cancel()
    results = await asyncio.gather(
        coalescer.add("evnt", [{"poison": True}]),
        coalescer.add("evnt", [{"n": "stays"}]),
        return_exceptions=True,
    )

    assert isinstance(results[0], ValueError)
    assert results[1] is None
    assert flush.stored == [{"n": "gone"}, {"n": "stays"}]


@pytest.mark.anyio
async def test_a_fallback_cancelled_midway_answers_only_the_callers_left(anyio_backend):
    release = asyncio.Event()
    calls: list[list[dict]] = []

    async def flush(table_group: str, rows: list[dict]) -> None:
        calls.append(list(rows))
        if len(calls) == 1:
            raise ValueError("row cannot be encoded")
        if len(calls) == 3:
            await release.wait()

    coalescer = RowCoalescer(flush, window_seconds=0.01, max_rows=100)
    waiting = [asyncio.create_task(coalescer.add("evnt", [{"n": i}])) for i in range(3)]
    await asyncio.wait_for(waiting[0], timeout=1)
    for task in list(coalescer._tasks):
        task.cancel()

    assert waiting[0].result() is None
    for task in waiting[1:]:
        with pytest.raises(IngestUnavailableError):
            await asyncio.wait_for(task, timeout=1)
    assert calls == [[{"n": 0}, {"n": 1}, {"n": 2}], [{"n": 0}], [{"n": 1}]]


@pytest.mark.anyio
async def test_close_waits_for_a_running_fallback(anyio_backend):
    flush = _PoisonAwareFlush()
    coalescer = RowCoalescer(flush, window_seconds=60, max_rows=100)

    waiting = [
        asyncio.create_task(coalescer.add("evnt", rows))
        for rows in ([{"n": 0}], [{"poison": True}], [{"n": 2}])
    ]
    await asyncio.sleep(0)
    await coalescer.close()

    assert all(task.done() for task in waiting)
    assert waiting[0].result() is None
    assert isinstance(waiting[1].exception(), ValueError)
    assert waiting[2].result() is None
    assert flush.stored == [{"n": 0}, {"n": 2}]


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
