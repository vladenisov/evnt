"""FastAPI dependencies shared by the collector routes.

Every dependency here is ``async def`` on purpose: FastAPI runs a plain ``def``
dependency in the thread pool, and three or four such hops per request cost
more than processing a small batch. They only read ``app.state``, settings and
headers; keep them free of blocking work, since they now run on the event loop.
"""

import re
from ipaddress import IPv4Address, IPv6Address
from typing import Annotated, Final, NamedTuple

from fastapi import Depends, HTTPException, Request
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from evnt.config import settings
from evnt.crypto import Keyring
from evnt.protocols import RowSink
from evnt.tracker.geo import Geo, parse_cloudflare_geo
from evnt.tracker.ip import extract_ip_from_header

# Two ASCII letters (ISO 3166-1 alpha-2, Cloudflare's XX) or Cloudflare's T1 (Tor).
_COUNTRY_CODE = re.compile(r"[A-Z]{2}|T1")

# Sent by the Snowplow trackers (value "*") when server anonymisation is on.
SP_ANONYMOUS_HEADER: Final[str] = "sp-anonymous"


class ClientNetwork(NamedTuple):
    """What the collector knows about the client's network, for one request."""

    ip: IPv4Address | IPv6Address | None
    country: str | None
    geo: Geo | None
    anonymous: bool


# An anonymous request carries nothing that identifies the client's network.
_ANONYMOUS_NETWORK: Final = ClientNetwork(ip=None, country=None, geo=None, anonymous=True)


async def get_db_connector(request: Request) -> RowSink:
    """Return the active ingest connector from application state."""
    connector: RowSink = request.app.state.connector
    return connector


async def get_keyring(request: Request) -> Keyring:
    """Return the keyring resolved during application startup."""
    keyring: Keyring | None = getattr(request.app.state, "keyring", None)
    if keyring is None:
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="encrypted ingest is not ready",
        )
    return keyring


def _client_ip(request: Request) -> IPv4Address | IPv6Address | None:
    if not settings.security.trust_proxy_headers:
        host = request.client.host if request.client else None
        return extract_ip_from_header(host)

    for header_name in settings.common.snowplow.user_ip_headers:
        for header_value in request.headers.getlist(header_name):
            parsed_ip = extract_ip_from_header(header_value)
            if parsed_ip is not None:
                return parsed_ip
    return None


def _client_country(request: Request) -> str | None:
    header_name = settings.common.snowplow.user_country_header
    if not settings.security.trust_proxy_headers or header_name is None:
        return None
    for value in request.headers.getlist(header_name):
        candidate = value.strip()
        # Check ASCII before upper(): headers decode as latin-1, and "ß".upper() == "SS".
        if not candidate.isascii():
            continue
        country = candidate.upper()
        if _COUNTRY_CODE.fullmatch(country):
            return country
    return None


def _client_geo(request: Request) -> Geo | None:
    if not (
        settings.security.trust_proxy_headers and settings.common.snowplow.cloudflare_geo_headers
    ):
        return None
    return parse_cloudflare_geo(request.scope["headers"])


def is_anonymous_request(request: Request) -> bool:
    """Whether ``request`` asks for server anonymisation and the collector honours it.

    Any value counts, not just the trackers' ``*``: a client that asks not to
    be identified is not identified, whatever it put in the header.
    """
    return settings.common.snowplow.honor_sp_anonymous and SP_ANONYMOUS_HEADER in request.headers


async def get_client_network(request: Request) -> ClientNetwork:
    """Return the client's IP, country and location, or nothing for an anonymous request.

    One dependency rather than one per value, so the anonymity check decides
    for all of them at once and FastAPI resolves a single dependency.
    """
    if is_anonymous_request(request):
        return _ANONYMOUS_NETWORK
    return ClientNetwork(
        ip=_client_ip(request),
        country=_client_country(request),
        geo=_client_geo(request),
        anonymous=False,
    )


DbConnector = Annotated[RowSink, Depends(get_db_connector)]
KeyringDep = Annotated[Keyring, Depends(get_keyring)]
ClientNetworkDep = Annotated[ClientNetwork, Depends(get_client_network)]
