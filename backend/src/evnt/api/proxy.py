"""First-party proxy for third-party analytics scripts.

``POST /proxy/hash`` turns the URL of an allowlisted script (Google Analytics,
GTM) into a URL on the collector's own origin; ``GET /proxy/route/...`` fetches
it from upstream and streams it back, so ad blockers and third-party cookie
rules do not drop it.

The route is a fetch-anything primitive by nature, so it only reaches hosts in
``proxy.domains``, on ports in ``proxy.allowed_ports``, and never follows
redirects (see ``lifespan._configure_proxy_http_client``).
"""

import base64
import binascii
from typing import Final, cast

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import AnyHttpUrl, AnyUrl, BaseModel, Field
from starlette.background import BackgroundTask

from evnt.config import settings
from evnt.constants import CONTENT_TYPE_OCTET_STREAM

logger = structlog.get_logger(__name__)

PROXY_ENDPOINT = settings.common.snowplow.endpoints.proxy_endpoint
HOSTNAME = settings.common.hostname
ALLOWED_PROXY_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
# Fallback used only if the lifespan did not populate the port allowlist.
DEFAULT_ALLOWED_PROXY_PORTS: Final[frozenset[int]] = frozenset({80, 443})


class HashModel(BaseModel):
    """Request body for the proxy hash endpoint."""

    url: AnyUrl = Field(..., title="URL to hash")


router = APIRouter(tags=["proxy"], prefix=PROXY_ENDPOINT)


def _encode_url_part(value: str) -> str:
    """URL-safe base64, so a host or a path with slashes fits one path segment."""
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _decode_url_part(value: str) -> str:
    return base64.urlsafe_b64decode(value).decode("utf-8")


def _normalize_hostname(hostname: str) -> str:
    """Normalize a hostname for allowlist comparison."""
    return hostname.rstrip(".").lower()


def _is_proxied_host(host: str | None) -> bool:
    if host is None:
        return False
    allowed = {_normalize_hostname(domain) for domain in settings.proxy.domains}
    return _normalize_hostname(host) in allowed


def _parse_proxy_target_url(schema: str, host: str, path: str) -> httpx.URL:
    target_url = httpx.URL(f"{schema}://{host}/{path}")
    if not target_url.host:
        raise ValueError("Proxy target is missing a hostname")
    return target_url


def _state_value[T](request: Request, name: str, default: T | None = None) -> T:
    value = getattr(request.app.state, name, default)
    if value is None:
        raise HTTPException(status_code=500, detail="Proxy is not ready")
    return cast(T, value)


@router.post("/hash", response_model=AnyHttpUrl, status_code=200)
async def proxy_hash(data: HashModel) -> AnyHttpUrl:
    """Return the proxied form of ``url``, or ``url`` unchanged.

    Only URLs on an allowlisted host are rewritten, since the route refuses
    every other host. Host and path (with its query) are each base64-encoded
    into a single path segment.
    """
    url = data.url
    if url.scheme not in ALLOWED_PROXY_SCHEMES or not _is_proxied_host(url.host):
        return AnyHttpUrl(str(url))

    host = cast(str, url.host)
    target = (url.path or "/").removeprefix("/")
    if url.query:
        target += f"?{url.query}"

    route = f"{PROXY_ENDPOINT}/route/{url.scheme}/{_encode_url_part(host)}"
    route += f"/{_encode_url_part(target)}"
    return AnyHttpUrl.build(
        scheme=HOSTNAME.scheme,
        host=cast(str, HOSTNAME.host),
        port=HOSTNAME.port,
        path=route.removeprefix("/"),
    )


@router.get(
    "/route/{schema}/{host}/{path}",
    responses={
        400: {"description": "Unsupported proxy scheme or invalid proxy target"},
        403: {"description": "Proxy target host or port not allowed"},
        502: {"description": "Proxy request to the upstream target failed"},
        504: {"description": "Proxy request to the upstream target timed out"},
    },
)
async def proxy(request: Request, schema: str, host: str, path: str = "") -> StreamingResponse:
    """Fetch an allowlisted upstream resource and stream it back.

    ``host`` and ``path`` are the base64 segments produced by ``/hash``.
    """
    if schema not in ALLOWED_PROXY_SCHEMES:
        raise HTTPException(status_code=400, detail="Unsupported proxy scheme")

    try:
        decoded_host = _decode_url_part(host)
        decoded_path = _decode_url_part(path)
        target_url = _parse_proxy_target_url(schema, decoded_host, decoded_path)
    except (ValueError, binascii.Error, UnicodeDecodeError, httpx.InvalidURL) as exc:
        raise HTTPException(status_code=400, detail="Invalid proxy target") from exc

    # Without the host allowlist the endpoint is a generic SSRF gadget (cloud
    # metadata, internal services, localhost, ...).
    allowed_hosts: frozenset[str] = _state_value(request, "proxy_allowed_hosts")
    if _normalize_hostname(target_url.host) not in allowed_hosts:
        raise HTTPException(status_code=403, detail="Proxy target not allowed")

    # The allowlist only compares hostnames, so "google-analytics.com:9200"
    # would otherwise reach any port. A target without an explicit port uses
    # the scheme default and is always permitted.
    allowed_ports: frozenset[int] = _state_value(
        request,
        "proxy_allowed_ports",
        DEFAULT_ALLOWED_PROXY_PORTS,
    )
    if target_url.port is not None and target_url.port not in allowed_ports:
        raise HTTPException(status_code=403, detail="Proxy target port not allowed")

    client: httpx.AsyncClient = _state_value(request, "proxy_http_client")
    try:
        response = await client.send(client.build_request("GET", target_url), stream=True)
    except httpx.TimeoutException as exc:
        logger.warning("Proxy request timed out", url=str(target_url))
        raise HTTPException(
            status_code=504,
            detail=f"Proxy request to '{decoded_host}' timed out",
        ) from exc
    except httpx.RequestError as exc:
        logger.warning("Proxy request failed", url=str(target_url), error=str(exc))
        raise HTTPException(
            status_code=502,
            detail=f"Proxy request to '{decoded_host}' failed",
        ) from exc

    return StreamingResponse(
        response.aiter_bytes(),
        status_code=response.status_code,
        media_type=response.headers.get("Content-Type", CONTENT_TYPE_OCTET_STREAM),
        background=BackgroundTask(response.aclose),
    )
