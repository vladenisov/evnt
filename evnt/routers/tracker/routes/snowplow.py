"""
Route handlers for Snowplow events.

This module provides handlers for collecting tracking data using the
Snowplow tracking protocol via both GET and POST methods.
"""

from ipaddress import IPv4Address, IPv6Address

from core.config import settings
from core.constants import CONTENT_TYPE_GIF, TRACKING_PIXEL
from core.dependencies import DbConnector
from core.tracing import async_capture_span
from fastapi import Depends, Header, Request, Response
from routers.tracker.handlers import process_data
from routers.tracker.models.snowplow import (
    PayloadElementModel,
    PayloadModel,
)
from routers.tracker.parsers.ip import extract_ip_from_header
from starlette.status import HTTP_204_NO_CONTENT


async def get_user_ip_from_configured_header(
    request: Request,
) -> IPv4Address | IPv6Address | None:
    """
    Extract user IP from the configured header value.

    Returns:
        Parsed IP address or None
    """
    # Trust proxy headers contract: when proxy headers are not trusted, ignore
    # the configured forwarding header and rely on the direct peer address only.
    if not settings.security.trust_proxy_headers:
        host = request.client.host if request.client else None
        return extract_ip_from_header(host)

    header_name = settings.common.snowplow.user_ip_header
    for header_value in request.headers.getlist(header_name):
        parsed_ip = extract_ip_from_header(header_value)
        if parsed_ip is not None:
            return parsed_ip
    return None


@async_capture_span()
async def tracker_cors() -> None:
    """
    Handle CORS preflight requests.

    Returns:
        Empty response for OPTIONS requests
    """
    return


@async_capture_span()
async def tracker_post(
    connector: DbConnector,
    body: PayloadModel,
    user_agent: str | None = Header(None),
    user_ip: IPv4Address | IPv6Address | None = Depends(
        get_user_ip_from_configured_header,
    ),
    cookie: str | None = Header(None),
) -> Response:
    """
    Handle POST requests from Snowplow JS Tracker.

    Args:
        connector: Database connector (injected)
        body: Request body containing Snowplow payload
        user_agent: User agent header
        user_ip: IP address from configured proxy header
        cookie: Browser cookies

    Returns:
        Empty response with 204 status code
    """
    data = await process_data(body, user_agent, user_ip, cookie)
    await connector.insert_rows(data)

    return Response(status_code=HTTP_204_NO_CONTENT)


@async_capture_span()
async def tracker_get(
    connector: DbConnector,
    params: PayloadElementModel = Depends(),
    user_agent: str | None = Header(None),
    user_ip: IPv4Address | IPv6Address | None = Depends(
        get_user_ip_from_configured_header,
    ),
    cookie: str | None = Header(None),
) -> Response:
    """
    Handle GET requests from Snowplow JS Tracker.

    Args:
        connector: Database connector (injected)
        params: Query parameters containing Snowplow payload
        user_agent: User agent header
        user_ip: IP address from configured proxy header
        cookie: Browser cookies

    Returns:
        1x1 transparent GIF pixel response
    """
    data = await process_data(params, user_agent, user_ip, cookie)
    await connector.insert_rows(data)

    return Response(content=TRACKING_PIXEL, media_type=CONTENT_TYPE_GIF)
