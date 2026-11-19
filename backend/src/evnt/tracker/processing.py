"""
Core data processing handlers for Snowplow events.

Every event of a request is built on its own: a malformed or crashing event
costs only itself, never the rest of its batch (see ``evnt.tracker.element``
for why a batch must not fail as a whole).
"""

import asyncio
from collections.abc import Mapping, Sequence
from ipaddress import IPv4Address, IPv6Address
from typing import Any

import structlog

from evnt.observability.ratelimit import KeyedRateLimiter
from evnt.observability.tracing import async_capture_span
from evnt.tracker.element import EventIssues, build_elements, clip, drop_event
from evnt.tracker.fingerprint import event_fingerprint
from evnt.tracker.geo import Geo
from evnt.tracker.ip import convert_ip, extract_ip_from_header
from evnt.tracker.models import (
    PayloadElementModel,
    PayloadModel,
)
from evnt.tracker.params import RequestAgents, event_ip
from evnt.tracker.payload import parse_payload
from evnt.tracker.rows import build_fallback_model, dump_insert_model
from evnt.tracker.useragent import agent_fields_for_insert_async

logger = structlog.get_logger(__name__)

# Events processed between yields to the event loop (about 10-20 ms of work).
_EVENTS_PER_YIELD = 64

# A payload that trips a parsing bug does so on every event a tracker sends,
# so the line is rate-limited per exception type.
_PROCESSING_ERROR_LOG = KeyedRateLimiter()
_MAX_ERROR_TEXT = 200

type EventSource = PayloadModel | PayloadElementModel | Mapping[str, Any]


def reset_log_rate_limits() -> None:
    """Forget every rate-limited warning key, so tests start from a clean slate."""

    _PROCESSING_ERROR_LOG.reset()


def _events_of(body: EventSource) -> Sequence[Any]:
    """The events of a request: a POST batch, or the one event of a pixel."""

    if isinstance(body, PayloadModel):
        return body.data
    return (body,)


def _log_processing_error(exc: Exception) -> None:
    key = type(exc).__name__
    if (permit := _PROCESSING_ERROR_LOG.take(key)) is None:
        return
    if permit.evicted is not None:
        evicted_key, suppressed = permit.evicted
        logger.warning(
            "Tracker event failed to parse",
            exc_type=evicted_key,
            suppressed=suppressed,
            evicted=True,
        )
    logger.warning(
        "Tracker event failed to parse",
        **permit.log_fields(),
        exc_type=key,
        error=clip(str(exc), _MAX_ERROR_TEXT),
        exc_info=exc,
    )


# ``extra.tracker`` keys recorded before parsing: from validation and from
# ``evnt.tracker.params``, which the fallback row still applies.
_PRE_PARSE_TRACKER_KEYS = ("platform", "ua", "ip", "network_userid")


def _issues_before_parsing(issues: EventIssues) -> EventIssues:
    """Keep what was recorded before parsing, drop what parsing added."""

    kept = EventIssues()
    for entry in issues.validation or ():
        if entry["stage"] == "payload":
            kept.add("payload", entry["error"], field=entry.get("field"))
    for key in _PRE_PARSE_TRACKER_KEYS:
        if issues.tracker and key in issues.tracker:
            kept.track(key, issues.tracker[key])
    return kept


async def _build_row(
    element: PayloadElementModel,
    issues: EventIssues,
    ua_data: dict[str, Any],
    user_ip: IPv4Address,
    cookies: str | None,
) -> tuple[dict[str, Any], EventIssues] | None:
    """Turn one validated tracker event into a row, or ``None`` if that fails."""

    try:
        model = await parse_payload(element, ua_data, user_ip, cookies, issues)
    except Exception as exc:
        # A bug reachable from tracker input must not fail the batch: as a
        # 5xx the tracker would resend it forever. The event is stored with
        # its validated atomic fields only, so it still counts in every
        # report, and the failure is visible in extra.validation and the log.
        _log_processing_error(exc)
        issues = _issues_before_parsing(issues)
        issues.add("processing", f"{type(exc).__name__}: {exc}")
        try:
            model = build_fallback_model(element, ua_data, user_ip, issues)
        except Exception as fallback_exc:
            drop_event("processing_error", exc_type=type(fallback_exc).__name__)
            return None
    return dump_insert_model(model), issues


def _collector_metadata(
    client_ip: IPv4Address | IPv6Address | None,
    user_country: str | None,
    geo: Geo | None,
    *,
    anonymous: bool,
) -> dict[str, Any]:
    """``extra.collector`` of every row of one request."""

    if anonymous:
        return {"anonymous": True}
    collector: dict[str, Any] = {
        "client_ip": str(client_ip) if client_ip is not None else "",
        "ip_country": user_country or "",
    }
    if geo:
        collector["geo"] = geo
    return collector


@async_capture_span()
async def process_data(
    body: EventSource,
    user_agent: str | None,
    user_ip: IPv4Address | IPv6Address | str | None,
    cookies: str | None,
    user_country: str | None = None,
    geo: Geo | None = None,
    anonymous: bool = False,
) -> list[dict[str, Any]]:
    """
    Process incoming event data from various sources.

    This function:
    1. Processes the IP address
    2. Parses the user agent
    3. Validates and parses every event on its own
    4. Combines all information into complete event records

    Args:
        body: A POST envelope, or the single event of a GET pixel (its query
            parameters, or an already validated element)
        user_agent: User agent string from headers; an event's ``ua``
            parameter replaces it for that event
        user_ip: IP address from headers; an ``srv`` event's ``ip``
            parameter replaces it in that event's ``user_ip``, never in
            ``extra.collector.client_ip``
        cookies: Cookie string from headers
        user_country: Validated country code from the trusted proxy header,
            stored as ``extra.collector.ip_country``
        geo: Validated visitor location from the trusted proxy headers,
            stored as ``extra.collector.geo``
        anonymous: The request carried ``SP-Anonymous`` and the collector
            honours it: no IP (header or ``ip`` param), country, location or
            ``_sp_id`` cookie is used, and ``extra.collector`` holds only
            ``anonymous: true``

    Returns:
        Rows ready for storage; events that cannot be built are left out
    """
    if anonymous:
        # Enforced here as well as by the caller, so no path can leak them.
        user_ip = user_country = geo = cookies = None
    client_ip = extract_ip_from_header(user_ip) if isinstance(user_ip, str) else user_ip
    stored_ip = convert_ip(client_ip)
    agents = RequestAgents(await agent_fields_for_insert_async(user_agent))
    collector = _collector_metadata(client_ip, user_country, geo, anonymous=anonymous)

    raws = _events_of(body)
    result = []
    for index, ((element, issues), raw) in enumerate(zip(build_elements(raws), raws, strict=True)):
        # parse_payload never really suspends, so without this a large batch
        # holds the event loop for its whole run (about 1 s for 5000 events)
        # and every other request waits behind it.
        if index and index % _EVENTS_PER_YIELD == 0:
            await asyncio.sleep(0)
        if element is None:
            continue
        ua_data = await agents.fields_for(element, issues)
        user_ip_for_event = event_ip(element, issues, stored_ip, anonymous=anonymous)
        built = await _build_row(element, issues, ua_data, user_ip_for_event, cookies)
        if built is None:
            continue
        item_data, issues = built
        issues.fingerprint = event_fingerprint(raw)
        # A copy per row: rows are separate documents downstream. ``geo`` inside
        # it is shared, as nothing after this point modifies a row's ``extra``.
        extra = {**item_data["extra"], "collector": {**collector}}
        issues.apply(extra)
        item_data["extra"] = extra
        result.append(item_data)
    return result
