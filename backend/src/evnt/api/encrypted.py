"""
Encrypted ingest endpoint.

This is the sealed-payload twin of the plain Snowplow endpoints: the client
encrypts the exact same Snowplow JSON body with the collector's public key, and
this module unseals it and hands it to the shared processing path. Clients hook
in by replacing the tracker's network layer -- ``NetworkConnection`` on the
mobile trackers -- so nothing about the event model changes.

See ``evnt.crypto`` for the envelope format and the key schedule.
"""

import base64
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final

import orjson
import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import ValidationError
from starlette.status import (
    HTTP_204_NO_CONTENT,
    HTTP_400_BAD_REQUEST,
    HTTP_413_CONTENT_TOO_LARGE,
)

from evnt.api.deps import ClientIP, DbConnector, KeyringDep
from evnt.config import settings
from evnt.constants import CONTENT_TYPE_GIF, CONTENT_TYPE_JAVASCRIPT, TRACKING_PIXEL
from evnt.crypto import DecryptionError, Keyring, coerce_envelope_bytes, open_envelope
from evnt.observability.tracing import async_capture_span
from evnt.tracker.models import PayloadModel
from evnt.tracker.processing import process_data

logger = structlog.get_logger(__name__)

# One opaque message for every rejection. A sender that cannot produce a valid
# envelope must not learn whether it got the key id, the tag, or the JSON
# wrong, since that turns the endpoint into an oracle.
_REJECTION_DETAIL: Final[str] = "invalid encrypted payload"

# The browser sealer, served with its key material substituted in.
_SEAL_SCRIPT_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "seal.js"
)
_SEAL_CONFIG_TOKEN: Final[str] = "__EVNT_CONFIG__"
# Long enough that the script is not refetched on every page view, short enough
# that a key rotation reaches browsers within the hour without a purge.
_SEAL_SCRIPT_MAX_AGE: Final[int] = 900


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


@lru_cache(maxsize=4)
def _render_seal_script(public_key_b64: str, kid: str, endpoint: str) -> bytes:
    """
    Substitute key material into the browser sealer.

    Cached on what it interpolates rather than read per request: the keyring
    only changes on restart, so a page view should not cost a file read plus a
    template pass.
    """
    config = orjson.dumps(
        {
            "publicKey": public_key_b64,
            "kid": kid,
            "endpoint": endpoint,
            "compress": True,
        },
    ).decode()
    template = _SEAL_SCRIPT_PATH.read_text(encoding="utf-8")
    return template.replace(_SEAL_CONFIG_TOKEN, config).encode()


@async_capture_span()
async def encrypted_cors() -> None:
    """Answer CORS preflight requests; the CORS middleware adds the headers."""


@async_capture_span()
async def encrypted_script(keyring: KeyringDep) -> Response:
    """
    Serve the browser sealer with this collector's public key baked in.

    Handing the key out from the keyring rather than from a build artifact is
    what keeps key material out of tag manager containers and app bundles, and
    makes rotation a collector config change: browsers pick the new key up as
    their cached copy expires, while the old one keeps opening envelopes that
    are still in flight.
    """
    key_pair = keyring.primary
    body = _render_seal_script(
        base64.b64encode(key_pair.public_key).decode(),
        key_pair.kid,
        settings.encryption.endpoint,
    )
    return Response(
        content=body,
        media_type=CONTENT_TYPE_JAVASCRIPT,
        headers={"Cache-Control": f"public, max-age={_SEAL_SCRIPT_MAX_AGE}"},
    )


@async_capture_span()
async def encrypted_post(
    request: Request,
    connector: DbConnector,
    keyring: KeyringDep,
    user_ip: ClientIP,
    user_agent: Annotated[str | None, Header()] = None,
    cookie: Annotated[str | None, Header()] = None,
) -> Response:
    """
    Store a sealed Snowplow batch.

    The body is either the raw binary envelope (``application/octet-stream``)
    or its base64 rendering, which is what a browser transport that cannot
    send bytes will produce. It is read straight from the request so it stays
    unparsed bytes.
    """
    raw = await _read_limited_body(request, settings.encryption.max_envelope_bytes)
    body = _unseal(keyring, raw)

    data = await process_data(body, user_agent, user_ip, cookie)
    await connector.insert_rows(data)
    return Response(status_code=HTTP_204_NO_CONTENT)


@async_capture_span()
async def encrypted_get(
    connector: DbConnector,
    d: Annotated[str, Query(description="Base64url-encoded envelope")],
    keyring: KeyringDep,
    user_ip: ClientIP,
    user_agent: Annotated[str | None, Header()] = None,
    cookie: Annotated[str | None, Header()] = None,
) -> Response:
    """
    Store a sealed single event sent in the query string.

    This is the pixel fallback for transports that cannot POST. Query strings
    are size-capped by every proxy in the path, so it carries its own much
    smaller ceiling (``max_query_bytes``) and suits single events only.
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


def build_router() -> APIRouter:
    """Build the router for the encrypted twin of the collector endpoints.

    A factory because the endpoint is opt-in: ``create_app`` mounts it only
    when encryption is enabled, so a deployment that does not seal payloads
    exposes no extra surface. Both methods share one path since, unlike the
    plain endpoints, there is no legacy Snowplow path contract to honour.
    """
    endpoint = settings.encryption.endpoint
    router = APIRouter(tags=["tracker"])

    router.options(
        endpoint,
        include_in_schema=False,
        status_code=HTTP_204_NO_CONTENT,
    )(encrypted_cors)

    router.post(
        endpoint,
        summary="Encrypted Snowplow endpoint",
        description=(
            "Accepts a sealed Snowplow payload as raw bytes "
            "(application/octet-stream) or base64 text."
        ),
        status_code=HTTP_204_NO_CONTENT,
    )(encrypted_post)

    router.get(
        endpoint,
        summary="Encrypted Snowplow GET endpoint",
        description="Pixel fallback carrying a base64url envelope in `d`.",
        response_class=Response,
        responses={
            200: {
                "content": {CONTENT_TYPE_GIF: {}},
                "description": "1x1 transparent GIF tracking pixel",
            },
        },
    )(encrypted_get)

    # Served from the endpoint path plus `.js` so the browser resolves the
    # collector origin from the script's own URL, with no second host to
    # configure and no key material in the page.
    router.get(
        f"{endpoint}.js",
        summary="Browser sealer for the encrypted endpoint",
        description=(
            "JavaScript that seals Snowplow batches, served with this "
            "collector's public key and key id substituted in."
        ),
        response_class=Response,
        responses={
            200: {
                "content": {CONTENT_TYPE_JAVASCRIPT: {}},
                "description": "The browser sealing module",
            },
        },
    )(encrypted_script)

    return router
