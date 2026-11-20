"""Context entities kept in ``extra.contexts``: unknown schemas and repeats.

Every test goes through ``process_data``, so it sees the row exactly as the
connector would receive it.
"""

import importlib
import json

import pytest

from evnt.tracker.element import COLLECTOR_OWNED_EXTRA_KEYS
from evnt.tracker.entities import MAX_STORED_ENTITIES, MAX_STORED_ENTITY_BYTES, StoredEntities
from evnt.tracker.iglu import MAX_IGLU_URI_CHARS, schema_key
from evnt.tracker.models import PayloadModel
from evnt.tracker.processing import process_data

payload_module = importlib.import_module("evnt.tracker.payload")

CONTEXTS_WRAPPER = "iglu:com.snowplowanalytics.snowplow/contexts/jsonschema/1-0-0"
SCREEN_VIEW_URI = "iglu:com.snowplowanalytics.mobile/screen_view/jsonschema/1-0-0"
SCREEN_URI = "iglu:com.snowplowanalytics.mobile/screen/jsonschema/1-0-0"
PRODUCT_URI = "iglu:com.snowplowanalytics.snowplow.ecommerce/product/jsonschema/1-0-0"
ACME_URI = "iglu:com.acme/checkout_step/jsonschema/2-1-0"
STATIC_URI = "iglu:com.acme/static_context/jsonschema/1-0-0"
SCREEN_ID = "9b2c3d4e-5f60-4718-8a9b-0c1d2e3f4a5b"


def _event(*entities, **fields) -> dict:
    co = json.dumps({"schema": CONTEXTS_WRAPPER, "data": list(entities)})
    return {"e": "pv", "aid": "example-app", "p": "mob", "tv": "andr-6.0.0", "co": co, **fields}


async def _row(*entities, **fields) -> dict:
    rows = await process_data(PayloadModel(data=[_event(*entities, **fields)]), "", "1.2.3.4", None)
    assert len(rows) == 1
    return rows[0]


def _stored(row: dict, key: str) -> list[tuple[str, object]]:
    return [(e["version"], json.loads(e["data"])) for e in row["extra"]["contexts"][key]]


@pytest.mark.anyio
async def test_unknown_entities_are_kept_in_arrival_order_with_their_version():
    row = await _row(
        {"schema": PRODUCT_URI, "data": {"id": "sku-1", "price": 10}},
        {"schema": ACME_URI, "data": {"step": 2}},
        {"schema": PRODUCT_URI, "data": {"id": "sku-2", "price": "n/a"}},
    )

    assert _stored(row, "com.snowplowanalytics.snowplow.ecommerce/product") == [
        ("1-0-0", {"id": "sku-1", "price": 10}),
        ("1-0-0", {"id": "sku-2", "price": "n/a"}),
    ]
    assert _stored(row, "com.acme/checkout_step") == [("2-1-0", {"step": 2})]
    # ``data`` is JSON text: ClickHouse rejects some nested JSON as a whole insert.
    entry = row["extra"]["contexts"]["com.acme/checkout_step"][0]
    assert entry == {"version": "2-1-0", "data": '{"step":2}'}


@pytest.mark.anyio
async def test_data_clickhouse_would_reject_as_nested_json_is_kept_as_text():
    duplicate_path = {"a.b": 1, "a": {"b": 2}}
    row = await _row({"schema": ACME_URI, "data": duplicate_path})

    assert _stored(row, "com.acme/checkout_step") == [("2-1-0", duplicate_path)]


@pytest.mark.anyio
async def test_android_screen_view_entity_is_kept_with_its_validation_error():
    # Production case: an Android app attaches screen_view as an entity
    # without the ``id`` its schema requires, on most events.
    row = await _row({"schema": SCREEN_VIEW_URI, "data": {"name": "Home"}})

    assert _stored(row, "com.snowplowanalytics.mobile/screen_view") == [("1-0-0", {"name": "Home"})]
    (entry,) = row["extra"]["validation"]
    assert entry["stage"] == "contexts"
    assert entry["schema"] == SCREEN_VIEW_URI
    assert entry["error"].startswith('{"name":"Home"}')
    # Not a self-describing event: the row stays a page view of its own URL.
    assert row["e"] == "pv"


