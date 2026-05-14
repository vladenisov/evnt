"""
Payload parsing functionality for Snowplow events.
"""

import urllib.parse as urlparse
from collections.abc import Callable
from copy import copy
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from ipaddress import IPv4Address
from typing import Any
from uuid import UUID

import orjson
import structlog
from core.config import settings
from core.tracing import async_capture_span, capture_span
from routers.tracker.models.snowplow import (
    InsertModel,
    PayloadElementModel,
    StructuredEvent,
    UserAgentModel,
)
from routers.tracker.parsers.iglu import ValidationResult, validate_iglu_payload
from routers.tracker.parsers.utils import parse_base64

logger = structlog.stdlib.get_logger()

# Constants for empty values
EMPTY_DICTS = (
    "ue_context",
    "extra",
    "user_data",
    "page_data",
    "screen",
    "session_unstructured",
    "browser_extra",
    "amp",
    "device_extra",
    "geolocation",
)
EMPTY_STRINGS = (
    "app_version",
    "app_build",
    "storage_mechanism",
    "device_model",
    "device_brand",
)

EMPTY_DATES = ("first_event_time",)

EMPTY_UUIDS = (
    "view_id",
    "previous_session_id",
    "first_event_id",
)

EMPTY_INTS = ("event_index",)

DEFAULT_UUID = UUID("00000000-0000-0000-0000-000000000000")
DEFAULT_DATE = datetime(1970, 1, 1, tzinfo=UTC)

# _sp_id cookie value is a 6-part dot-separated string:
# device_id.created_time.vid.now_time.last_visit_time.session_id
SP_ID_COOKIE_PARTS = 6

_SE_PR_JSON_ERRORS = (orjson.JSONDecodeError, TypeError)

schemas = settings.common.snowplow.schemas

_MUTABLE_USER_AGENT_FIELDS = (
    "browser_version",
    "browser_extra",
    "os_version",
    "device_extra",
)


def _coalesce_dimension_value(value: Any, fallback: str) -> str:
    """Keep an existing dimension when a context tries to overwrite it with null."""

    if isinstance(value, str) and value.strip():
        return value
    return fallback


def _log_validation_result(
    validation: ValidationResult,
    schema_uri: str,
    validation_stage: str,
) -> None:
    """Log Iglu validation outcomes without changing ingest behavior."""

    schema_path = str(validation.schema_path) if validation.schema_path else None

    if validation.status == "ok":
        logger.debug(
            "Iglu validation passed",
            validation_stage=validation_stage,
            schema=schema_uri,
            schema_path=schema_path,
        )
        return
    if validation.status != "warning":
        return

    logger.warning(
        "Iglu validation warning",
        validation_stage=validation_stage,
        schema=schema_uri,
        schema_path=schema_path,
        error=validation.error,
    )


@capture_span()
def parse_cookies(cookies_str: str | None) -> dict[str, Any]:
    """
    Parse cookies string.

    Args:
        cookies_str: The cookies string

    Returns:
        Dictionary of cookie values
    """
    if not cookies_str:
        return {}

    cookies = SimpleCookie()
    try:
        cookies.load(cookies_str)
    except Exception as e:
        logger.warning(f"Failed to parse cookies: {e}")
        return {}

    # Extract Snowplow specific cookies
    result = {}
    for name, cookie in cookies.items():
        # Extract domain user ID from sp cookie
        if name.startswith("_sp_id"):
            parts = cookie.value.split(".")
            if len(parts) < SP_ID_COOKIE_PARTS:
                logger.warning("Incomplete _sp_id cookie", cookie_value=cookie.value)
                continue

            result["device_id"] = parts[0]
            result["created_time"] = parts[1]
            if parts[2] is not None:
                result["vid"] = parts[2]
            result["now_time"] = parts[3]
            result["last_visit_time"] = parts[4]
            result["session_id"] = parts[5]

        # # Extract session ID from sp cookie
        # if name.startswith("_sp_ses"):
        #     result["session_id"] = cookie.value

    return result


ContextHandler = Callable[[InsertModel, dict[str, Any]], None]


def _h_static(model: InsertModel, data: dict[str, Any]) -> None:
    model.extra.update(data)


def _h_perf_timing(model: InsertModel, data: dict[str, Any]) -> None:
    model.extra["performance_timing"] = data


