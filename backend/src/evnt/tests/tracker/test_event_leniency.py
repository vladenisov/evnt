"""Per-event leniency: what a malformed event still stores, and what it costs.

Every test goes through ``process_data`` so it sees the row exactly as the
connector would receive it, including the collector-owned ``extra`` keys.
"""

import base64
import importlib
import json
from datetime import UTC, datetime

import pytest

from evnt.observability.metrics import EVENTS_DROPPED
from evnt.tracker.element import (
    MAX_QUOTED_VALUE_CHARS,
    MAX_RAW_PLATFORM_CHARS,
    MAX_VALIDATION_ENTRIES,
    MAX_VALIDATION_ERROR_CHARS,
    MAX_VALIDATION_NAME_CHARS,
    EventIssues,
    build_element,
)
from evnt.tracker.iglu import ValidationResult
from evnt.tracker.models import PayloadModel
from evnt.tracker.processing import process_data

payload_module = importlib.import_module("evnt.tracker.payload")
processing_module = importlib.import_module("evnt.tracker.processing")

NIL_UUID = "00000000-0000-0000-0000-000000000000"
SESSION_URI = "iglu:com.snowplowanalytics.snowplow/client_session/jsonschema/1-0-2"
SCREEN_VIEW_URI = "iglu:com.snowplowanalytics.mobile/screen_view/jsonschema/1-0-0"
UE_WRAPPER = "iglu:com.snowplowanalytics.snowplow/unstruct_event/jsonschema/1-0-0"
CONTEXTS_WRAPPER = "iglu:com.snowplowanalytics.snowplow/contexts/jsonschema/1-0-0"
AMP_ENTITY = {
    "schema": "iglu:dev.amp.snowplow/amp_id/jsonschema/1-0-0",
    "data": {"ampClientId": "amp-abc"},
}


def _event(**fields) -> dict:
    return {"e": "pv", "aid": "example-app", "p": "web", "tv": "js-3.0.0", "res": "1x1", **fields}


def _contexts(*entities) -> str:
    return json.dumps({"schema": CONTEXTS_WRAPPER, "data": list(entities)})


def _ue(data) -> str:
    return json.dumps({"schema": UE_WRAPPER, "data": data})


async def _rows(*events) -> list[dict]:
    return await process_data(PayloadModel(data=list(events)), "", "198.51.100.7", None)


async def _row(**fields) -> dict:
    rows = await _rows(_event(**fields))
    assert len(rows) == 1
    return rows[0]


def _fields_with_errors(row: dict) -> list[str]:
    return [entry.get("field") for entry in row["extra"].get("validation", [])]


def _stages(row: dict) -> list[str]:
    return [entry["stage"] for entry in row["extra"].get("validation", [])]


def _dropped(reason: str) -> float:
    return EVENTS_DROPPED.labels(reason=reason)._value.get()


@pytest.fixture(autouse=True)
def _iglu_skipped(monkeypatch):
    """Keep the tests independent of the vendored Iglu schemas."""
    monkeypatch.setattr(
        payload_module,
        "validate_iglu_payload",
        lambda schema_uri, data: ValidationResult(status="skipped"),
    )


# --- one bad event in a batch ------------------------------------------------


@pytest.mark.anyio
async def test_unbuildable_events_are_dropped_and_counted_and_the_rest_stored():
    before = _dropped("invalid_event_type"), _dropped("not_an_object")
    rows = await _rows(
        _event(aid="first"),
        {"aid": "no event type"},
        _event(e="bogus"),
        ["not", "an", "object"],
        _event(aid="last"),
    )

    assert [row["aid"] for row in rows] == ["first", "last"]
    assert _dropped("invalid_event_type") == before[0] + 2
    assert _dropped("not_an_object") == before[1] + 1


@pytest.mark.anyio
async def test_valid_event_has_no_collector_issue_keys():
    row = await _row()
    assert "validation" not in row["extra"]
    assert "tracker" not in row["extra"]


