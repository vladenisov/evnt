"""End-to-end: tracker requests through the real app into a real ClickHouse."""

import asyncio
import base64
import time
import uuid
from datetime import UTC, datetime

import orjson
import pytest
from clickhouse_connect.driver import AsyncClient
from clickhouse_connect.driver.client import Client
from fastapi.testclient import TestClient

from evnt.config import EncryptionKeyConfig, settings
from evnt.crypto import generate_keypair, seal_envelope
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


@pytest.fixture
def encryption_key(monkeypatch):
    private_key, public_key = generate_keypair()
    monkeypatch.setattr(settings.encryption, "enabled", True)
    monkeypatch.setattr(
        settings.encryption,
        "keys",
        [EncryptionKeyConfig(kid="integration", private_key=private_key)],
    )
    return base64.b64decode(public_key)


def _send_encrypted(client, public_key, app_id, transport, *, compress=True, headers=None):
    envelope = seal_envelope(
        public_key, "integration", orjson.dumps(_payload(app_id)), compress=compress
    )
    endpoint = settings.encryption.endpoint
    if transport == "pixel":
        return client.get(
            endpoint, params={"d": base64.urlsafe_b64encode(envelope).decode()}, headers=headers
        )
    body = base64.b64encode(envelope) if transport == "base64" else envelope
    return client.post(endpoint, content=body, headers=headers)


def test_db_init_is_idempotent(clickhouse_settings, ch, database):
    asyncio.run(_init_tables())
    asyncio.run(_init_tables())

    tables = {row[0] for row in ch.query(f"SHOW TABLES FROM {database}").result_rows}
    assert "local" in tables


@pytest.mark.anyio
async def test_native_async_client_handles_concurrent_queries(clickhouse_settings):
    client = await clickhouse.create_ready_client(settings.clickhouse, pool_size=2)
    try:
        assert isinstance(client, AsyncClient)
        results = await asyncio.wait_for(
            asyncio.gather(*(client.query(clickhouse.READINESS_QUERY) for _ in range(16))),
            timeout=10,
        )
        assert all(result.first_row == (1,) for result in results)
    finally:
        await client.close()


@pytest.mark.parametrize("transport", ["post", "pixel"])
def test_tracker_timestamps_keep_utc_millisecond_precision(tables, ch, database, transport):
    from evnt.main import create_app

    app_id = f"it-time-{uuid.uuid4().hex[:8]}"
    milliseconds = 1791522000123
    payload = _payload(app_id)
    payload["data"][0].update(dtm=str(milliseconds), stm=str(milliseconds + 456))
    with TestClient(create_app()) as client:
        if transport == "post":
            response = client.post(settings.common.snowplow.endpoints.post_endpoint, json=payload)
        else:
            response = client.get(
                settings.common.snowplow.endpoints.get_endpoint, params=payload["data"][0]
            )
        assert response.status_code == (204 if transport == "post" else 200)

    assert _rows_for(ch, database, app_id)
    row = ch.query(
        f"SELECT time, toUnixTimestamp64Milli(time), toUnixTimestamp64Milli(time_extra.sent) "
        f"FROM {database}.local WHERE app_id = %(app_id)s",
        parameters={"app_id": app_id},
    ).first_row
    assert row == (
        datetime.fromtimestamp(milliseconds / 1000, tz=UTC).replace(tzinfo=None),
        milliseconds,
        milliseconds + 456,
    )


@pytest.mark.parametrize("transport", ["post", "pixel", "sealed", "sealed_pixel"])
@pytest.mark.parametrize(
    "client_ip,legacy_ip",
    [
        ("2001:db8::7", "0.0.0.0"),
        ("198.51.100.7", "198.51.100.7"),
    ],
)
def test_client_network_json_roundtrip_keeps_ipv4_schema(
    tables,
    ch,
    database,
    monkeypatch,
    request,
    transport,
    client_ip,
    legacy_ip,
):
    from evnt.main import create_app

    monkeypatch.setattr(settings.security, "trust_proxy_headers", True)
    monkeypatch.setattr(
        settings.common,
        "snowplow",
        settings.common.snowplow.model_copy(
            update={
                "user_ip_header": "CF-Connecting-IP",
                "user_country_header": "CF-IPCountry",
            }
        ),
    )
    public_key = (
        request.getfixturevalue("encryption_key") if transport.startswith("sealed") else None
    )
    app_id = f"it-network-{uuid.uuid4().hex[:8]}"
    headers = {"CF-Connecting-IP": client_ip, "CF-IPCountry": "CN"}
    with TestClient(create_app()) as client:
        if public_key is not None:
            response = _send_encrypted(
                client,
                public_key,
                app_id,
                "pixel" if transport == "sealed_pixel" else "binary",
                headers=headers,
            )
        elif transport == "post":
            response = client.post(
                settings.common.snowplow.endpoints.post_endpoint,
                json=_payload(app_id),
                headers=headers,
            )
        else:
            response = client.get(
                settings.common.snowplow.endpoints.get_endpoint,
                params=_payload(app_id)["data"][0],
                headers=headers,
            )
        assert response.status_code in (200, 204)
    assert _rows_for(ch, database, app_id)
    row = ch.query(
        f"SELECT toTypeName(user_ip), toString(user_ip), extra.collector.client_ip.:String, "
        f"extra.collector.ip_country.:String FROM {database}.local WHERE app_id = %(app_id)s",
        parameters={"app_id": app_id},
    ).first_row
    assert row == ("IPv4", legacy_ip, client_ip, "CN")


