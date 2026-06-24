"""Utility helpers for decoding Snowplow tracker payloads."""

import base64

import orjson
import structlog
from core.tracing import capture_span

logger = structlog.get_logger(__name__)


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
    unencoded: str | dict | None,
    encoded: str | dict | None,
) -> dict | None:
    """Return the first available payload as a dict, decoding base64 if needed.

    Prefers ``unencoded`` over ``encoded``. Returns ``None`` when neither
    yields a valid JSON object.
    """
    result = None

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
        logger.warning(
            "Failed to decode Snowplow JSON payload",
            error=str(exc),
            value_type=type(result).__name__,
        )
        return None

    if isinstance(parsed, dict):
        return parsed

    logger.warning(
        "Snowplow JSON payload has unexpected type",
        decoded_type=type(parsed).__name__,
    )
    return None