@pytest.mark.anyio
async def test_buildable_invalid_event_is_stored_with_its_errors():
    row = await _row(
        duid="legacy-cookie-id",
        sid="not-a-session",
        vid="",
        cd="deep",
        cookie="yes",
        pp_mix="1.5",
        dtm="not a time",
        eid="not-an-eid",
        se_va=[1],
    )

    assert row["duid"] is None
    assert row["sid"] is None
    assert row["vid"] == 0
    assert row["eid"] is not None
    assert row["dtm"].tzinfo is not None
    assert row["se_va"] == 0.0
    assert set(_fields_with_errors(row)) == {
        "duid",
        "sid",
        "vid",
        "cd",
        "cookie",
        "pp_mix",
        "dtm",
        "eid",
        "se_va",
    }
    duid_error = next(e for e in row["extra"]["validation"] if e["field"] == "duid")
    assert duid_error["stage"] == "payload"
    # The rejected value is quoted, so a custom id is not lost entirely.
    assert "'legacy-cookie-id'" in duid_error["error"]


@pytest.mark.anyio
async def test_valid_values_are_coerced_exactly_as_before():
    row = await _row(vid="3", cd="24", dtm="1700000000123", cookie="1")

    assert row["vid"] == 3
    assert row["dtm"] == datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC)
    assert "validation" not in row["extra"]


@pytest.mark.anyio
async def test_out_of_range_visit_count_falls_back_instead_of_failing_the_insert():
    row = await _row(vid="-1")
    assert row["vid"] == 0
    assert _fields_with_errors(row) == ["vid"]


@pytest.mark.anyio
async def test_validation_entries_and_their_text_are_bounded():
    entities = ["not an object"] * (MAX_VALIDATION_ENTRIES + 10)
    row = await _row(co=_contexts(*entities), duid="x" * 5000)

    validation = row["extra"]["validation"]
    assert len(validation) == MAX_VALIDATION_ENTRIES
    assert all(len(entry["error"]) <= MAX_VALIDATION_ERROR_CHARS for entry in validation)


@pytest.mark.anyio
async def test_quoted_values_in_errors_are_bounded():
    row = await _row(duid="x" * 5000)
    (entry,) = row["extra"]["validation"]
    # The error text has its own, larger cap; the quoted value must be cut
    # well before it so the message itself stays readable.
    assert "x" * MAX_QUOTED_VALUE_CHARS not in entry["error"]


@pytest.mark.anyio
async def test_raw_platform_kept_in_extra_is_bounded():
    row = await _row(p="x" * 5000)
    assert row["p"] == "app"
    assert len(row["extra"]["tracker"]["platform"]) <= MAX_RAW_PLATFORM_CHARS


@pytest.mark.anyio
async def test_event_schema_name_kept_in_validation_is_bounded():
    row = await _row(e="ue", ue_pr=_ue({"schema": "x" * 5000, "data": {}}))
    (entry,) = row["extra"]["validation"]
    assert entry["stage"] == "event"
    assert entry["schema"].startswith("xxx")
    assert len(entry["schema"]) <= MAX_VALIDATION_NAME_CHARS


@pytest.mark.anyio
async def test_context_schema_name_kept_in_validation_is_bounded(monkeypatch):
    monkeypatch.setattr(
        payload_module,
        "validate_iglu_payload",
        lambda schema_uri, data: ValidationResult(status="warning", error="bad", invalid_data=True),
    )
    schema = f"iglu:com.acme/{'x' * 5000}/jsonschema/1-0-0"
    row = await _row(co=_contexts({"schema": schema, "data": {}}))
    # The failed validation, then the URI too long to be kept in extra.contexts.
    entries = row["extra"]["validation"]
    assert [entry["error"] for entry in entries] == ["bad", "schema is not an Iglu URI"]
    for entry in entries:
        assert entry["stage"] == "contexts"
        assert entry["schema"].startswith("iglu:com.acme/xxx")
        assert len(entry["schema"]) <= MAX_VALIDATION_NAME_CHARS
    assert "contexts" not in row["extra"]


@pytest.mark.anyio
async def test_payload_cannot_forge_the_collector_issue_keys():
    static = {
        "schema": "iglu:com.acme/static_context/jsonschema/1-0-0",
        "data": {"validation": "forged", "tracker": {"ip": "forged"}},
    }
    row = await _row(co=_contexts(static))

    # The only validation entry is the collector's own note of the stripped keys.
    assert row["extra"]["validation"] == [
        {
            "stage": "contexts",
            "schema": static["schema"],
            "error": "2 keys on collector-owned extra paths not merged: 'validation', 'tracker'",
        }
    ]
    assert "tracker" not in row["extra"]


# --- field strictness ----------------------------------------------------------


@pytest.mark.anyio
async def test_missing_aid_is_stored_as_empty():
    event = _event()
    del event["aid"]
    rows = await _rows(event)
    assert rows[0]["aid"] == ""
    assert "validation" not in rows[0]["extra"]


