"""End-to-end: tracker requests through the real app into a real ClickHouse."""

import asyncio
import time
import uuid

import pytest
from clickhouse_connect.driver.client import Client
from fastapi.testclient import TestClient

from evnt.config import settings
from evnt.storage.clickhouse import ClickHouseConnector, TableManager
from evnt.storage.clickhouse import client as clickhouse
from evnt.tests.support import minimal_tp2_payload

pytestmark = pytest.mark.integration


async def _init_tables() -> None:
    client = await clickhouse.create_client(settings.clickhouse, pool_size=1)
    try:
        connector = ClickHouseConnector(
            client,
            insert_settings=clickhouse.insert_settings(settings.ingest.direct),
            **settings.clickhouse.configuration.model_dump(),
        )
        await TableManager(connector).create_all_tables()
    finally:
        await client.close()


def _rows_for(ch: Client, database: str, app_id: str, *, timeout: float = 10.0) -> list[tuple]:
    """Poll until the rows for ``app_id`` are visible (async inserts flush late)."""
    deadline = time.monotonic() + timeout
    while True:
        rows = ch.query(
            f"SELECT app_id, platform, event_type FROM {database}.local WHERE app_id = %(app_id)s",
            parameters={"app_id": app_id},
        ).result_rows
        if rows or time.monotonic() > deadline:
            return rows
        time.sleep(0.2)


@pytest.fixture
def tables(clickhouse_settings: None) -> None:
    asyncio.run(_init_tables())


def _payload(app_id: str) -> dict:
    payload = minimal_tp2_payload()
    payload["data"][0]["aid"] = app_id
    return payload


def test_db_init_is_idempotent(clickhouse_settings, ch, database):
    asyncio.run(_init_tables())
    asyncio.run(_init_tables())

    tables = {row[0] for row in ch.query(f"SHOW TABLES FROM {database}").result_rows}
    assert "local" in tables


def test_post_batch_lands_in_clickhouse(tables, ch, database):
    from evnt.main import create_app

    app_id = f"it-post-{uuid.uuid4().hex[:8]}"
    with TestClient(create_app()) as client:
        response = client.post(
            settings.common.snowplow.endpoints.post_endpoint, json=_payload(app_id)
        )
        assert client.get("/").json()["healthy"] is True

    assert response.status_code == 204
    assert _rows_for(ch, database, app_id) == [(app_id, "web", "pv")]


def test_get_pixel_lands_in_clickhouse(tables, ch, database):
    from evnt.main import create_app

    app_id = f"it-get-{uuid.uuid4().hex[:8]}"
    event = _payload(app_id)["data"][0]
    with TestClient(create_app()) as client:
        response = client.get(settings.common.snowplow.endpoints.get_endpoint, params=event)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/gif"
    assert _rows_for(ch, database, app_id) == [(app_id, "web", "pv")]


def test_sync_insert_overrides_async_client_defaults(tables, ch, database, monkeypatch):
    from evnt.main import create_app

    real_get_async_client = clickhouse.get_async_client

    async def get_client_with_async_defaults(**kwargs):
        return await real_get_async_client(
            **kwargs,
            settings={
                "async_insert": 1,
                "wait_for_async_insert": 0,
                "async_insert_busy_timeout_ms": 60000,
                "async_insert_use_adaptive_busy_timeout": 0,
            },
        )

    monkeypatch.setattr(clickhouse, "get_async_client", get_client_with_async_defaults)
    monkeypatch.setattr(settings.ingest.direct, "async_insert", False)
    app_id = f"it-sync-{uuid.uuid4().hex[:8]}"
    with TestClient(create_app()) as client:
        response = client.post(
            settings.common.snowplow.endpoints.post_endpoint, json=_payload(app_id)
        )
        assert response.status_code == 204
        # A successful synchronous response means the row is already visible,
        # even when the connection would otherwise buffer inserts for a minute.
        assert _rows_for(ch, database, app_id, timeout=0) == [(app_id, "web", "pv")]


def test_rabbitmq_worker_delivers_published_events(
    tables,
    rabbitmq_settings,
    monkeypatch,
    ch,
    database,
):
    from evnt.ingest import RabbitMQBatchWorker
    from evnt.main import create_app

    app_id = f"it-queue-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings.ingest, "mode", "rabbitmq")
    with TestClient(create_app()) as client:
        response = client.post(
            settings.common.snowplow.endpoints.post_endpoint, json=_payload(app_id)
        )
        assert response.status_code == 204
        assert client.get("/").json()["ingest_mode"] == "rabbitmq"

    async def run_worker() -> None:
        ch_client = await clickhouse.create_ready_client(settings.clickhouse, pool_size=1)
        connector = ClickHouseConnector(
            ch_client,
            insert_settings=clickhouse.insert_settings(settings.ingest.direct, require_wait=True),
            **settings.clickhouse.configuration.model_dump(),
        )
        worker = await RabbitMQBatchWorker.create(connector, settings.ingest.rabbitmq)
        task = asyncio.create_task(worker.run())
        try:
            deadline = asyncio.get_running_loop().time() + 15
            while not await asyncio.to_thread(_rows_for, ch, database, app_id, timeout=0):
                assert asyncio.get_running_loop().time() < deadline, "worker never flushed"
                await asyncio.sleep(0.2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await worker.close()
            await ch_client.close()

    asyncio.run(run_worker())
    assert _rows_for(ch, database, app_id) == [(app_id, "web", "pv")]
