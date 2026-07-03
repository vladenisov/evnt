from uuid import UUID

import pytest
from routers.tracker.db.clickhouse.connector import ClickHouseConnector
from routers.tracker.db.clickhouse.schemas import register_fields
from routers.tracker.db.clickhouse.schemas.snowplow import (
    STRING,
    UUID as CLICKHOUSE_UUID,
    ColumnDef,
    TupleColumnDef,
)


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def insert(
        self,
        table_name,
        data,
        column_names,
        column_types,
        settings,
    ):
        self.calls.append(
            {
                "table_name": table_name,
                "data": data,
                "column_names": column_names,
                "column_types": column_types,
                "settings": settings,
            },
        )


def _tables():
    return {
        "test_events": {
            "local": {"name": "events_local"},
            "distributed": {"name": "events_distributed"},
        },
    }


register_fields(
    "test_events",
    [
        ColumnDef(payload_name="foo", name="foo", type=STRING),
        ColumnDef(payload_name="bar", name="bar", type=STRING),
    ],
)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_insert_rows_issues_single_batch_request(anyio_backend):
    client = _FakeClient()
    connector = ClickHouseConnector(
        client,
        database="evnt",
        tables=_tables(),
    )

    await connector.insert_rows(
        [{"foo": "a", "bar": "1"}, {"foo": "b", "bar": "2"}],
        table_group="test_events",
    )

    assert len(client.calls) == 1
    assert client.calls[0]["data"] == [["a", "1"], ["b", "2"]]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_insert_rows_noop_for_empty_batch(anyio_backend):
    client = _FakeClient()
    connector = ClickHouseConnector(
        client,
        database="evnt",
        tables=_tables(),
    )

    await connector.insert_rows([], table_group="test_events")

    assert client.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_insert_batch_sends_single_clickhouse_insert(anyio_backend):
    client = _FakeClient()
    connector = ClickHouseConnector(
        client,
        database="evnt",
        tables=_tables(),
    )

    await connector.insert_batch(
        [{"foo": "a", "bar": "1"}, {"foo": "b", "bar": "2"}],
        table_group="test_events",
    )

    assert len(client.calls) == 1
    assert client.calls[0]["table_name"] == "evnt.events_local"
    assert client.calls[0]["data"] == [["a", "1"], ["b", "2"]]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_insert_batch_reuses_cached_insert_metadata(anyio_backend):
    register_fields(
        "test_cached_events",
        [
            ColumnDef(payload_name="foo", name="foo", type=STRING),
        ],
    )
    client = _FakeClient()
    connector = ClickHouseConnector(
        client,
        database="evnt",
        tables={
            "test_cached_events": {
                "local": {"name": "cached_events_local"},
                "distributed": {"name": "cached_events_distributed"},
            },
        },
    )

    await connector.insert_batch([{"foo": "a"}], table_group="test_cached_events")

    register_fields(
        "test_cached_events",
        [
            ColumnDef(payload_name="bar", name="bar", type=STRING),
        ],
    )
    await connector.insert_batch(
        [{"foo": "b", "bar": "should-not-be-used"}],
        table_group="test_cached_events",
    )

    assert len(client.calls) == 2
    assert client.calls[0]["column_names"] == ["foo"]
    assert client.calls[0]["data"] == [["a"]]
    assert client.calls[1]["column_names"] == ["foo"]
    assert client.calls[1]["data"] == [["b"]]


register_fields(
    "test_tuple_events",
    [
        ColumnDef(payload_name="foo", name="foo", type=STRING),
        TupleColumnDef(
            name="resolution",
            elements=(
                ColumnDef(payload_name="res", name="browser", type=STRING),
                ColumnDef(payload_name="vp", name="viewport", type=STRING),
                ColumnDef(payload_name="ds", name="page", type=STRING),
            ),
        ),
    ],
)


register_fields(
    "test_uuid_events",
    [
        ColumnDef(payload_name="eid", name="event_id", type=CLICKHOUSE_UUID),
    ],
)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_insert_batch_sanitizes_none_for_string_columns(anyio_backend):
    client = _FakeClient()
    connector = ClickHouseConnector(
        client,
        database="evnt",
        tables={
            "test_tuple_events": {
                "local": {"name": "tuple_events_local"},
                "distributed": {"name": "tuple_events_distributed"},
            },
        },
    )

    await connector.insert_batch(
        [{"foo": None, "res": None, "vp": "1280x720", "ds": None}],
        table_group="test_tuple_events",
    )

    assert len(client.calls) == 1
    assert client.calls[0]["table_name"] == "evnt.tuple_events_local"
    assert client.calls[0]["column_names"] == ["foo", "resolution"]
    assert client.calls[0]["data"] == [["", ("", "1280x720", "")]]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_insert_batch_converts_uuid_strings_from_json_queue(anyio_backend):
    client = _FakeClient()
    connector = ClickHouseConnector(
        client,
        database="evnt",
        tables={
            "test_uuid_events": {
                "local": {"name": "uuid_events_local"},
                "distributed": {"name": "uuid_events_distributed"},
            },
        },
    )

    await connector.insert_batch(
        [
            {"eid": None},
            {"eid": "94eb9eca-a77f-4c08-b90c-1260efde3cc5"},
        ],
        table_group="test_uuid_events",
    )

    assert client.calls[0]["data"] == [
        [UUID("00000000-0000-0000-0000-000000000000")],
        [UUID("94eb9eca-a77f-4c08-b90c-1260efde3cc5")],
    ]