def _h_client_hints(model: InsertModel, data: dict[str, Any]) -> None:
    model.extra["client_hints"] = data


def _h_ga_cookies(model: InsertModel, data: dict[str, Any]) -> None:
    model.extra["ga_cookies"] = data


def _h_web_page(model: InsertModel, data: dict[str, Any]) -> None:
    model.view_id = UUID(data["id"])


def _h_amp(model: InsertModel, data: dict[str, Any]) -> None:
    model.amp = dict(model.amp, **data)


def _h_page_data(model: InsertModel, data: dict[str, Any]) -> None:
    model.page_data = dict(model.page_data, **data)


def _h_mobile_context(model: InsertModel, data: dict[str, Any]) -> None:
    model.device_brand = data.pop("deviceManufacturer")
    model.device_model = data.pop("deviceModel")
    model.os_family = data.pop("osType")
    model.os_version_string = data.pop("osVersion")
    model.device_is_mobile = True
    model.device_is_touch_capable = True
    model.device_extra = data


def _h_mobile_application(model: InsertModel, data: dict[str, Any]) -> None:
    for k in ("version", "build"):
        if k in data and isinstance(data[k], str):
            setattr(model, f"app_{k}", data[k])


def _h_client_session(model: InsertModel, data: dict[str, Any]) -> None:
    visit_count = data.pop("sessionIndex", 0)
    if visit_count:
        model.vid = visit_count
    if data.get("sessionId"):
        model.sid = UUID(data.pop("sessionId"))
    if data.get("userId"):
        model.duid = UUID(data.pop("userId"))
    model.event_index = data.pop("eventIndex", 0)
    first_event_time = data.pop("firstEventTimestamp", None)
    if first_event_time is not None:
        model.first_event_time = datetime.fromisoformat(first_event_time)
    previous_session_id = data.pop("previousSessionId", None)
    if previous_session_id:
        model.previous_session_id = UUID(previous_session_id)
    first_event_id = data.pop("firstEventId", None)
    if first_event_id:
        model.first_event_id = UUID(first_event_id)
    model.storage_mechanism = data.pop("storageMechanism", "")


def _h_mobile_screen(model: InsertModel, data: dict[str, Any]) -> None:
    model.url = data.pop("name")
    model.view_id = UUID(data.pop("id"))
    model.screen = dict(model.screen, **data)


def _h_browser_context(model: InsertModel, data: dict[str, Any]) -> None:
    if "resolution" in data:
        model.res = _coalesce_dimension_value(data.pop("resolution"), model.res)
    if "viewport" in data:
        model.vp = _coalesce_dimension_value(data.pop("viewport"), model.vp)
    if "documentSize" in data:
        model.ds = _coalesce_dimension_value(data.pop("documentSize"), model.ds)
    if data:
        model.browser_extra = data


def _h_geolocation(model: InsertModel, data: dict[str, Any]) -> None:
    model.geolocation = data


def _h_screen_data(model: InsertModel, data: dict[str, Any]) -> None:
    model.screen = dict(model.screen, **data)


def _h_user_data(model: InsertModel, data: dict[str, Any]) -> None:
    model.user_data = dict(model.user_data, **data)


def _ue_setter(key: str) -> ContextHandler:
    def _h(model: InsertModel, data: dict[str, Any]) -> None:
        model.ue[key] = data

    return _h