@pytest.mark.anyio
async def test_visionos_event_without_res_is_stored():
    event = _event(p="mob", tv="ios-6.0.0")
    del event["res"]
    rows = await _rows(event)
    assert rows[0]["res"] == ""
    assert "validation" not in rows[0]["extra"]


@pytest.mark.anyio
async def test_headset_platform_is_stored_as_mob_with_the_code_kept():
    row = await _row(p="headset")
    assert row["p"] == "mob"
    assert row["extra"]["tracker"] == {"platform": "headset"}
    assert "validation" not in row["extra"]


@pytest.mark.anyio
async def test_unknown_platform_is_stored_as_app_and_reported():
    row = await _row(p="watch")
    assert row["p"] == "app"
    assert row["extra"]["tracker"]["platform"] == "watch"
    assert _fields_with_errors(row) == ["p"]


@pytest.mark.anyio
async def test_missing_platform_and_tracker_version_fall_back():
    event = _event()
    del event["p"], event["tv"]
    rows = await _rows(event)
    assert rows[0]["p"] == "app"
    assert rows[0]["tv"] == ""
    assert sorted(_fields_with_errors(rows[0])) == ["p", "tv"]
    assert "tracker" not in rows[0]["extra"]


@pytest.mark.anyio
async def test_non_string_url_is_a_field_error_not_a_crash():
    row = await _row(url=123, refr=["x"])
    assert row["url"] == ""
    assert sorted(_fields_with_errors(row)) == ["refr", "url"]


@pytest.mark.anyio
async def test_nil_uuid_atomic_ids_are_absent():
    row = await _row(duid=NIL_UUID, sid=NIL_UUID)
    assert row["duid"] is None
    assert row["sid"] is None
    assert "validation" not in row["extra"]


def test_build_element_takes_a_single_validation_for_valid_input(monkeypatch):
    issues = EventIssues()
    element = build_element(_event(), issues)
    assert element is not None
    assert issues.validation is None
    assert issues.tracker is None


# --- true timestamp --------------------------------------------------------------


@pytest.mark.anyio
async def test_ttm_without_dtm_becomes_the_event_time():
    row = await _row(ttm="1700000000123")
    expected = datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC)
    assert row["dtm"] == expected
    assert row["extra"]["tracker"] == {"true_tstamp": "2023-11-14T22:13:20.123Z"}


@pytest.mark.anyio
async def test_ttm_with_dtm_keeps_dtm_and_stores_ttm():
    row = await _row(dtm="1700000000000", ttm="1600000000000")
    assert row["dtm"] == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert row["extra"]["tracker"]["true_tstamp"] == "2020-09-13T12:26:40.000Z"


@pytest.mark.anyio
async def test_ttm_replaces_an_invalid_dtm():
    row = await _row(dtm="garbage", ttm="1700000000000")
    assert row["dtm"] == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert _fields_with_errors(row) == ["dtm"]


@pytest.mark.anyio
async def test_invalid_ttm_is_reported_and_ignored():
    row = await _row(ttm="soon")
    assert _fields_with_errors(row) == ["ttm"]
    assert "tracker" not in row["extra"]


# --- mobile screen views ---------------------------------------------------------


def _screen_view(**data) -> dict:
    return {"schema": SCREEN_VIEW_URI, "data": data}


@pytest.mark.anyio
async def test_screen_view_without_id_is_stored():
    row = await _row(e="ue", p="mob", ue_pr=_ue(_screen_view(name="Home")))
    assert row["e"] == "pv"
    assert row["url"] == "Home"
    assert str(row["view_id"]) == NIL_UUID


@pytest.mark.anyio
async def test_screen_view_without_name_keeps_the_url():
    row = await _row(e="ue", p="mob", url="app://home", ue_pr=_ue(_screen_view(id=NIL_UUID)))
    assert row["e"] == "pv"
    assert row["url"] == "app://home"
    assert "tracker" not in row["extra"]


@pytest.mark.anyio
async def test_deep_link_url_and_referrer_are_kept_when_the_screen_name_replaces_them():
    row = await _row(
        e="ue",
        p="mob",
        url="https://example.com/promo?utm_source=mail",
        refr="https://mail.example.com/",
        ue_pr=_ue(_screen_view(id=NIL_UUID, name="Promo", previousName="Home")),
    )

    assert row["url"] == "Promo"
    assert row["refr"] == "Home"
    assert row["extra"]["tracker"] == {
        "url": "https://example.com/promo?utm_source=mail",
        "refr": "https://mail.example.com/",
    }


