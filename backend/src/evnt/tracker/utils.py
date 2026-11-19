"""Utility helpers for decoding Snowplow tracker payloads."""

import base64
from typing import Any, cast

import orjson
import structlog

from evnt.observability.ratelimit import KeyedRateLimiter
from evnt.observability.tracing import capture_span

logger = structlog.get_logger(__name__)

# A client can send an undecodable ``ue_px``/``cx`` (or ``ue_pr``/``co``) on every
# event, so these warnings are rate-limited the same way as the malformed-input
# warnings in ``evnt.tracker.payload``: lines carry ``suppressed``, the
# occurrences of that warning held back since its previous line.
_MALFORMED_JSON_LOG = KeyedRateLimiter()  # keyed by the warning text


def reset_log_rate_limits() -> None:
    """Forget every rate-limited warning key, so tests start from a clean slate."""

    _MALFORMED_JSON_LOG.reset()


def _warn_malformed(event: str, **fields: Any) -> None:
    """Log a fixed-text warning about malformed client input, rate-limited by text."""

    if (permit := _MALFORMED_JSON_LOG.take(event)) is not None:
        if permit.evicted is not None:
            key, suppressed = permit.evicted
            logger.warning(key, suppressed=suppressed, evicted=True)
        logger.warning(event, **permit.log_fields(), **fields)


@capture_span()
def parse_base64(data: str | bytes) -> str:
    """
    Parse base64 encoded data (supports both standard and URL-safe base64).

    Args:
        data: The base64 encoded data

    Returns:
        Decoded string
    """

    if isinstance(data, str):
        data_bytes: bytes = data.encode("UTF-8")
    elif isinstance(data, (bytearray, memoryview)):
        data_bytes = bytes(data)
    else:
        data_bytes = data  # already bytes

    missing_padding = len(data_bytes) % 4
    if missing_padding:
        data_bytes = data_bytes + (b"=" * (4 - missing_padding))

    return base64.urlsafe_b64decode(data_bytes).decode("UTF-8")


def find_available(
    unencoded: str | dict[str, Any] | None,
    encoded: str | None,
) -> dict[str, Any] | None:
    """Return the first available payload as a dict, decoding base64 if needed.

    Prefers ``unencoded`` over ``encoded``. Returns ``None`` when neither
    yields a valid JSON object.
    """
    result: str | dict[str, Any] | None = None

    if unencoded:
        result = unencoded
    elif encoded:
        result = parse_base64(encoded)

    if result is None:
        return None

    if isinstance(result, dict):
        return result

    try:
        parsed = orjson.loads(result)
    except (orjson.JSONDecodeError, TypeError) as exc:
        _warn_malformed(
            "Failed to decode Snowplow JSON payload",
            error=str(exc),
            value_type=type(result).__name__,
        )
        return None

    if isinstance(parsed, dict):
        return cast(dict[str, Any], parsed)

    _warn_malformed(
        "Snowplow JSON payload has unexpected type",
        decoded_type=type(parsed).__name__,
    )
    return None
