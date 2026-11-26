"""
Payload parsing functionality for Snowplow events.
"""

import urllib.parse as urlparse
from collections.abc import Callable
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from ipaddress import IPv4Address
from typing import Any, Final, Literal
from uuid import UUID

import structlog
from pydantic import ValidationError

from evnt.config import settings
from evnt.observability.ratelimit import KeyedRateLimiter, Permit
from evnt.observability.tracing import async_capture_span, capture_span
from evnt.tracker.element import EventIssues, without_collector_keys
from evnt.tracker.iglu import ValidationResult, validate_iglu_payload
from evnt.tracker.models import (
    UINT64_MAX,
    InsertModel,
    PayloadElementModel,
    StructuredEvent,
    UserAgentModel,
)
from evnt.tracker.rows import (
    apply_platform,
    apply_true_timestamp,
    build_initial_model,
    keep_replaced_urls,
    normalize_se_property,
    normalize_se_value,
)
from evnt.tracker.utils import parse_base64

logger = structlog.stdlib.get_logger()

# A tracker can trigger the warnings below on every event it sends, and each
# emitted line costs a few hundred microseconds, so they are rate-limited per
# key. Lines carry ``suppressed``: how many occurrences of that key were held
# back since its previous line. Two kinds of line count differently: a line
# with ``untracked_suppressed`` came from the shared overflow bucket (the key
# table was full or the key too long), and that count spans every untracked
# key; a line with ``evicted=True`` stands for no new occurrence and only
# reports what a key dropped from the table still held back.
_MALFORMED_INPUT_LOG = KeyedRateLimiter()  # keyed by the warning text
type ValidationStage = Literal["contexts", "event"]
_VALIDATION_LOG: dict[ValidationStage, KeyedRateLimiter] = {
    "contexts": KeyedRateLimiter(),  # keyed by schema URI
    "event": KeyedRateLimiter(),
}
_LOG_LIMITERS = (_MALFORMED_INPUT_LOG, *_VALIDATION_LOG.values())


def reset_log_rate_limits() -> None:
    """Forget every rate-limited warning key, so tests start from a clean slate."""

    for limiter in _LOG_LIMITERS:
        limiter.reset()


def _log_evicted(permit: Permit, event: str | None, key_field: str | None, **fields: Any) -> None:
    """Report the held-back count of a key the limiter dropped to admit another.

    ``event`` of ``None`` means the key is the event text itself; otherwise
    the key goes into ``key_field``.
    """

    if permit.evicted is None:
        return
    key, suppressed = permit.evicted
    if key_field is not None:
        fields[key_field] = key
    logger.warning(event or key, suppressed=suppressed, evicted=True, **fields)


def _warn_malformed(event: str, **fields: Any) -> None:
    """Log a fixed-text warning about malformed client input, rate-limited by text."""

    if (permit := _MALFORMED_INPUT_LOG.take(event)) is not None:
        _log_evicted(permit, None, None)
        logger.warning(event, **permit.log_fields(), **fields)


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

# ``isMobile`` from the client hints context: anything ``int()`` refuses.
_CLIENT_HINT_ERRORS = (TypeError, ValueError)

schemas = settings.common.snowplow.schemas

# Vendors of the collector's own schemas (``dev.snowplow.simple/user_data``
# and the rest of ``SnowplowSchemas``). They are fixed names with no format
# or version, defined by this collector rather than published to Iglu, so
# they are never validated and a repeat on one event is merged by its
# handler, as it always was, instead of being kept or flagged.
_COLLECTOR_VENDORS: Final = frozenset(
    name.split("/", 1)[0] for name in schemas.model_dump().values()
)
_NOT_VALIDATED: Final = ValidationResult(status="skipped")


def _coalesce_dimension_value(value: Any, fallback: str) -> str:
    """Keep an existing dimension when a context tries to overwrite it with null."""

    if isinstance(value, str) and value.strip():
        return value
    return fallback


def _safe_uuid(value: Any, fallback: UUID = DEFAULT_UUID) -> UUID:
    """Parse a tracker-supplied UUID string, falling back on malformed input.

    Tracker payloads are untrusted, so a bad value must not raise and abort the
    whole ingest. Logs a warning and returns ``fallback`` instead.
    """

    # ``UUID()`` raises AttributeError, not ValueError, for a JSON number, list
    # or object, so anything but a string is rejected before it gets there.
    # Only the type is logged: the value may be an arbitrarily large object.
    if value is not None and not isinstance(value, str):
        _warn_malformed("Failed to parse UUID", value_type=type(value).__name__)
        return fallback
    try:
        return UUID(value)
    except (ValueError, TypeError) as e:
        _warn_malformed("Failed to parse UUID", value=value, error=str(e))
        return fallback