@pytest.mark.anyio
async def test_screen_view_with_non_string_name_is_not_stored_in_url():
    row = await _row(e="ue", p="mob", ue_pr=_ue(_screen_view(name=5, previousName=["x"])))
    assert row["url"] == ""
    assert row["refr"] == ""


@pytest.mark.anyio
async def test_screen_view_data_that_is_not_an_object_stays_in_ue():
    row = await _row(e="ue", p="mob", ue_pr=_ue({"schema": SCREEN_VIEW_URI, "data": [1]}))
    assert row["e"] == "ue"
    assert row["ue"] == {"screen_view": [1]}


# --- anonymous mobile users ------------------------------------------------------


@pytest.mark.anyio
async def test_nil_client_session_ids_do_not_become_or_replace_ids():
    duid = "11111111-1111-4111-8111-111111111111"
    session = {
        "schema": SESSION_URI,
        "data": {
            "userId": NIL_UUID,
            "sessionId": NIL_UUID,
            "previousSessionId": NIL_UUID,
            "sessionIndex": 2,
        },
    }
    row = await _row(duid=duid, co=_contexts(session))

    assert str(row["duid"]) == duid
    assert row["sid"] is None
    assert str(row["previous_session_id"]) == NIL_UUID
    assert row["vid"] == 2


@pytest.mark.anyio
async def test_anonymous_mobile_user_has_no_device_id():
    session = {"schema": SESSION_URI, "data": {"userId": NIL_UUID, "sessionIndex": 1}}
    row = await _row(p="mob", co=_contexts(session))
    assert row["duid"] is None


# --- crash paths on malformed input ----------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fields", "stage"),
    [
        ({"e": "ue", "ue_pr": json.dumps({"data": {"data": {}}})}, "event"),
        ({"e": "ue", "ue_pr": json.dumps({"data": "text"})}, "event"),
        ({"e": "ue", "ue_pr": json.dumps({"data": {"schema": 7, "data": {}}})}, "event"),
        ({"e": "ue", "ue_pr": _ue({"schema": "no-slashes", "data": {}})}, "event"),
        ({"co": json.dumps({"data": 5})}, "contexts"),
        ({"co": json.dumps({"data": "text"})}, "contexts"),
        ({"co": _contexts({"schema": 1, "data": {}})}, "contexts"),
        ({"co": _contexts({"data": {}})}, "contexts"),
        ({"cx": "%%% not base64 %%%"}, "payload"),
        ({"cx": base64.urlsafe_b64encode(b"\xff\xfe").decode()}, "payload"),
        ({"ue_pr": "[1, 2]"}, "payload"),
    ],
)
async def test_malformed_event_parts_are_reported_not_raised(fields, stage):
    row = await _row(**fields)
    assert [entry["stage"] for entry in row["extra"]["validation"]] == [stage]


@pytest.mark.anyio
async def test_structured_event_with_invalid_data_is_reported(monkeypatch):
    u2s = payload_module.schemas.u2s_data
    row = await _row(e="ue", ue_pr=_ue({"schema": u2s, "data": {"action": {"x": 1}}}))
    assert row["e"] == "ue"
    assert row["extra"]["validation"][0]["stage"] == "event"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "entity",
    [
        {
            "schema": "iglu:com.snowplowanalytics.snowplow/mobile_context/jsonschema/1-0-3",
            "data": {"deviceManufacturer": 1, "deviceModel": {}, "osType": [], "osVersion": 2},
        },
        {
            "schema": SESSION_URI,
            "data": {"sessionIndex": "7", "eventIndex": -1, "storageMechanism": 5},
        },
        {
            "schema": "iglu:com.snowplowanalytics.mobile/screen/jsonschema/1-0-0",
            "data": {"name": 3},
        },
        {"schema": "iglu:org.ietf/http_client_hints/jsonschema/1-0-0", "data": {"isMobile": "yes"}},
        {"schema": "iglu:com.acme/static_context/jsonschema/1-0-0", "data": {"client_hints": 1}},
        {"schema": "iglu:dev.amp.snowplow/amp_id/jsonschema/1-0-0", "data": {"uid": "x"}},
        {
            "schema": "iglu:dev.amp.snowplow/amp_id/jsonschema/1-0-0",
            "data": {"uid": "x", "userId": 5, "domainUserid": NIL_UUID},
        },
    ],
)
async def test_context_values_of_the_wrong_type_never_reach_typed_columns(entity):
    row = await _row(co=_contexts(entity))

    for column in ("device_brand", "device_model", "os_family", "os_version_string", "url"):
        assert isinstance(row[column], str)
    assert isinstance(row["storage_mechanism"], str)
    assert isinstance(row["uid"], str)
    assert isinstance(row["vid"], int)
    assert row["event_index"] >= 0
    assert row["duid"] is None
    # The generic per-event fallback also yields typed columns, so the handler
    # must have dealt with the value itself: no ``processing`` entry.
    assert "processing" not in _stages(row)


