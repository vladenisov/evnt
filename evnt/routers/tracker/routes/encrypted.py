"""
Route handlers for the encrypted ingest endpoint.

This is the sealed-payload twin of the plain Snowplow endpoints: the client
encrypts the exact same Snowplow JSON body with the collector's public key, and
this module unseals it and hands it to the shared processing path. Clients hook
in by replacing the tracker's network layer -- ``NetworkConnection`` on the
mobile trackers -- so nothing about the event model changes.

See ``core.crypto`` for the envelope format and the key schedule.
"""

from ipaddress import IPv4Address, IPv6Address
from typing import Final

import orjson
import structlog
from core.config import settings
from core.constants import CONTENT_TYPE_GIF, TRACKING_PIXEL
from core.crypto import DecryptionError, Keyring, coerce_envelope_bytes, open_envelope
from core.dependencies import DbConnector
from core.tracing import async_capture_span
from fastapi import Depends, Header, HTTPException, Query, Request, Response
from pydantic import ValidationError
from routers.tracker.handlers import process_data
from routers.tracker.models.snowplow import PayloadModel
from routers.tracker.routes.snowplow import get_user_ip_from_configured_header
from starlette.status import (
    HTTP_204_NO_CONTENT,
    HTTP_400_BAD_REQUEST,
    HTTP_413_CONTENT_TOO_LARGE,
    HTTP_503_SERVICE_UNAVAILABLE,
)

logger = structlog.get_logger(__name__)

# One opaque message for every rejection. A sender that cannot produce a valid
# envelope must not learn whether it got the key id, the tag, or the JSON
# wrong, since that turns the endpoint into an oracle.
_REJECTION_DETAIL: Final[str] = "invalid encrypted payload"


def get_keyring(request: Request) -> Keyring:
    """Return the keyring resolved during application startup."""
    keyring = getattr(request.app.state, "keyring", None)
    if keyring is None:
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="encrypted ingest is not ready",
        )
    return keyring


def _rejection(reason: str, **context: object) -> HTTPException:
    """Log why a payload was dropped and build the uniform client error.

    Returns rather than raises so call sites read ``raise _rejection(...)``
    and stay visibly terminal.
    """
    logger.warning("Rejected encrypted payload", reason=reason, **context)
    return HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=_REJECTION_DETAIL)


async def _read_limited_body(request: Request, limit: int) -> bytes:
    """
    Read the request body, refusing anything over ``limit``.

    Streaming with a running total means an oversized upload is cut off as it
    arrives instead of being buffered in full and rejected afterwards.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise HTTPException(
                    status_code=HTTP_413_CONTENT_TOO_LARGE,
                    detail="payload too large",
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="malformed content-length",
            ) from exc

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=HTTP_413_CONTENT_TOO_LARGE,
                detail="payload too large",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _unseal(keyring: Keyring, raw: bytes) -> PayloadModel:
    """Turn envelope bytes into a validated Snowplow payload."""
    config = settings.encryption
    try:
        envelope = coerce_envelope_bytes(raw)
        plaintext = open_envelope(keyring, envelope, config.max_plaintext_bytes)
    except DecryptionError as exc:
        raise _rejection(exc.message, **exc.details) from exc

    try:
        document = orjson.loads(plaintext)
    except orjson.JSONDecodeError as exc:
        raise _rejection("plaintext is not JSON") from exc

    try:
        return PayloadModel.model_validate(document)
    except ValidationError as exc:
        raise _rejection(
            "plaintext failed schema validation",
            errors=exc.error_count(),
        ) from exc


@async_capture_span()
async def encrypted_cors() -> None:
    """Handle CORS preflight requests for the encrypted endpoint."""
    return


@async_capture_span()
async def encrypted_post(
    request: Request,
    connector: DbConnector,
    keyring: Keyring = Depends(get_keyring),
    user_agent: str | None = Header(None),
    user_ip: IPv4Address | IPv6Address | None = Depends(
        get_user_ip_from_configured_header,
    ),
    cookie: str | None = Header(None),
) -> Response:
    """
    Handle POST requests carrying a sealed Snowplow payload.

    The body is either the raw binary envelope
    (``application/octet-stream``) or its base64 rendering, which is what a
    browser transport that cannot send bytes will produce.

    Args:
        request: Raw request, read directly so the body stays unparsed bytes
        connector: Database connector (injected)
        keyring: Decryption keys resolved at startup (injected)
        user_agent: User agent header
        user_ip: IP address from configured proxy header
        cookie: Browser cookies

    Returns:
        Empty response with 204 status code
    """
    raw = await _read_limited_body(request, settings.encryption.max_envelope_bytes)
    body = _unseal(keyring, raw)

    data = await process_data(body, user_agent, user_ip, cookie)
    await connector.insert_rows(data)

    return Response(status_code=HTTP_204_NO_CONTENT)


@async_capture_span()
async def encrypted_get(
    connector: DbConnector,
    d: str = Query(..., description="Base64url-encoded envelope"),
    keyring: Keyring = Depends(get_keyring),
    user_agent: str | None = Header(None),
    user_ip: IPv4Address | IPv6Address | None = Depends(
        get_user_ip_from_configured_header,
    ),
    cookie: str | None = Header(None),
) -> Response:
    """
    Handle GET requests carrying a sealed payload in the query string.

    This is the pixel fallback for transports that cannot POST. Query strings
    are size-capped by every proxy in the path, so it carries its own much
    smaller ceiling (``max_query_bytes``) and suits single events only.

    Args:
        connector: Database connector (injected)
        d: Base64url-encoded envelope
        keyring: Decryption keys resolved at startup (injected)
        user_agent: User agent header
        user_ip: IP address from configured proxy header
        cookie: Browser cookies

    Returns:
        1x1 transparent GIF pixel response
    """
    if len(d) > settings.encryption.max_query_bytes:
        raise HTTPException(
            status_code=HTTP_413_CONTENT_TOO_LARGE,
            detail="payload too large",
        )
    try:
        raw = d.encode("ascii")
    except UnicodeEncodeError as exc:
        raise _rejection("query parameter is not ASCII") from exc
    body = _unseal(keyring, raw)

    data = await process_data(body, user_agent, user_ip, cookie)
    await connector.insert_rows(data)

    return Response(content=TRACKING_PIXEL, media_type=CONTENT_TYPE_GIF)