def _optional_uuid(value: Any) -> UUID | None:
    """Parse a tracker-supplied UUID that may be absent.

    Mobile trackers send the nil UUID for an anonymised user, and treating it
    as an id would fold every anonymous user into one, so it means "absent"
    here, as does a malformed value (logged by ``_safe_uuid``).
    """

    parsed = _safe_uuid(value)
    return None if parsed.int == 0 else parsed


def _as_str(value: Any) -> str | None:
    """Return ``value`` if it is a string, else ``None``.

    Context data is untrusted JSON; a number or object assigned to a String
    column would fail the whole insert, not just this event.
    """

    return value if isinstance(value, str) else None


def _as_uint(value: Any) -> int | None:
    """Return ``value`` if it is an integer that fits a UInt64 column."""

    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= UINT64_MAX:
        return value
    return None


def _safe_datetime(value: Any, fallback: datetime = DEFAULT_DATE) -> datetime:
    """Parse a tracker-supplied ISO datetime string, falling back on bad input.

    Tracker payloads are untrusted, so a bad value must not raise and abort the
    whole ingest. Logs a warning and returns ``fallback`` instead.
    """

    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError) as e:
        _warn_malformed("Failed to parse datetime", value=value, error=str(e))
        return fallback


_IGLU_SCHEME_PREFIX = "iglu:"


def _schema_name_from_uri(schema_uri: str) -> str:
    """Extract the ``vendor/name`` key from an Iglu schema URI.

    Strips a leading ``iglu:`` scheme prefix explicitly (instead of a magic
    numeric slice) so non-iglu URIs are not silently truncated, then keeps the
    first two path segments (``vendor/name``).
    """

    body = schema_uri.removeprefix(_IGLU_SCHEME_PREFIX)
    return "/".join(body.split("/")[:2])


def _is_collector_schema(schema: str) -> bool:
    """Whether a ``vendor/name`` key belongs to the collector's own schemas."""

    return schema.split("/", 1)[0] in _COLLECTOR_VENDORS