@pytest.mark.anyio
async def test_unknown_entities_log_no_warning_per_event(monkeypatch):
    lines: list[str] = []

    class Logger:
        def debug(self, *args, **kwargs) -> None:
            pass

        def warning(self, event, **kwargs) -> None:
            lines.append(event)

    monkeypatch.setattr(payload_module, "logger", Logger())
    for _ in range(50):
        await _row({"schema": SCREEN_VIEW_URI, "data": {"name": "Home"}})

    # Only the rate-limited validation line, once per schema and interval.
    assert lines == ["Iglu validation warning"]


@pytest.mark.anyio
async def test_repeats_of_a_handled_schema_keep_the_columns_and_the_entities():
    first = {"name": "Home", "id": SCREEN_ID, "type": "feed"}
    second = {"name": "Details", "id": SCREEN_ID, "activity": "Main"}
    row = await _row({"schema": SCREEN_URI, "data": first}, {"schema": SCREEN_URI, "data": second})

    # The columns are what the merging handler always produced ...
    assert row["url"] == "Details"
    assert row["screen"] == {"type": "feed", "activity": "Main"}
    # ... and the repeat is also kept whole, as it was sent.
    assert _stored(row, "com.snowplowanalytics.mobile/screen") == [("1-0-0", second)]


@pytest.mark.anyio
async def test_a_single_handled_entity_is_not_duplicated_into_extra():
    row = await _row({"schema": SCREEN_URI, "data": {"name": "Home", "id": SCREEN_ID}})

    assert row["url"] == "Home"
    assert "contexts" not in row["extra"]


@pytest.mark.anyio
async def test_schema_that_is_not_an_iglu_uri_is_recorded_not_stored():
    row = await _row(
        {"schema": "com.acme/checkout_step", "data": {"step": 1}},
        {"schema": "iglu:com.acme/checkout_step/jsonschema/1-0", "data": {"step": 2}},
    )

    assert "contexts" not in row["extra"]
    assert [(e["stage"], e["error"]) for e in row["extra"]["validation"]] == [
        ("contexts", "schema is not an Iglu URI"),
        ("contexts", "schema is not an Iglu URI"),
    ]


@pytest.mark.anyio
async def test_non_object_data_is_kept_and_recorded():
    row = await _row({"schema": ACME_URI, "data": [1, 2]})

    assert _stored(row, "com.acme/checkout_step") == [("2-1-0", [1, 2])]
    assert row["extra"]["validation"][0]["error"] == "context data is not an object"


@pytest.mark.anyio
async def test_entities_over_the_count_limit_are_counted_once():
    entities = [{"schema": ACME_URI, "data": {"i": i}} for i in range(MAX_STORED_ENTITIES + 5)]
    row = await _row(*entities)

    stored = row["extra"]["contexts"]["com.acme/checkout_step"]
    assert len(stored) == MAX_STORED_ENTITIES
    assert json.loads(stored[-1]["data"]) == {"i": MAX_STORED_ENTITIES - 1}
    (entry,) = row["extra"]["validation"]
    assert entry["stage"] == "contexts"
    assert entry["error"].startswith("5 context entities not stored")


@pytest.mark.anyio
async def test_an_oversized_entity_costs_only_itself():
    big = {"blob": "x" * MAX_STORED_ENTITY_BYTES}
    row = await _row(
        {"schema": ACME_URI, "data": {"i": 1}},
        {"schema": ACME_URI, "data": big},
        {"schema": ACME_URI, "data": {"i": 2}},
    )

    assert _stored(row, "com.acme/checkout_step") == [("2-1-0", {"i": 1}), ("2-1-0", {"i": 2})]
    assert row["extra"]["validation"][0]["error"].startswith("1 context entities not stored")


