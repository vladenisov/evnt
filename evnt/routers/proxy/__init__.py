"""
Proxy router for evnt.

This module provides proxy functionality for external analytics scripts,
allowing them to be served through the same domain as the application.
"""

from __future__ import annotations

import base64
from typing import Final

import httpx
import structlog
from core.config import settings
from core.constants import CONTENT_TYPE_OCTET_STREAM
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRouter
from pydantic import AnyHttpUrl
from routers.proxy import models
from starlette.background import BackgroundTask

logger = structlog.get_logger(__name__)

PROXY_CONFIG = settings.proxy
PROXY_ENDPOINT = settings.common.snowplow.endpoints.proxy_endpoint
HOSTNAME = settings.common.hostname
ALLOWED_PROXY_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
# Fallback used only if the lifespan did not populate the port allowlist.
DEFAULT_ALLOWED_PROXY_PORTS: Final[frozenset[int]] = frozenset({80, 443})


def _normalize_hostname(hostname: str) -> str:
    """Normalize hostnames for allowlist comparison."""
    return hostname.rstrip(".").lower()


router = APIRouter(tags=["proxy"], prefix=PROXY_ENDPOINT)


def _encode_url_part(s: str) -> str:
    """URL-safe base64 encode a string."""
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("utf-8")


def _decode_url_part(s: str) -> str:
    """URL-safe base64 decode a string."""
    return base64.urlsafe_b64decode(s).decode("utf-8")


def _parse_proxy_target_url(schema: str, host: str, path: str) -> httpx.URL:
    """Build the proxy target URL and expose the parsed hostname."""
    target_url = httpx.URL(f"{schema}://{host}/{path}")
    if not target_url.host:
        raise ValueError("Proxy target is missing a hostname")
    return target_url


def _should_encode_domain(domain: str | None) -> bool:
    """Check if the domain should be encoded."""
    return domain in PROXY_CONFIG.domains


def _should_encode_path(path: str) -> bool:
    """Check if the path should be encoded."""
    # Remove leading slash for comparison
    clean_path = path.lstrip("/")
    return clean_path in PROXY_CONFIG.paths


def _get_proxy_allowed_hosts(request: Request) -> frozenset[str]:
    """Return the lifespan-owned proxy host allowlist."""

    allowed_hosts = getattr(request.app.state, "proxy_allowed_hosts", None)
    if allowed_hosts is None:
        raise HTTPException(status_code=500, detail="Proxy is not ready")
    return allowed_hosts


def _get_proxy_allowed_ports(request: Request) -> frozenset[int]:
    """Return the lifespan-owned set of permitted outbound ports."""

    allowed_ports = getattr(request.app.state, "proxy_allowed_ports", None)
    if allowed_ports is None:
        return DEFAULT_ALLOWED_PROXY_PORTS
    return allowed_ports


def _get_proxy_http_client(request: Request) -> httpx.AsyncClient:
    """Return the lifespan-owned outbound HTTP client for proxy requests."""

    client = getattr(request.app.state, "proxy_http_client", None)
    if client is None:
        raise HTTPException(status_code=500, detail="Proxy HTTP client is not ready")
    return client


@router.post("/hash", response_model=AnyHttpUrl, status_code=200)
async def proxy_hash(data: models.HashModel) -> AnyHttpUrl:
    """
    Generate a proxied URL for an external resource.

    Args:
        data: The URL to proxy

    Returns:
        The proxied URL if applicable, otherwise the original URL
    """
    should_encode = False

    domain = data.url.host
    if _should_encode_domain(domain):
        domain = _encode_url_part(domain)
        should_encode = True

    full_path = data.url.path[1:]
    if data.url.query is not None:
        full_path += f"?{data.url.query}"

    if _should_encode_path(data.url.path):
        full_path = _encode_url_part(full_path)
        should_encode = True

    if not should_encode:
        return AnyHttpUrl(str(data.url))

    path = f"{PROXY_ENDPOINT}/route/{data.url.scheme}"
    path += f"/{_encode_url_part(data.url.host)}/{full_path}"

    if path.startswith("/"):
        path = path[1:]

    result = AnyHttpUrl.build(
        scheme=HOSTNAME.scheme,
        host=HOSTNAME.host,
        port=HOSTNAME.port,
        path=path,
    )

    return result


@router.get(
    "/route/{schema}/{host}/{path}",
    responses={
        400: {"description": "Unsupported proxy scheme or invalid proxy target"},
        403: {"description": "Proxy target host or port not allowed"},
        502: {"description": "Proxy request to the upstream target failed"},
        504: {"description": "Proxy request to the upstream target timed out"},
    },
)
async def proxy(
    request: Request,
    schema: str,
    host: str,
    path: str = "",
) -> StreamingResponse:
    """
    Proxy a request to an external resource.

    Args:
        schema: The URL scheme (http/https)
        host: The base64-encoded host
        path: The base64-encoded path

    Returns:
        The proxied response

    Raises:
        HTTPException: If the proxy request fails
    """
    if schema not in ALLOWED_PROXY_SCHEMES:
        raise HTTPException(status_code=400, detail="Unsupported proxy scheme")

    try:
        decoded_host = _decode_url_part(host)
        decoded_path = _decode_url_part(path)
        target_url = _parse_proxy_target_url(schema, decoded_host, decoded_path)
    except (ValueError, UnicodeDecodeError, httpx.InvalidURL) as exc:
        raise HTTPException(status_code=400, detail="Invalid proxy target") from exc

    # Only allow hosts that were explicitly opted-in via configuration.
    # Without this check the endpoint is a generic SSRF gadget
    # (cloud metadata, internal services, localhost, ...).
    if _normalize_hostname(target_url.host) not in _get_proxy_allowed_hosts(request):
        raise HTTPException(status_code=403, detail="Proxy target not allowed")

    # The host allowlist check above compares only the hostname, so an attacker
    # could append an arbitrary port (e.g. "google-analytics.com:9200") to reach
    # internal services. Restrict the outbound port to the configured allowlist
    # (standard web ports by default); a target with no explicit port uses the
    # scheme default and is always permitted.
    if target_url.port is not None and target_url.port not in _get_proxy_allowed_ports(
        request
    ):
        raise HTTPException(status_code=403, detail="Proxy target port not allowed")

    try:
        client = _get_proxy_http_client(request)
        response = await client.send(
            client.build_request("GET", target_url),
            stream=True,
        )
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
