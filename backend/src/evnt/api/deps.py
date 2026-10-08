"""FastAPI dependencies shared by the collector routes."""

from ipaddress import IPv4Address, IPv6Address
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from evnt.config import settings
from evnt.crypto import Keyring
from evnt.protocols import RowSink
from evnt.tracker.ip import extract_ip_from_header


def get_db_connector(request: Request) -> RowSink:
    """Return the active ingest connector from application state."""
    connector: RowSink = request.app.state.connector
    return connector


def get_keyring(request: Request) -> Keyring:
    """Return the keyring resolved during application startup."""
    keyring: Keyring | None = getattr(request.app.state, "keyring", None)
    if keyring is None:
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="encrypted ingest is not ready",
        )
    return keyring


def get_client_ip(request: Request) -> IPv4Address | IPv6Address | None:
    """Return the end user's IP address.

    With ``security.trust_proxy_headers`` on, the first parseable value of the
    configured forwarding header wins. With it off, the header is ignored and
    the direct peer address is used, so a client cannot spoof its own IP.
    """
    if not settings.security.trust_proxy_headers:
        host = request.client.host if request.client else None
        return extract_ip_from_header(host)

    header_name = settings.common.snowplow.user_ip_header
    for header_value in request.headers.getlist(header_name):
        parsed_ip = extract_ip_from_header(header_value)
        if parsed_ip is not None:
            return parsed_ip
    return None


DbConnector = Annotated[RowSink, Depends(get_db_connector)]
KeyringDep = Annotated[Keyring, Depends(get_keyring)]
ClientIP = Annotated[IPv4Address | IPv6Address | None, Depends(get_client_ip)]