def test_geo_and_anonymous_collector_keys_roundtrip(tables, ch, database, monkeypatch):
    """Geo floats and the anonymous flag land beside rows that have neither."""
    from evnt.main import create_app

    monkeypatch.setattr(settings.security, "trust_proxy_headers", True)
    monkeypatch.setattr(
        settings.common,
        "snowplow",
        settings.common.snowplow.model_copy(
            update={
                "user_ip_header": "CF-Connecting-IP",
                "user_country_header": "CF-IPCountry",
                "cloudflare_geo_headers": True,
                "honor_sp_anonymous": True,
            }
        ),
    )
    located_app = f"it-geo-{uuid.uuid4().hex[:8]}"
    anonymous_app = f"it-anon-{uuid.uuid4().hex[:8]}"
    headers = {
        "CF-Connecting-IP": "203.0.113.5",
        "CF-IPCountry": "US",
        "CF-IPCity": "Austin",
        "CF-IPLatitude": "30.2713",
        "CF-IPLongitude": "-97.7426",
        "CF-Timezone": "America/Chicago",
    }
    endpoint = settings.common.snowplow.endpoints.post_endpoint
    with TestClient(create_app()) as client:
        located = client.post(endpoint, json=_payload(located_app), headers=headers)
        anonymous = client.post(
            endpoint, json=_payload(anonymous_app), headers={**headers, "SP-Anonymous": "*"}
        )
    assert (located.status_code, anonymous.status_code) == (204, 204)
    assert _rows_for(ch, database, located_app)
    assert _rows_for(ch, database, anonymous_app)
    query = (
        "SELECT toString(user_ip), extra.collector.client_ip.:String, "
        "extra.collector.geo.city.:String, extra.collector.geo.latitude.:Float64, "
        "extra.collector.geo.longitude.:Float64, extra.collector.anonymous.:Bool "
        f"FROM {database}.local WHERE app_id = %(app_id)s"
    )
    assert ch.query(query, parameters={"app_id": located_app}).first_row == (
        "203.0.113.5",
        "203.0.113.5",
        "Austin",
        30.2713,
        -97.7426,
        None,
    )
    assert ch.query(query, parameters={"app_id": anonymous_app}).first_row == (
        "0.0.0.0",
        None,
        None,
        None,
        None,
        True,
    )


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


@pytest.mark.parametrize("transport", ["plaintext", "binary", "pixel"])
def test_rabbitmq_worker_delivers_published_events(
    tables,
    rabbitmq_settings,
    monkeypatch,
    ch,
    database,
    request,
    transport,
):
    from evnt.ingest import RabbitMQBatchWorker
    from evnt.main import create_app

    app_id = f"it-queue-{uuid.uuid4().hex[:8]}"
    public_key = request.getfixturevalue("encryption_key") if transport != "plaintext" else None
    monkeypatch.setattr(settings.ingest, "mode", "rabbitmq")
    with TestClient(create_app()) as client:
        if public_key is None:
            response = client.post(
                settings.common.snowplow.endpoints.post_endpoint, json=_payload(app_id)
            )
        else:
            response = _send_encrypted(client, public_key, app_id, transport)
        assert response.status_code == (200 if transport == "pixel" else 204)
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


@pytest.mark.parametrize("transport", ["binary", "base64", "pixel"])
@pytest.mark.parametrize("compress", [False, True])
def test_encrypted_event_lands_in_clickhouse(
    tables, encryption_key, ch, database, transport, compress
):
    from evnt.main import create_app

    app_id = f"it-sealed-{uuid.uuid4().hex[:8]}"
    with TestClient(create_app()) as client:
        response = _send_encrypted(client, encryption_key, app_id, transport, compress=compress)
    assert response.status_code == (200 if transport == "pixel" else 204)
    assert _rows_for(ch, database, app_id) == [(app_id, "web", "pv")]