def _log_validation_result(
    validation: ValidationResult,
    schema_uri: str,
    validation_stage: ValidationStage,
    issues: EventIssues | None = None,
) -> None:
    """Log Iglu validation outcomes and record data errors on the event.

    Only data that failed its schema goes into ``extra.validation``; a schema
    the collector cannot load is a deployment matter and stays in the log.
    """

    if issues is not None and validation.invalid_data and validation.error is not None:
        issues.add(validation_stage, validation.error, schema=schema_uri)

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

    if (permit := _VALIDATION_LOG[validation_stage].take(schema_uri)) is None:
        return
    _log_evicted(permit, "Iglu validation warning", "schema", validation_stage=validation_stage)
    logger.warning(
        "Iglu validation warning",
        **permit.log_fields(),
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
        _warn_malformed("Failed to parse cookies", error=str(e))
        return {}

    # Extract Snowplow specific cookies
    result = {}
    for name, cookie in cookies.items():
        # Extract domain user ID from sp cookie
        if name.startswith("_sp_id"):
            parts = cookie.value.split(".")
            if len(parts) < SP_ID_COOKIE_PARTS:
                _warn_malformed("Incomplete _sp_id cookie", cookie_value=cookie.value)
                continue

            result["device_id"] = parts[0]
            result["created_time"] = parts[1]
            result["vid"] = parts[2]
            result["now_time"] = parts[3]
            result["last_visit_time"] = parts[4]
            result["session_id"] = parts[5]

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
    model.view_id = _safe_uuid(data.get("id"))


def _h_amp(model: InsertModel, data: dict[str, Any]) -> None:
    model.amp = dict(model.amp, **data)


def _h_page_data(model: InsertModel, data: dict[str, Any]) -> None:
    model.page_data = dict(model.page_data, **data)


def _h_mobile_context(model: InsertModel, data: dict[str, Any]) -> None:
    device_brand = _as_str(data.pop("deviceManufacturer", None))
    if device_brand is not None:
        model.device_brand = device_brand
    device_model = _as_str(data.pop("deviceModel", None))
    if device_model is not None:
        model.device_model = device_model
    os_family = _as_str(data.pop("osType", None))
    if os_family is not None:
        model.os_family = os_family
    os_version = _as_str(data.pop("osVersion", None))
    if os_version is not None:
        model.os_version_string = os_version
    model.device_is_mobile = True
    model.device_is_touch_capable = True
    model.device_extra = data


def _h_mobile_application(model: InsertModel, data: dict[str, Any]) -> None:
    for k in ("version", "build"):
        if k in data and isinstance(data[k], str):
            setattr(model, f"app_{k}", data[k])


def _h_client_session(model: InsertModel, data: dict[str, Any]) -> None:
    visit_count = _as_uint(data.pop("sessionIndex", 0))
    if visit_count:
        model.vid = visit_count
    # The nil UUID is an anonymised id (userAnonymisation): it must not
    # replace a real id, nor become one, so it counts as absent.
    if data.get("sessionId") and (session_id := _optional_uuid(data.pop("sessionId"))):
        model.sid = session_id
    if data.get("userId") and (user_id := _optional_uuid(data.pop("userId"))):
        model.duid = user_id
    model.event_index = _as_uint(data.pop("eventIndex", 0)) or 0
    first_event_time = data.pop("firstEventTimestamp", None)
    if first_event_time is not None:
        model.first_event_time = _safe_datetime(first_event_time)
    previous_session_id = data.pop("previousSessionId", None)
    if previous_session_id:
        model.previous_session_id = _optional_uuid(previous_session_id) or DEFAULT_UUID
    first_event_id = data.pop("firstEventId", None)
    if first_event_id:
        model.first_event_id = _safe_uuid(first_event_id)
    model.storage_mechanism = _as_str(data.pop("storageMechanism", "")) or ""


def _h_mobile_screen(model: InsertModel, data: dict[str, Any]) -> None:
    name = _as_str(data.pop("name", None))
    if name is not None:
        model.url = name
    screen_id = data.pop("id", None)
    if screen_id is not None:
        model.view_id = _safe_uuid(screen_id)
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
    issues: EventIssues | None = None,
) -> InsertModel:
    """
    Parse Snowplow contexts.

    Entities with a handler fill their columns; every other entity, and every
    repeat of a handled schema, is kept in ``extra.contexts`` through
    ``issues`` (see ``evnt.tracker.entities``).

    Args:
        contexts: The contexts dictionary
        model: The InsertModel to update
        issues: Where malformed contexts of a stored event are recorded

    Returns:
        Processed InsertModel with updated fields
    """

    if not contexts or "data" not in contexts:
        return model

    entities = contexts["data"]
    if not isinstance(entities, list):
        _warn_malformed("Contexts data is not a list", value_type=type(entities).__name__)
        if issues is not None:
            issues.add("contexts", "contexts data is not a list")
        return model

    handled: set[str] = set()
    for context in entities:
        if not isinstance(context, dict):
            _warn_malformed("Context is not a dict", context=context)
            if issues is not None:
                issues.add("contexts", "context entity is not an object")
            continue

        missing_keys = [k for k in ("schema", "data") if k not in context]
        if missing_keys:
            _warn_malformed(
                "Context is missing required keys",
                missing=missing_keys,
                context=context,
            )
            if issues is not None:
                issues.add("contexts", f"context entity has no {' or '.join(missing_keys)}")
            continue

        schema_uri = context["schema"]
        data = context["data"]

        if not isinstance(schema_uri, str):
            _warn_malformed("Wrong schema type", context=context)
            if issues is not None:
                issues.add("contexts", "context schema is not a string")
            continue

        schema = _schema_name_from_uri(schema_uri)
        collector_schema = _is_collector_schema(schema)
        if collector_schema:
            validation = _NOT_VALIDATED
        else:
            validation = validate_iglu_payload(schema_uri, data)
            _log_validation_result(
                validation,
                schema_uri=schema_uri,
                validation_stage="contexts",
                issues=issues,
            )
        handler = CONTEXT_HANDLERS.get(schema)
        # A collector schema has no version, so it is kept under its name.
        kept_as = (schema, "") if collector_schema else None

        if not isinstance(data, dict):
            # Kept whatever the schema: no handler can consume it.
            _warn_malformed("Wrong data type", schema=schema_uri, value_type=type(data).__name__)
            if issues is not None:
                if not validation.invalid_data:
                    issues.add("contexts", "context data is not an object", schema=schema_uri)
                issues.keep_entity(schema_uri, data, key=kept_as)
            continue

        if handler is None:
            # Kept in extra.contexts. Not logged: this used to be a warning
            # per entity and was the main source of log noise.
            if issues is not None:
                issues.keep_entity(schema_uri, data, key=kept_as)
            continue

        if schema in handled:
            # The handlers merge or overwrite a repeated schema, which keeps
            # only one entity's values in the columns. They still run, so the
            # columns get what they always got, and the repeat is kept whole,
            # encoded before the handler pops fields out of it.
            if issues is not None and not collector_schema:
                issues.keep_entity(schema_uri, data)
        else:
            handled.add(schema)
        if handler is _h_static:
            # The only handler that merges tracker keys into extra itself; a
            # kept repeat above is stored whole, as JSON text it cannot spoof.
            data = without_collector_keys(data, schema_uri, issues)
        handler(model, data)

    if issues is not None:
        issues.note_dropped_entities()
    return model


