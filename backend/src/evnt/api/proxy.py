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
import re
from typing import Annotated, Final, cast

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
# Caching validators: without them a browser refetches the script through the
# collector on every page view.
CONDITIONAL_REQUEST_HEADERS: Final[tuple[str, ...]] = ("If-None-Match", "If-Modified-Since")
FORWARDED_RESPONSE_HEADERS: Final[tuple[str, ...]] = (
    "Cache-Control",
    "ETag",
    "Last-Modified",
    "Expires",
)
# A hostname, IPv4 address or bracketed IPv6 address with an optional port.
# Underscores are not valid DNS but are common in container service names.
_HOST_HEADER_RE: Final = re.compile(r"(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?")


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


def _forwarded_scheme(request: Request) -> str:
    """Scheme the client used, honouring ``X-Forwarded-Proto`` behind a trusted proxy."""
    if settings.security.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if forwarded in ALLOWED_PROXY_SCHEMES:
            return forwarded
    return request.url.scheme


async def _public_origin(request: Request) -> AnyHttpUrl:
    """Origin the hashed URLs point at.

    An explicitly configured ``common.hostname`` always wins. Otherwise the
    origin is the one the request arrived on: its ``Host`` header (vetted by
    ``security.trusted_hosts`` when that is configured) and its scheme.
    """
    if settings.common.hostname_configured:
        return settings.common.hostname
    host = request.headers.get("host", "")
    if _HOST_HEADER_RE.fullmatch(host):
        try:
            return AnyHttpUrl(f"{_forwarded_scheme(request)}://{host}")
        except ValueError:
            pass
    logger.warning(
        "Unusable Host header, proxy hash falls back to the default origin",
        host=host,
        origin=str(settings.common.hostname),
    )
    return settings.common.hostname


@router.post("/hash", response_model=AnyHttpUrl, status_code=200)
async def proxy_hash(
    data: HashModel,
    origin: Annotated[AnyHttpUrl, Depends(_public_origin)] = HOSTNAME,
) -> AnyHttpUrl:
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
        scheme=origin.scheme,
        host=cast(str, origin.host),
        port=origin.port,
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
async def proxy(request: Request, schema: str, host: str, path: str = "") -> Response:
    """Fetch an allowlisted upstream resource and stream it back.

    ``host`` and ``path`` are the base64 segments produced by ``/hash``. The
    browser's cache validators go upstream and the upstream caching headers
    come back, so a cached script revalidates with a bodyless ``304``.
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
    conditional = {
        name: value for name in CONDITIONAL_REQUEST_HEADERS if (value := request.headers.get(name))
    }
    upstream_request = client.build_request("GET", target_url, headers=conditional)
    try:
        response = await client.send(upstream_request, stream=True)
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

    headers = {
        name: value for name in FORWARDED_RESPONSE_HEADERS if (value := response.headers.get(name))
    }
    if response.status_code == 304:
        await response.aclose()
        return Response(status_code=304, headers=headers)

    return StreamingResponse(
        response.aiter_bytes(),
        status_code=response.status_code,
        headers=headers,
        media_type=response.headers.get("Content-Type", CONTENT_TYPE_OCTET_STREAM),
        background=BackgroundTask(response.aclose),
    )