def test_bad_events_cost_only_themselves_and_their_issues_land_in_extra(tables, ch, database):
    """A batch with an unbuildable event and an invalid-but-buildable one.

    The batch is acknowledged, the unbuildable event is dropped, and the
    collector-owned ``extra.validation`` / ``extra.tracker`` keys round-trip
    through the real JSON column without failing the insert.
    """
    from evnt.main import create_app

    app_id = f"it-lenient-{uuid.uuid4().hex[:8]}"
    payload = _payload(app_id)
    good = payload["data"][0]
    payload["data"] += [
        {"aid": app_id},  # no event type: dropped
        dict(
            good,
            p="headset",
            duid="legacy-cookie",
            vid="",
            ttm="1700000000123",
            url="https://example.com/deep?utm_source=x",
            e="ue",
            ue_pr=orjson.dumps(
                {
                    "schema": "iglu:com.snowplowanalytics.snowplow/unstruct_event/jsonschema/1-0-0",
                    "data": {
                        "schema": "iglu:com.snowplowanalytics.mobile/screen_view/jsonschema/1-0-0",
                        "data": {"name": "Home"},
                    },
                }
            ).decode(),
        ),
    ]
    with TestClient(create_app()) as client:
        response = client.post(settings.common.snowplow.endpoints.post_endpoint, json=payload)
    assert response.status_code == 204
    assert sorted(_rows_for(ch, database, app_id)) == [(app_id, "mob", "pv"), (app_id, "web", "pv")]
    row = ch.query(
        f"SELECT page, toString(time), extra.tracker.platform.:String, "
        f"extra.tracker.url.:String, toString(extra.tracker.true_tstamp), "
        f"arrayMap(e -> (e.stage.:String, e.field.:String), extra.validation.:`Array(JSON)`) "
        f"FROM {database}.local WHERE app_id = %(app_id)s AND platform = 'mob'",
        parameters={"app_id": app_id},
    ).first_row
    # ClickHouse may infer the timestamp string as DateTime64, hence the prefix check.
    page, time, platform, url, true_tstamp, validation = row
    assert (page, time, platform, url) == (
        "Home",
        "2023-11-14 22:13:20.123",
        "headset",
        "https://example.com/deep?utm_source=x",
    )
    assert true_tstamp.startswith("2023-11-14")
    assert true_tstamp.replace("T", " ").startswith("2023-11-14 22:13:20.123")
    # The screen view without ``id`` also fails its Iglu schema.
    assert validation == [("payload", "duid"), ("payload", "vid"), ("event", None)]


def test_context_entities_tracker_params_and_fingerprint_land_in_extra(tables, ch, database):
    """``extra.contexts``, ``extra.tracker`` overrides and the fingerprint round-trip.

    One entity carries data with a duplicate JSON path, which the ``JSON``
    column rejects as a nested object (see the next test); stored as text it
    must not cost the insert.
    """
    from evnt.main import create_app

    app_id = f"it-entities-{uuid.uuid4().hex[:8]}"
    payload = _payload(app_id)
    entities = [
        {
            "schema": "iglu:com.snowplowanalytics.mobile/screen_view/jsonschema/1-0-0",
            "data": {"name": "Home"},
        },
        {"schema": "iglu:com.acme/thing/jsonschema/1-0-0", "data": {"a.b": 1, "a": {"b": 2}}},
        {"schema": "iglu:com.acme/thing/jsonschema/1-0-0", "data": {"n": [1, "x", {"y": None}]}},
    ]
    payload["data"][0].update(
        p="srv",
        ip="203.0.113.9",
        ua="curl/8.0",
        tnuid="ecdff4d0-9175-40ac-a8bb-325c49733607",
        co=orjson.dumps(
            {
                "schema": "iglu:com.snowplowanalytics.snowplow/contexts/jsonschema/1-0-0",
                "data": entities,
            }
        ).decode(),
    )
    with TestClient(create_app()) as client:
        response = client.post(settings.common.snowplow.endpoints.post_endpoint, json=payload)
    assert response.status_code == 204
    assert _rows_for(ch, database, app_id) == [(app_id, "srv", "pv")]

    row = ch.query(
        "SELECT toString(user_ip), user_agent, "
        "extra.tracker.ip.:String, extra.tracker.network_userid.:String, "
        "extra.collector.client_ip.:String, "
        "arrayMap(e -> (e.version.:String, JSONExtractString(e.data.:String, 'name')), "
        "extra.contexts.`com.snowplowanalytics.mobile/screen_view`.:`Array(JSON)`), "
        "arrayMap(e -> e.data.:String, extra.contexts.`com.acme/thing`.:`Array(JSON)`), "
        "arrayMap(e -> e.schema.:String, extra.validation.:`Array(JSON)`), "
        "length(extra.event_fingerprint.:String) "
        f"FROM {database}.local WHERE app_id = %(app_id)s",
        parameters={"app_id": app_id},
    ).first_row
    ip, user_agent, sent_ip, network_userid, client_ip, screen_views, things, schemas, fp = row
    assert (ip, user_agent, sent_ip) == ("203.0.113.9", "curl/8.0", "203.0.113.9")
    assert network_userid == "ecdff4d0-9175-40ac-a8bb-325c49733607"
    # client_ip stays the connection's address; the override is in user_ip.
    assert client_ip != "203.0.113.9"
    assert screen_views == [("1-0-0", "Home")]
    assert [orjson.loads(data) for data in things] == [entity["data"] for entity in entities[1:]]
    assert schemas == [entities[0]["schema"]]
    assert fp == 32


def test_json_column_rejects_a_duplicate_path_as_nested_json(ch):
    """Why entity data is stored as text: nested, this value fails the insert."""
    from clickhouse_connect.driver.exceptions import DatabaseError

    with pytest.raises(DatabaseError, match="Duplicate path"):
        ch.query('SELECT CAST(\'{"a.b": 1, "a": {"b": 2}}\' AS JSON)')
