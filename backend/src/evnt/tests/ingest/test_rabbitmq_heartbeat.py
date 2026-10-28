"""Tests for the worker liveness heartbeat.

The heartbeat must refresh the liveness file independently of message flow and
the batch-flush timeout, so an idle worker configured with a large
``batch_timeout_ms`` is never wrongly reported as dead by ``queue healthcheck``.
"""

import asyncio
import os
import types
from contextlib import suppress

import pytest

from evnt.constants import WORKER_HEARTBEAT_SECONDS, WORKER_LIVENESS_STALE_SECONDS
from evnt.ingest import rabbitmq as rabbitmq_module
from evnt.ingest.rabbitmq import RabbitMQBatchWorker


def test_heartbeat_interval_below_stale_threshold():
    # Heartbeat cadence must stay strictly under the staleness threshold so the
    # file is refreshed before the healthcheck would consider it stale.
    assert 0 < WORKER_HEARTBEAT_SECONDS < WORKER_LIVENESS_STALE_SECONDS


def test_mark_alive_replaces_liveness_file_atomically(monkeypatch, tmp_path):
    liveness_path = tmp_path / "evnt-worker.alive"
    liveness_path.write_text("123.0")
    observed_contents = []
    real_replace = os.replace

    def observing_replace(source, destination):
        observed_contents.append(liveness_path.read_text())
        real_replace(source, destination)

    monkeypatch.setattr(rabbitmq_module, "WORKER_LIVENESS_PATH", liveness_path)
    monkeypatch.setattr(rabbitmq_module.os, "replace", observing_replace)

    RabbitMQBatchWorker._mark_alive(object())

    assert observed_contents == ["123.0"]
    assert float(liveness_path.read_text()) > 123.0
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.anyio
async def test_heartbeat_loop_marks_alive_immediately(anyio_backend):
    # The loop writes liveness before its first sleep, so the file is fresh as
    # soon as the worker starts, regardless of the configured batch timeout.
    marks: list[int] = []
    worker = types.SimpleNamespace(
        _mark_alive=lambda: marks.append(1),
        _busy_seconds=lambda: 0.0,
        stall_threshold=120.0,
    )

    task = asyncio.create_task(RabbitMQBatchWorker._heartbeat_loop(worker))
    await asyncio.sleep(0)  # let the loop run its first mark, then suspend on sleep
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert marks == [1]


@pytest.mark.anyio
async def test_heartbeat_loop_withholds_liveness_while_step_stalled(anyio_backend):
    # A step running past the stall threshold means the batch loop is stuck on
    # a hung await; the heartbeat must let the liveness file go stale.
    marks: list[int] = []
    worker = types.SimpleNamespace(
        _mark_alive=lambda: marks.append(1),
        _busy_seconds=lambda: 121.0,
        stall_threshold=120.0,
    )

    task = asyncio.create_task(RabbitMQBatchWorker._heartbeat_loop(worker))
    await asyncio.sleep(0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert marks == []


def test_busy_seconds_is_zero_while_idle_and_grows_during_step(monkeypatch):
    worker = types.SimpleNamespace(_busy_since=None)
    assert RabbitMQBatchWorker._busy_seconds(worker) == 0.0

    monkeypatch.setattr(rabbitmq_module.time, "monotonic", lambda: 1000.0)
    worker._busy_since = 900.0
    assert RabbitMQBatchWorker._busy_seconds(worker) == 100.0