@async_capture_span()
async def parse_event(
    event: dict[str, Any] | None,
    model: InsertModel,
    issues: EventIssues | None = None,
) -> InsertModel:
    """
    Parse Snowplow event data.

    A self-describing event that is not shaped as one (no schema, a schema
    that is not an Iglu URI, data that is not what its schema needs) leaves
    the event without its ``ue`` data and records why in ``issues``, so the
    event itself is still stored.

    Args:
        event: The event dictionary
        model: The InsertModel to update
        issues: Where problems of a stored event are recorded

    Returns:
        Processed InsertModel with updated fields
    """

    if event is None or "data" not in event:
        return model

    event_payload = event["data"]
    if not isinstance(event_payload, dict):
        _malformed_event(issues, "unstructured event data is not an object")
        return model
    schema_uri = event_payload.get("schema")
    if not isinstance(schema_uri, str):
        _malformed_event(issues, "unstructured event has no schema")
        return model
    data = event_payload.get("data")
    if not _is_collector_schema(_schema_name_from_uri(schema_uri)):
        validation = validate_iglu_payload(schema_uri, data)
        _log_validation_result(
            validation,
            schema_uri=schema_uri,
            validation_stage="event",
            issues=issues,
        )

    if schema_uri == schemas.u2s_data:
        try:
            se = StructuredEvent.model_validate(data)
        except ValidationError as exc:
            _malformed_event(issues, f"structured event data is invalid: {exc.errors()[0]['msg']}")
            return model
        for field_name in StructuredEvent.model_fields:
            setattr(model, field_name, getattr(se, field_name))
        model.e = "se"
        return model

    # iglu:vendor/name/format/version: the name is the third segment from the end.
    segments = schema_uri.split("/")
    if len(segments) < _IGLU_URI_SEGMENTS or not segments[-3]:
        _malformed_event(issues, "unstructured event schema is not an Iglu URI", schema_uri)
        return model
    model.ue[segments[-3]] = data
    return model


_IGLU_URI_SEGMENTS = 4


def _malformed_event(issues: EventIssues | None, error: str, schema: str | None = None) -> None:
    _warn_malformed("Malformed unstructured event", error=error)
    if issues is not None:
        issues.add("event", error, schema=schema)


def _apply_amp_fields(result: InsertModel) -> None:
    """Promote AMP-specific fields from `result.amp` onto top-level fields."""

    # The ``uid`` test is kept as it was (it gates on a key the AMP contexts do
    # not send); only the KeyError on a missing ``userId`` and a non-string
    # value reaching the String column are fixed here.
    if "uid" in result.amp and (user_id := _as_str(result.amp.get("userId"))) is not None:
        del result.amp["userId"]
        result.uid = user_id
    if result.e == "ue" and "amp_page_ping" in result.ue:
        result.e = "pp"
        result.ue["amp_page_ping"] = result.ue.pop("amp_page_ping")
    if result.amp.get("domainUserid"):
        domain_userid = _optional_uuid(result.amp.pop("domainUserid"))
        if domain_userid is not None:
            result.duid = domain_userid


def _apply_amp_linker(result: InsertModel) -> None:
    """Decode the ``sp_amp_linker`` query param and extract the AMP device id."""

    # Checked for every event: skip URL parsing unless the param can be there.
    if not result.url or "sp_amp_linker" not in result.url:
        return

    try:
        parsed_url = urlparse.urlparse(result.url)
    except ValueError as e:  # e.g. an unterminated IPv6 host: "http://[::1"
        _warn_malformed("Failed to parse URL for AMP linker", error=str(e))
        return
    query_string = urlparse.parse_qs(parsed_url.query)
    if not query_string.get("sp_amp_linker"):
        return

    amp_linker = query_string["sp_amp_linker"][0]
    try:
        *_, amp_device_id = amp_linker.split("*")
        amp_device_id = parse_base64(amp_device_id)
        result.amp["device_id"] = UUID(amp_device_id)
    except Exception as e:
        _warn_malformed("Failed to parse AMP linker", error=str(e))


