"""Behavioral tests for Snowplow context handlers and ``_apply_*`` helpers.

These run against the *real* ``InsertModel`` (loaded via the production import
path that ``conftest.py`` enables) so the ``_h_*`` context handlers and the
``_apply_*`` post-processing helpers are exercised end to end through
``parse_contexts`` / ``parse_payload``. Iglu validation is monkeypatched to a
no-op result so no network/registry is needed, and the error-guard paths (bad
UUID / bad datetime / missing keys) are checked to fall back rather than raise.
"""

import base64
import importlib
from datetime import UTC, datetime
from ipaddress import IPv4Address
from uuid import UUID

import pytest

from evnt.routers.tracker.models.snowplow import (
    PayloadElementModel,
    UserAgentModel,
)

payload = importlib.import_module("routers.tracker.parsers.payload")

DEFAULT_UUID = UUID("00000000-0000-0000-0000-000000000000")
DEFAULT_DATE = datetime(1970, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _stub_iglu_validation(monkeypatch):
    """Make Iglu validation a no-op so handlers are tested in isolation."""

    monkeypatch.setattr(
        payload,
        "validate_iglu_payload",
        lambda schema_uri, data: payload.ValidationResult(status="skipped"),
    )


def _base_model(**overrides) -> payload.InsertModel:
    element = PayloadElementModel.model_validate(
        {
            "aid": "example-app",
            "e": "pv",
            "p": "web",
            "tv": "js-3.0.0",
            "res": "1920x1080",
            **overrides,
        },
    )
    return payload._build_initial_model(
        element,
        UserAgentModel(),
        IPv4Address("127.0.0.1"),
    )


def _ctx(schema_uri, data):
    return {"data": [{"schema": schema_uri, "data": data}]}


async def _run_contexts(schema_uri, data, **overrides):
    model = _base_model(**overrides)
    return await payload.parse_contexts(_ctx(schema_uri, data), model)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_web_page_context_sets_view_id(anyio_backend):
    view_id = "11111111-1111-1111-1111-111111111111"
    result = await _run_contexts(
        "iglu:com.snowplowanalytics.snowplow/web_page/jsonschema/1-0-0",
        {"id": view_id},
    )

    assert result.view_id == UUID(view_id)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_web_page_context_bad_uuid_falls_back_to_default(anyio_backend):
    result = await _run_contexts(
        "iglu:com.snowplowanalytics.snowplow/web_page/jsonschema/1-0-0",
        {"id": "not-a-uuid"},
    )

    assert result.view_id == DEFAULT_UUID


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_client_session_context_populates_session_fields(anyio_backend):
    session_id = "22222222-2222-2222-2222-222222222222"
    user_id = "33333333-3333-3333-3333-333333333333"
    prev = "44444444-4444-4444-4444-444444444444"
    first_event = "55555555-5555-5555-5555-555555555555"
    result = await _run_contexts(
        "iglu:com.snowplowanalytics.snowplow/client_session/jsonschema/1-0-2",
        {
            "sessionIndex": 7,
            "sessionId": session_id,
            "userId": user_id,
            "eventIndex": 3,
            "firstEventTimestamp": "2024-01-02T03:04:05+00:00",
            "previousSessionId": prev,
            "firstEventId": first_event,
            "storageMechanism": "LOCAL_STORAGE",
        },
    )

    assert result.vid == 7
    assert result.sid == UUID(session_id)
    assert result.duid == UUID(user_id)
    assert result.event_index == 3
    assert result.first_event_time == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert result.previous_session_id == UUID(prev)
    assert result.first_event_id == UUID(first_event)
    assert result.storage_mechanism == "LOCAL_STORAGE"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_client_session_bad_datetime_and_uuids_fall_back(anyio_backend):
    result = await _run_contexts(
        "iglu:com.snowplowanalytics.snowplow/client_session/jsonschema/1-0-2",
        {
            "sessionId": "bad-session",
            "userId": "bad-user",
            "firstEventTimestamp": "definitely-not-a-date",
            "previousSessionId": "bad-prev",
            "firstEventId": "bad-first",
        },
    )

    assert result.sid == DEFAULT_UUID
    assert result.duid == DEFAULT_UUID
    assert result.first_event_time == DEFAULT_DATE
    assert result.previous_session_id == DEFAULT_UUID
    assert result.first_event_id == DEFAULT_UUID


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_mobile_context_maps_device_and_os_fields(anyio_backend):
    result = await _run_contexts(
        "iglu:com.snowplowanalytics.snowplow/mobile_context/jsonschema/1-0-3",
        {
            "deviceManufacturer": "Apple",
            "deviceModel": "iPhone15,2",
            "osType": "ios",
            "osVersion": "17.4",
            "carrier": "Verizon",
        },
    )

    assert result.device_brand == "Apple"
    assert result.device_model == "iPhone15,2"
    assert result.os_family == "ios"
    assert result.os_version_string == "17.4"
    assert result.device_is_mobile is True
    assert result.device_is_touch_capable is True
    # Remaining keys survive in device_extra.
    assert result.device_extra == {"carrier": "Verizon"}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_mobile_screen_context_sets_url_and_view_id(anyio_backend):
    screen_id = "66666666-6666-6666-6666-666666666666"
    result = await _run_contexts(
        "iglu:com.snowplowanalytics.mobile/screen/jsonschema/1-0-0",
        {"name": "HomeScreen", "id": screen_id, "type": "feed"},
    )

    assert result.url == "HomeScreen"
    assert result.view_id == UUID(screen_id)
    assert result.screen == {"type": "feed"}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_amp_context_merges_into_amp_dict(anyio_backend):
    result = await _run_contexts(
        "iglu:dev.amp.snowplow/amp_id/jsonschema/1-0-0",
        {"ampClientId": "amp-123", "userId": "u-1"},
    )

    assert result.amp["ampClientId"] == "amp-123"
    assert result.amp["userId"] == "u-1"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_mobile_application_sets_app_version_and_build(anyio_backend):
    result = await _run_contexts(
        "iglu:com.snowplowanalytics.mobile/application/jsonschema/1-0-0",
        {"version": "2.1.0", "build": "204", "ignored": 5},
    )

    assert result.app_version == "2.1.0"
    assert result.app_build == "204"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_static_and_performance_and_client_hints_go_to_extra(anyio_backend):
    model = _base_model()
    contexts = {
        "data": [
            {
                "schema": "iglu:com.acme/static_context/jsonschema/1-0-0",
                "data": {"tenant": "acme"},
            },
            {
                "schema": "iglu:org.w3/PerformanceTiming/jsonschema/1-0-0",
                "data": {"loadEventEnd": 1234},
            },
            {
                "schema": "iglu:org.ietf/http_client_hints/jsonschema/1-0-0",
                "data": {"isMobile": True},
            },
            {
                "schema": "iglu:com.google.ga4/cookies/jsonschema/1-0-0",
                "data": {"_ga": "GA1.1.x"},
            },
        ],
    }

    result = await payload.parse_contexts(contexts, model)

    assert result.extra["tenant"] == "acme"
    assert result.extra["performance_timing"] == {"loadEventEnd": 1234}
    assert result.extra["client_hints"] == {"isMobile": True}
    assert result.extra["ga_cookies"] == {"_ga": "GA1.1.x"}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_geolocation_and_ue_setter_handlers(anyio_backend):
    model = _base_model()
    contexts = {
        "data": [
            {
                "schema": (
                    "iglu:com.snowplowanalytics.snowplow/"
                    "geolocation_context/jsonschema/1-1-0"
                ),
                "data": {"latitude": 1.0, "longitude": 2.0},
            },
            {
                "schema": (
                    "iglu:com.snowplowanalytics.mobile/screen_summary/jsonschema/1-0-0"
                ),
                "data": {"foregroundSec": 12},
            },
        ],
    }

    result = await payload.parse_contexts(contexts, model)

    assert result.geolocation == {"latitude": 1.0, "longitude": 2.0}
    assert result.ue["screen_summary"] == {"foregroundSec": 12}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_browser_context_keeps_dims_and_stashes_extra(anyio_backend):
    result = await _run_contexts(
        "iglu:com.snowplowanalytics.snowplow/browser_context/jsonschema/2-0-0",
        {"resolution": "800x600", "viewport": "400x300", "tabId": "t1"},
    )

    assert result.res == "800x600"
    assert result.vp == "400x300"
    assert result.browser_extra == {"tabId": "t1"}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_unknown_schema_is_skipped_with_warning(anyio_backend):
    model = _base_model()
    contexts = {
        "data": [
            {"schema": "iglu:com.unknown/whatever/jsonschema/1-0-0", "data": {"x": 1}},
        ],
    }

    result = await payload.parse_contexts(contexts, model)

    # Nothing crashed and no handler captured the data.
    assert "x" not in result.extra


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_contexts_with_missing_keys_and_non_dict_are_skipped(anyio_backend):
    model = _base_model()
    contexts = {
        "data": [
            "not-a-dict",
            {"schema": "iglu:com.acme/static_context/jsonschema/1-0-0"},  # no data
            {"data": {"x": 1}},  # no schema
            {
                "schema": "iglu:com.acme/static_context/jsonschema/1-0-0",
                "data": {"kept": True},
            },
        ],
    }

    result = await payload.parse_contexts(contexts, model)

    # Only the well-formed static context made it through.
    assert result.extra == {"kept": True}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_contexts_none_or_missing_data_key_returns_model_unchanged(anyio_backend):
    model = _base_model()

    assert await payload.parse_contexts(None, model) is model
    assert await payload.parse_contexts({"no_data": 1}, model) is model


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_non_dict_data_payload_is_skipped(anyio_backend):
    model = _base_model()
    contexts = {
        "data": [
            {
                "schema": "iglu:com.acme/static_context/jsonschema/1-0-0",
                "data": ["this", "is", "a", "list"],
            },
        ],
    }

    result = await payload.parse_contexts(contexts, model)

    assert result.extra == {}


# ---------------------------------------------------------------------------
# _apply_* helpers exercised via parse_payload
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_parse_payload_amp_linker_extracts_device_id(anyio_backend):
    # sp_amp_linker format: "1*<hash>*<base64 device id>".
    device_uuid = "77777777-7777-7777-7777-777777777777"
    encoded = base64.urlsafe_b64encode(device_uuid.encode()).decode().rstrip("=")
    element = PayloadElementModel.model_validate(
        {
            "aid": "example-app",
            "e": "pv",
            "p": "web",
            "tv": "js-3.0.0",
            "res": "1920x1080",
            "url": f"https://example.com/?sp_amp_linker=1*abc*{encoded}",
        },
    )

    result = await payload.parse_payload(
        element,
        UserAgentModel(),
        IPv4Address("127.0.0.1"),
        cookies=None,
    )

    assert result.amp["device_id"] == UUID(device_uuid)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_parse_payload_cookie_duid_fallback(anyio_backend):
    device_id = "88888888-8888-8888-8888-888888888888"
    cookie = (
        "_sp_id.1234="
        f"{device_id}.1700000000.1.1700000100.1700000050."
        "99999999-9999-9999-9999-999999999999"
    )
    element = PayloadElementModel.model_validate(
        {
            "aid": "example-app",
            "e": "pv",
            "p": "web",
            "tv": "js-3.0.0",
            "res": "1920x1080",
        },
    )

    result = await payload.parse_payload(
        element,
        UserAgentModel(),
        IPv4Address("127.0.0.1"),
        cookies=cookie,
    )

    assert result.duid == UUID(device_id)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_parse_payload_normalizes_se_property_and_value(anyio_backend):
    element = PayloadElementModel.model_validate(
        {
            "aid": "example-app",
            "e": "se",
            "p": "web",
            "tv": "js-3.0.0",
            "res": "1920x1080",
            "se_pr": "not-json-string",
            "se_va": "12.5",
        },
    )

    result = await payload.parse_payload(
        element,
        UserAgentModel(),
        IPv4Address("127.0.0.1"),
        cookies=None,
    )

    # Non-JSON se_pr string is wrapped under "ex-property".
    assert result.se_pr == {"ex-property": "not-json-string"}
    # Numeric-looking se_va string is coerced to float.
    assert result.se_va == 12.5


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_parse_payload_non_numeric_se_value_stashed(anyio_backend):
    element = PayloadElementModel.model_validate(
        {
            "aid": "example-app",
            "e": "se",
            "p": "web",
            "tv": "js-3.0.0",
            "res": "1920x1080",
            "se_va": "abc",
        },
    )

    result = await payload.parse_payload(
        element,
        UserAgentModel(),
        IPv4Address("127.0.0.1"),
        cookies=None,
    )

    assert result.se_va == 0.0
    assert result.se_pr.get("ex-value") == "abc"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_parse_payload_client_hints_device_flags(anyio_backend):
    element = PayloadElementModel.model_validate(
        {
            "aid": "example-app",
            "e": "pv",
            "p": "web",
            "tv": "js-3.0.0",
            "res": "1920x1080",
            "co": (
                '{"data": [{"schema": '
                '"iglu:org.ietf/http_client_hints/jsonschema/1-0-0", '
                '"data": {"isMobile": true}}]}'
            ),
        },
    )

    result = await payload.parse_payload(
        element,
        UserAgentModel(),
        IPv4Address("127.0.0.1"),
        cookies=None,
    )

    assert result.device_is_mobile is True
    assert result.device_is_pc is False