@pytest.mark.anyio
async def test_amp_linker_on_an_unparsable_url_is_ignored():
    row = await _row(url="http://[::1/?sp_amp_linker=1*abc", co=_contexts(AMP_ENTITY))
    assert row["amp"] == AMP_ENTITY["data"]
    assert "processing" not in _stages(row)


MALFORMED_IDS = [12345, [1], {"id": 1}]
USER_ID = "4f6f5f73-0d8c-4b4b-9a36-3c4b3c5b6a7d"
SESSION_ID = "9a0e1f2b-6c3d-4e5f-8a9b-0c1d2e3f4a5b"
MOBILE_CONTEXT = {
    "schema": "iglu:com.snowplowanalytics.snowplow/mobile_context/jsonschema/1-0-3",
    "data": {"deviceManufacturer": "Acme", "osType": "ios", "osVersion": "17"},
}


def _session(**data) -> dict:
    return {
        "schema": SESSION_URI,
        "data": {"userId": USER_ID, "sessionId": SESSION_ID, "sessionIndex": 3, **data},
    }


def _web_page(bad) -> dict:
    return {
        "co": _contexts(
            {
                "schema": "iglu:com.snowplowanalytics.snowplow/web_page/jsonschema/1-0-0",
                "data": {"id": bad},
            },
            _session(),
            MOBILE_CONTEXT,
        )
    }


def _mobile_screen(bad) -> dict:
    return {
        "co": _contexts(
            {
                "schema": "iglu:com.snowplowanalytics.mobile/screen/jsonschema/1-0-0",
                "data": {"id": bad, "name": "Home"},
            },
            _session(),
            MOBILE_CONTEXT,
        )
    }


def _session_field(field: str):
    def build(bad) -> dict:
        return {"co": _contexts(_session(**{field: bad}), MOBILE_CONTEXT)}

    return build


def _amp_domain_userid(bad) -> dict:
    amp = {"schema": AMP_ENTITY["schema"], "data": {"domainUserid": bad}}
    return {"co": _contexts(amp, _session(), MOBILE_CONTEXT)}


def _screen_view_event(bad) -> dict:
    return {
        "e": "ue",
        "p": "mob",
        "ue_pr": _ue({"schema": SCREEN_VIEW_URI, "data": {"id": bad, "name": "Home"}}),
        "co": _contexts(_session(), MOBILE_CONTEXT),
    }


@pytest.mark.anyio
@pytest.mark.parametrize("bad", MALFORMED_IDS, ids=["int", "list", "dict"])
@pytest.mark.parametrize(
    "build",
    [
        _web_page,
        _mobile_screen,
        _session_field("firstEventId"),
        _session_field("previousSessionId"),
        _amp_domain_userid,
        _screen_view_event,
    ],
    ids=["web_page", "screen", "first_event_id", "previous_session_id", "amp", "screen_view"],
)
async def test_non_string_ids_fall_back_without_losing_the_other_contexts(build, bad):
    row = await _row(**build(bad))

    assert "processing" not in _stages(row)
    assert str(row["duid"]) == USER_ID
    assert str(row["sid"]) == SESSION_ID
    assert row["vid"] == 3
    assert row["device_brand"] == "Acme"
    if build in (_mobile_screen, _screen_view_event):
        assert row["url"] == "Home"
    if build is _screen_view_event:
        assert row["e"] == "pv"


@pytest.mark.anyio
@pytest.mark.parametrize("bad", MALFORMED_IDS, ids=["int", "list", "dict"])
@pytest.mark.parametrize("field", ["sessionId", "userId"])
async def test_non_string_session_or_user_id_is_absent(field, bad):
    row = await _row(co=_contexts(_session(**{field: bad}), MOBILE_CONTEXT))

    assert "processing" not in _stages(row)
    assert row["vid"] == 3
    assert row["device_brand"] == "Acme"
    column, other, other_id = (
        ("sid", "duid", USER_ID) if field == "sessionId" else ("duid", "sid", SESSION_ID)
    )
    assert row[column] is None
    assert str(row[other]) == other_id