def _apply_cookie_duid(result: InsertModel, cookies: str | None) -> None:
    """Fall back to the Snowplow `_sp_id` cookie when `duid` is still unset."""

    if result.duid is not None:
        return
    sp_cookies = parse_cookies(cookies)
    if sp_cookies and "device_id" in sp_cookies:
        result.duid = _optional_uuid(sp_cookies["device_id"])


def _apply_screen_view(result: InsertModel) -> None:
    """Flatten a mobile ``screen_view`` unstructured event into a page view.

    Mobile dashboards read the screen name from ``url``, so it replaces the
    URL the tracker sent; ``parse_payload`` keeps that original in
    ``extra.tracker``. A screen view missing ``id`` or ``name`` keeps the
    view id and URL it already had rather than failing the event.
    """

    screen_view = result.ue.get("screen_view")
    if isinstance(screen_view, dict):
        result.e = "pv"
        screen_id = screen_view.pop("id", None)
        if screen_id is not None:
            result.view_id = _safe_uuid(screen_id)
        name = _as_str(screen_view.pop("name", None))
        if name is not None:
            result.url = name

        previous_name = _as_str(screen_view.pop("previousName", None))
        result.refr = "" if previous_name in (None, "Unknown") else previous_name

        result.screen.update(result.ue.pop("screen_view"))

    # A mobile screen context can also supply the URL directly.
    if "screen_name" in result.screen:
        screen_name = _as_str(result.screen.pop("screen_name"))
        if screen_name is not None:
            result.url = screen_name


def _apply_client_hints_device(result: InsertModel) -> None:
    """Derive ``device_is_mobile`` / ``device_is_pc`` from UA client hints."""

    client_hints = result.extra.get("client_hints")
    if not isinstance(client_hints, dict):
        return
    is_mobile = client_hints.get("isMobile")
    if is_mobile is None:
        return
    try:
        result.device_is_mobile = bool(int(is_mobile))
    except _CLIENT_HINT_ERRORS:
        _warn_malformed("Client hint isMobile is not a number", value_type=type(is_mobile).__name__)
        return
    result.device_is_pc = bool(int(not is_mobile))


def _record_undecodable(
    issues: EventIssues, plain_field: str, plain: str | None, encoded: str | None
) -> None:
    """Note a ``co``/``cx`` or ``ue_pr``/``ue_px`` value that decoded to nothing.

    Called only when decoding returned ``None``; the plain field wins when
    both are sent, as in ``find_available``.
    """

    if plain:
        issues.add("payload", "not a JSON object", field=plain_field)
    elif encoded:
        issues.add("payload", "not base64 of a JSON object", field=_ENCODED_FIELD[plain_field])


_ENCODED_FIELD = {"co": "cx", "ue_pr": "ue_px"}


@async_capture_span()
async def parse_payload(
    element: PayloadElementModel,
    user_agent: UserAgentModel | dict[str, Any],
    ip: IPv4Address,
    cookies: str | None,
    issues: EventIssues | None = None,
) -> InsertModel:
    """Parse a Snowplow event payload into an ``InsertModel``.

    ``issues`` collects ``extra.validation`` / ``extra.tracker`` entries; the
    caller applies them to the row (``EventIssues.apply``).
    """

    if issues is None:
        issues = EventIssues()
    result = build_initial_model(element, user_agent, ip)
    apply_platform(result, issues)
    apply_true_timestamp(element, result, issues)

    contexts = element.contexts
    if contexts is None:
        _record_undecodable(issues, "co", element.co, element.cx)
    result = await parse_contexts(contexts, result, issues)
    ue_context = element.ue_context
    if ue_context is None:
        _record_undecodable(issues, "ue_pr", element.ue_pr, element.ue_px)
    result = await parse_event(ue_context, result, issues)

    if element.ping_context:
        result.ue["page_ping"] = element.ping_context

    _apply_amp_fields(result)
    _apply_amp_linker(result)
    _apply_cookie_duid(result, cookies)
    _apply_screen_view(result)
    normalize_se_property(result)
    normalize_se_value(result)
    _apply_client_hints_device(result)
    keep_replaced_urls(element, result, issues)

    return result
