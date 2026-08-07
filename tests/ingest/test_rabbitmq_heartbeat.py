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
from core.constants import WORKER_HEARTBEAT_SECONDS, WORKER_LIVENESS_STALE_SECONDS
from ingest import rabbitmq as rabbitmq_module
from ingest.rabbitmq import RabbitMQBatchWorker


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
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_heartbeat_loop_marks_alive_immediately(anyio_backend):
    # The loop writes liveness before its first sleep, so the file is fresh as
    # soon as the worker starts, regardless of the configured batch timeout.
    marks: list[int] = []
    worker = types.SimpleNamespace(_mark_alive=lambda: marks.append(1))

    task = asyncio.create_task(RabbitMQBatchWorker._heartbeat_loop(worker))
    await asyncio.sleep(0)  # let the loop run its first mark, then suspend on sleep
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert marks == [1]