@pytest.mark.anyio
async def test_structured_value_beyond_float_range_is_kept_as_text():
    assert (await _row(e="se", se_va=7))["se_va"] == 7
    huge = 10**400
    row = await _row(e="se", se_va=huge)
    assert row["se_va"] == 0.0
    assert row["se_pr"]["ex-value"] == str(huge)


@pytest.mark.anyio
async def test_parsing_exception_stores_the_atomic_fields_and_spares_the_batch(monkeypatch):
    calls = 0
    original = processing_module.parse_payload

    async def flaky(element, *args):
        nonlocal calls
        calls += 1
        if element.aid == "boom":
            raise KeyError("id")
        return await original(element, *args)

    monkeypatch.setattr(processing_module, "parse_payload", flaky)
    rows = await _rows(
        _event(aid="before"),
        _event(aid="boom", p="headset", duid="bad", ue_pr=_ue({"schema": "x", "data": {}})),
        _event(aid="after"),
    )

    assert calls == 3
    assert [row["aid"] for row in rows] == ["before", "boom", "after"]
    failed = rows[1]
    assert failed["p"] == "mob"
    assert failed["ue"] == {}
    assert failed["extra"]["tracker"] == {"platform": "headset"}
    stages = [(entry["stage"], entry.get("field")) for entry in failed["extra"]["validation"]]
    assert stages == [("payload", "duid"), ("processing", None)]
    assert failed["extra"]["validation"][1]["error"] == "KeyError: 'id'"
    assert failed["extra"]["collector"]["client_ip"] == "198.51.100.7"


@pytest.mark.anyio
async def test_event_is_dropped_when_even_the_fallback_row_fails(monkeypatch):
    async def broken(*_args):
        raise RuntimeError("parse")

    def broken_fallback(*_args):
        raise RuntimeError("fallback")

    monkeypatch.setattr(processing_module, "parse_payload", broken)
    monkeypatch.setattr(processing_module, "build_fallback_model", broken_fallback)
    before = _dropped("processing_error")

    assert await _rows(_event()) == []
    assert _dropped("processing_error") == before + 1


# --- Iglu validation -------------------------------------------------------------


@pytest.mark.anyio
async def test_iglu_data_errors_are_recorded_but_unloadable_schemas_are_not(monkeypatch):
    def validate(schema_uri, data):
        if "broken" in schema_uri:
            return ValidationResult(status="warning", error="schema file not found")
        return ValidationResult(
            status="warning", error="'id' is a required property", invalid_data=True
        )

    monkeypatch.setattr(payload_module, "validate_iglu_payload", validate)
    row = await _row(
        co=_contexts(
            {"schema": "iglu:com.acme/broken/jsonschema/1-0-0", "data": {}},
            {"schema": "iglu:com.snowplowanalytics.snowplow/web_page/jsonschema/1-0-0", "data": {}},
        )
    )

    assert row["extra"]["validation"] == [
        {
            "stage": "contexts",
            "schema": "iglu:com.snowplowanalytics.snowplow/web_page/jsonschema/1-0-0",
            "error": "'id' is a required property",
        }
    ]


def test_iglu_marks_data_failures(tmp_path, monkeypatch):
    iglu = importlib.import_module("evnt.tracker.iglu")
    schema_dir = tmp_path / "com.acme" / "thing" / "jsonschema"
    schema_dir.mkdir(parents=True)
    (schema_dir / "1-0-0").write_text(json.dumps({"type": "object", "required": ["id"]}))
    monkeypatch.setattr(iglu, "IGLU_SCHEMAS_DIR", tmp_path)
    iglu.clear_iglu_caches()
    try:
        uri = "iglu:com.acme/thing/jsonschema/1-0-0"
        assert iglu.validate_iglu_payload(uri, {}).invalid_data is True
        assert iglu.validate_iglu_payload(uri, {"id": 1}).invalid_data is False
        missing = iglu.validate_iglu_payload("iglu:com.acme/none/jsonschema/1-0-0", {})
        assert missing.status == "warning"
        assert missing.invalid_data is False
    finally:
        iglu.clear_iglu_caches()