@pytest.mark.anyio
async def test_the_size_limit_is_cumulative_across_the_entities_of_an_event():
    # Each blob encodes to just under half the limit: two fit, a third does
    # not, and a small entity after it still fits in what is left.
    blob = {"blob": "x" * (MAX_STORED_ENTITY_BYTES // 2 - 100)}
    row = await _row(
        {"schema": ACME_URI, "data": blob},
        {"schema": ACME_URI, "data": blob},
        {"schema": ACME_URI, "data": blob},
        {"schema": ACME_URI, "data": {"i": 1}},
    )

    assert _stored(row, "com.acme/checkout_step") == [
        ("2-1-0", blob),
        ("2-1-0", blob),
        ("2-1-0", {"i": 1}),
    ]
    (entry,) = row["extra"]["validation"]
    assert entry["error"].startswith("1 context entities not stored")


@pytest.mark.anyio
async def test_a_payload_cannot_set_the_collector_owned_keys():
    spoof = {"contexts": {"x/y": "spoofed"}, "event_fingerprint": "spoofed", "kept": True}
    row = await _row({"schema": "iglu:com.acme/static_context/jsonschema/1-0-0", "data": spoof})

    assert row["extra"]["kept"] is True
    assert "contexts" not in row["extra"]
    assert row["extra"]["event_fingerprint"] != "spoofed"


@pytest.mark.anyio
async def test_dotted_keys_cannot_reach_collector_owned_paths():
    # ClickHouse splits JSON keys at dots: "tracker.ip" is extra.tracker.ip.
    spoof = {
        "tracker.ip": "6.6.6.6",
        "contexts.com.acme/checkout_step": [{"version": "9-9-9", "data": "{}"}],
        "collector.client_ip": "6.6.6.6",
        "event_fingerprint.x": 1,
        "validation.0": 1,
        "trackers.kept": 1,
        "kept.tracker": 1,
    }
    row = await _row(
        {"schema": STATIC_URI, "data": spoof},
        # Collides with the spoofed contexts path: ClickHouse would reject the
        # whole insert as a duplicate path if the dotted key were merged.
        {"schema": ACME_URI, "data": {"i": 1}},
    )

    extra = row["extra"]
    assert extra["trackers.kept"] == 1
    assert extra["kept.tracker"] == 1
    assert not [
        key for key in extra if "." in key and key.partition(".")[0] in COLLECTOR_OWNED_EXTRA_KEYS
    ]
    assert _stored(row, "com.acme/checkout_step") == [("2-1-0", {"i": 1})]
    assert extra["collector"]["client_ip"] == "1.2.3.4"
    (entry,) = extra["validation"]
    assert entry["stage"] == "contexts"
    assert entry["schema"] == STATIC_URI
    assert entry["error"].startswith(
        "5 keys on collector-owned extra paths not merged: 'tracker.ip'"
    )


@pytest.mark.anyio
async def test_a_repeated_static_context_is_kept_whole_in_contexts():
    row = await _row(
        {"schema": STATIC_URI, "data": {"first": 1}},
        {"schema": STATIC_URI, "data": {"tracker.ip": "6.6.6.6", "second": 2}},
    )

    assert row["extra"]["first"] == 1
    assert row["extra"]["second"] == 2
    assert "tracker.ip" not in row["extra"]
    # As JSON text the repeat cannot land on a collector-owned path.
    assert _stored(row, "com.acme/static_context") == [
        ("1-0-0", {"tracker.ip": "6.6.6.6", "second": 2})
    ]


def test_schema_key_parses_iglu_uris_strictly():
    assert schema_key(PRODUCT_URI) == ("com.snowplowanalytics.snowplow.ecommerce/product", "1-0-0")
    assert schema_key("iglu:com.acme/x/jsonschema-lite/10-2-33") == ("com.acme/x", "10-2-33")
    for bad in (
        "",
        "com.acme/x/jsonschema/1-0-0",
        "iglu:com.acme/x/jsonschema/1-0",
        "iglu:com.acme/x.y/jsonschema/1-0-0",
        "iglu:com.acme/x/jsonschema/1-0-0/extra",
        "iglu:com.acme/x/jsonschema/1-0-0\n",
        f"iglu:com.acme/{'x' * MAX_IGLU_URI_CHARS}/jsonschema/1-0-0",
    ):
        assert schema_key(bad) is None, bad


def test_stored_entities_keep_a_value_orjson_cannot_encode_as_its_repr():
    stored = StoredEntities()

    assert stored.add("com.acme/x", "1-0-0", {"big": 2**70})
    assert json.loads(stored.by_schema["com.acme/x"][0]["data"]) == repr({"big": 2**70})
