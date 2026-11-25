"""Real-backend checks for input no fake can vouch for.

An unpaired surrogate is refused by the UTF-8 encoders of the real drivers,
not by the fakes, and what the broker does with an undecodable message (route
the parked copy, drain the source queue) is only visible on a real RabbitMQ.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aio_pika
import pytest
from clickhouse_connect.driver.client import Client
from fastapi.testclient import TestClient

from evnt.config import settings
from evnt.storage.clickhouse import ClickHouseConnector, TableManager
from evnt.storage.clickhouse import client as clickhouse
from evnt.tests.support import minimal_tp2_payload

pytestmark = pytest.mark.integration

# A high surrogate whose low half was cut off, as JSON carries it.
LONE_ESCAPE = "\\ud83d"
REPLACED = "�"
SURROGATE_NOTE = "unpaired surrogate replaced with U+FFFD"


async def _worker_connector() -> tuple[Any, ClickHouseConnector]:
    client = await clickhouse.create_ready_client(settings.clickhouse, pool_size=1)
    connector = ClickHouseConnector(
        client,
        insert_settings=clickhouse.insert_settings(settings.ingest.direct, require_wait=True),
        **settings.clickhouse.configuration.model_dump(),
    )
    return client, connector


@pytest.fixture
def tables(clickhouse_settings: None) -> None:
    async def create() -> None:
        client, connector = await _worker_connector()
        try:
            await TableManager(connector).create_all_tables()
        finally:
            await client.close()

    asyncio.run(create())


def _stored(ch: Client, database: str, app_id: str, *, timeout: float = 10.0) -> list[tuple]:
    """Poll until the rows for ``app_id`` are visible (async inserts flush late)."""
    deadline = time.monotonic() + timeout
    while True:
        rows = ch.query(
            f"SELECT title, page, toJSONString(extra) FROM {database}.local"
            " WHERE app_id = %(app_id)s",
            parameters={"app_id": app_id},
        ).result_rows
        if rows or time.monotonic() > deadline:
            return rows
        time.sleep(0.2)


def _surrogate_body(app_id: str) -> bytes:
    """A tp2 batch as raw JSON, so the lone escape stays an escape on the wire."""
    payload = minimal_tp2_payload()
    payload["data"][0].update(aid=app_id, page="x@@y", url="https://example.com/@@")
    body = json.dumps(payload).replace("@@", LONE_ESCAPE).encode("ascii")
    assert LONE_ESCAPE.encode() in body
    return body


def _post(client: TestClient, body: bytes) -> None:
    response = client.post(
        settings.common.snowplow.endpoints.post_endpoint,
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 204


def _assert_scrubbed(rows: list[tuple]) -> None:
    assert len(rows) == 1
    title, page, extra = rows[0]
    assert title == f"x{REPLACED}y"
    assert page == f"https://example.com/{REPLACED}"
    notes = sorted(json.loads(extra)["validation"], key=lambda note: note["field"])
    assert notes == [
        {"stage": "payload", "field": field, "error": SURROGATE_NOTE} for field in ("page", "url")
    ]


async def _with_worker(use: Callable[[Any], Awaitable[None]]) -> None:
    """Run the real worker on the real connector for the length of ``use``."""
    from evnt.ingest import RabbitMQBatchWorker

    ch_client, connector = await _worker_connector()
    worker = await RabbitMQBatchWorker.create(connector, settings.ingest.rabbitmq)
    task = asyncio.create_task(worker.run())
    try:
        await use(worker)
        assert not task.done(), "the worker stopped"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await worker.close()
        await ch_client.close()


async def _broker() -> aio_pika.abc.AbstractConnection:
    rabbitmq = settings.ingest.rabbitmq
    return await aio_pika.connect(
        host=rabbitmq.host,
        port=rabbitmq.port,
        login=rabbitmq.username,
        password=rabbitmq.password.get_secret_value(),
    )


async def _eventually(check: Callable[[], Awaitable[bool]], what: str) -> None:
    deadline = asyncio.get_running_loop().time() + 15
    while not await check():
        assert asyncio.get_running_loop().time() < deadline, what
        await asyncio.sleep(0.2)


def test_lone_surrogate_event_lands_in_clickhouse_directly(tables, ch, database):
    from evnt.main import create_app

    app_id = f"it-surrogate-{uuid.uuid4().hex[:8]}"
    with TestClient(create_app()) as client:
        _post(client, _surrogate_body(app_id))

    _assert_scrubbed(_stored(ch, database, app_id))


def test_lone_surrogate_event_lands_in_clickhouse_through_the_queue(
    tables, rabbitmq_settings, monkeypatch, ch, database
):
    from evnt.main import create_app

    app_id = f"it-surrogate-q-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings.ingest, "mode", "rabbitmq")
    with TestClient(create_app()) as client:
        _post(client, _surrogate_body(app_id))

    async def until_flushed(worker: Any) -> None:
        async def flushed() -> bool:
            return bool(await asyncio.to_thread(_stored, ch, database, app_id, timeout=0))

        await _eventually(flushed, "worker never flushed")

    asyncio.run(_with_worker(until_flushed))
    _assert_scrubbed(_stored(ch, database, app_id))


@pytest.mark.parametrize("body", [b"not-json{", b"\xff\xfe\x00", b'{"rows": "nope"}'])
def test_undecodable_message_is_parked_and_the_worker_keeps_delivering(
    tables, rabbitmq_settings, monkeypatch, ch, database, body
):
    from evnt.main import create_app

    rabbitmq = settings.ingest.rabbitmq
    queue_name = rabbitmq.queue_name
    failed_queue = rabbitmq.resolved_failed_queue_name
    app_id = f"it-parked-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings.ingest, "mode", "rabbitmq")

    async def scenario(worker: Any) -> None:
        connection = await _broker()
        try:
            channel = await connection.channel()
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body,
                    headers={"x-origin": "test"},
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=queue_name,
            )
            failed = await channel.get_queue(failed_queue)

            parked: list[Any] = []

            async def taken() -> bool:
                message = await failed.get(no_ack=True, fail=False)
                if message is not None:
                    parked.append(message)
                return bool(parked)

            await _eventually(taken, "the message never reached the failed queue")
            (message,) = parked
            assert message.body == body
            assert message.headers["x-origin"] == "test"
            assert message.headers["x-evnt-failure"] == "undecodable"
            assert message.headers["x-evnt-source-queue"] == queue_name
            assert message.headers["x-evnt-error"]
            assert "x-evnt-table-group" not in message.headers

            # The source queue drained and the worker is still consuming: an
            # event published after the bad message reaches ClickHouse.
            await asyncio.to_thread(_publish_event, create_app, app_id)

            async def flushed() -> bool:
                return bool(await asyncio.to_thread(_stored, ch, database, app_id, timeout=0))

            await _eventually(flushed, "worker stopped delivering after parking")
            source = await channel.declare_queue(queue_name, durable=True, passive=True)
            assert source.declaration_result.message_count == 0
            assert await failed.get(no_ack=True, fail=False) is None
        finally:
            await connection.close()

    try:
        asyncio.run(_with_worker(scenario))
    finally:
        asyncio.run(_delete_queues(queue_name, failed_queue))


def _publish_event(create_app: Callable[[], Any], app_id: str) -> None:
    payload = minimal_tp2_payload()
    payload["data"][0]["aid"] = app_id
    with TestClient(create_app()) as client:
        _post(client, json.dumps(payload).encode())


async def _delete_queues(*names: str) -> None:
    connection = await _broker()
    try:
        channel = await connection.channel()
        for name in names:
            await channel.queue_delete(name)
    finally:
        await connection.close()