CONTEXT_HANDLERS: dict[str, ContextHandler] = {
    "com.acme/static_context": _h_static,
    "org.w3/PerformanceTiming": _h_perf_timing,
    "org.ietf/http_client_hints": _h_client_hints,
    "com.google.analytics/cookies": _h_ga_cookies,
    "com.google.ga4/cookies": _h_ga_cookies,
    "com.snowplowanalytics.snowplow/web_page": _h_web_page,
    "dev.amp.snowplow/amp_session": _h_amp,
    "dev.amp.snowplow/amp_id": _h_amp,
    "dev.amp.snowplow/amp_web_page": _h_amp,
    schemas.page_data: _h_page_data,
    "com.snowplowanalytics.snowplow/mobile_context": _h_mobile_context,
    "com.snowplowanalytics.mobile/application": _h_mobile_application,
    "com.snowplowanalytics.snowplow/client_session": _h_client_session,
    "com.snowplowanalytics.mobile/screen": _h_mobile_screen,
    "com.snowplowanalytics.snowplow/browser_context": _h_browser_context,
    "com.snowplowanalytics.snowplow/geolocation_context": _h_geolocation,
    schemas.screen_data: _h_screen_data,
    schemas.user_data: _h_user_data,
    schemas.ad_data: _ue_setter("ad_data"),
    "com.snowplowanalytics.mobile/screen_summary": _ue_setter("screen_summary"),
    "com.snowplowanalytics.mobile/application_lifecycle": _ue_setter("app_lifecycle"),
    "com.android.installreferrer.api/referrer_details": _ue_setter("install_referrer"),
    "org.w3/PerformanceNavigationTiming": _ue_setter("performance_navigation_timing"),
    "com.snowplowanalytics.mobile/deep_link": _ue_setter("deep_link"),
    "com.snowplowanalytics.mobile/deep_link_received": _ue_setter("deep_link_received"),
    "com.snowplowanalytics.mobile/message_notification": _ue_setter(
        "message_notification",
    ),
}


@async_capture_span()
async def parse_contexts(
    contexts: dict[str, Any] | None,
    model: InsertModel,
) -> InsertModel:
    """
    Parse Snowplow contexts.

    Args:
        contexts: The contexts dictionary
        model: The InsertModel to update

    Returns:
        Processed InsertModel with updated fields
    """

    if not contexts or "data" not in contexts:
        return model

    for context in contexts["data"]:
        if not isinstance(context, dict):
            logger.warning("Context is not a dict", context=context)
            continue

        missing_keys = [k for k in ("schema", "data") if k not in context]
        if missing_keys:
            logger.warning(
                "Context is missing required keys",
                missing=missing_keys,
                context=context,
            )
            continue

        schema_uri = context["schema"]
        data = context["data"]

        validation = validate_iglu_payload(schema_uri, data)
        _log_validation_result(
            validation,
            schema_uri=schema_uri,
            validation_stage="contexts",
        )

        if not isinstance(schema_uri, str):
            logger.warning("Wrong schema type", context=context)
            continue

        schema = "/".join(schema_uri[5:].split("/")[:2])

        if not isinstance(data, dict):
            logger.warning("Wrong data type", context=context)
            continue

        handler = CONTEXT_HANDLERS.get(schema)
        if handler is None:
            logger.warning(
                "Schema has no parser",
                data=data,
                schema=schema,
                schema_uri=schema_uri,
            )
            continue

        handler(model, data)

    return model


@async_capture_span()
async def parse_event(event: dict[str, Any] | None, model: InsertModel) -> InsertModel:
    """
    Parse Snowplow event data.

    Args:
        event: The event dictionary
        model: The InsertModel to update

    Returns:
        Processed InsertModel with updated fields
    """

    if event is None or "data" not in event:
        return model

    event_payload: dict[str, Any] = event["data"]
    schema_uri = event_payload.get("schema", "")
    validation = validate_iglu_payload(schema_uri, event_payload.get("data"))
    _log_validation_result(
        validation,
        schema_uri=schema_uri,
        validation_stage="event",
    )

    if event_payload["schema"] == schemas.u2s_data:
        se = StructuredEvent.model_validate(event_payload["data"])
        for field_name in StructuredEvent.model_fields:
            setattr(model, field_name, getattr(se, field_name))
        model.e = "se"
    else:
        event_name = event_payload["schema"].split("/")[-3]
        model.ue[event_name] = event_payload["data"]
    return model


def _build_initial_model(
    element: PayloadElementModel,
    user_agent: UserAgentModel,
    ip: IPv4Address,
) -> InsertModel:
    """Merge payload + UA + IP into a fresh InsertModel."""

    data = user_agent.__dict__.copy()
    for field_name in _MUTABLE_USER_AGENT_FIELDS:
        data[field_name] = copy(data[field_name])
    data.update(element.__dict__)
    data["user_ip"] = ip
    return InsertModel.model_construct(**data)


def dump_insert_model(model: InsertModel) -> dict[str, Any]:
    """Return a ClickHouse row dict without running Pydantic serialization."""

    return model.__dict__.copy()


def _apply_amp_fields(result: InsertModel) -> None:
    """Promote AMP-specific fields from `result.amp` onto top-level fields."""

    if "uid" in result.amp:
        result.uid = result.amp.pop("userId")
    if result.e == "ue" and "amp_page_ping" in result.ue:
        result.e = "pp"
        result.ue["amp_page_ping"] = result.ue.pop("amp_page_ping")
    if result.amp.get("domainUserid"):
        result.duid = UUID(result.amp.pop("domainUserid"))


def _apply_amp_linker(result: InsertModel) -> None:
    """Decode the ``sp_amp_linker`` query param and extract the AMP device id."""

    if not result.url:
        return

    parsed_url = urlparse.urlparse(result.url)
    query_string = urlparse.parse_qs(parsed_url.query)
    if not query_string.get("sp_amp_linker"):
        return

    amp_linker = query_string["sp_amp_linker"][0]
    try:
        *_, amp_device_id = amp_linker.split("*")
        amp_device_id = parse_base64(amp_device_id)
        result.amp["device_id"] = UUID(amp_device_id)
    except Exception as e:
        logger.warning(f"Failed to parse AMP linker: {e}")


def _apply_cookie_duid(result: InsertModel, cookies: str | None) -> None:
    """Fall back to the Snowplow `_sp_id` cookie when `duid` is still unset."""

    if result.duid is not None:
        return
    sp_cookies = parse_cookies(cookies)
    if sp_cookies and "device_id" in sp_cookies:
        result.duid = UUID(sp_cookies["device_id"])


def _apply_screen_view(result: InsertModel) -> None:
    """Flatten a mobile ``screen_view`` unstructured event into a page view."""

    if "screen_view" in result.ue:
        result.e = "pv"
        result.view_id = UUID(result.ue["screen_view"].pop("id"))
        result.url = result.ue["screen_view"].pop("name")

        if "previousName" in result.ue["screen_view"]:
            result.refr = result.ue["screen_view"].pop("previousName")
            if result.refr == "Unknown":
                result.refr = ""
        else:
            result.refr = ""

        result.screen.update(result.ue.pop("screen_view"))

    # A mobile screen context can also supply the URL directly.
    if "screen_name" in result.screen:
        result.url = result.screen.pop("screen_name")


def _normalize_se_property(result: InsertModel) -> None:
    """Force ``result.se_pr`` to a dict, wrapping non-JSON strings."""

    if not (result.se_pr and isinstance(result.se_pr, str)):
        result.se_pr = {}
        return

    try:
        result.se_pr = orjson.loads(result.se_pr)
    except _SE_PR_JSON_ERRORS:
        result.se_pr = {"ex-property": result.se_pr}

    if not isinstance(result.se_pr, dict):
        result.se_pr = {"ex-property": result.se_pr}


def _normalize_se_value(result: InsertModel) -> None:
    """Force ``result.se_va`` to a float, stashing non-numeric originals.

    Assumes ``_normalize_se_property`` ran first so ``result.se_pr`` is a dict.
    """

    if not result.se_va:
        result.se_va = 0.0
        return

    if isinstance(result.se_va, (float, int)):
        return

    try:
        result.se_va = float(result.se_va)
    except ValueError:
        if isinstance(result.se_pr, dict):
            result.se_pr["ex-value"] = result.se_va
        result.se_va = 0.0


def _apply_client_hints_device(result: InsertModel) -> None:
    """Derive ``device_is_mobile`` / ``device_is_pc`` from UA client hints."""

    is_mobile = result.extra.get("client_hints", {}).get("isMobile")
    if is_mobile is None:
        return
    result.device_is_mobile = bool(int(is_mobile))
    result.device_is_pc = bool(int(not is_mobile))


@async_capture_span()
async def parse_payload(
    element: PayloadElementModel,
    user_agent: UserAgentModel,
    ip: IPv4Address,
    cookies: str | None,
) -> InsertModel:
    """Parse a Snowplow event payload into an ``InsertModel``."""

    result = _build_initial_model(element, user_agent, ip)
    result = await parse_contexts(element.contexts, result)
    result = await parse_event(element.ue_context, result)

    if element.ping_context:
        result.ue["page_ping"] = element.ping_context

    _apply_amp_fields(result)
    _apply_amp_linker(result)
    _apply_cookie_duid(result, cookies)
    _apply_screen_view(result)
    _normalize_se_property(result)
    _normalize_se_value(result)
    _apply_client_hints_device(result)

    return result
