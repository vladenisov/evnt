"""Visitor location from Cloudflare's "Add visitor location headers" transform.

Cloudflare adds ``cf-ipcity``, ``cf-region``, ``cf-iplatitude`` and the rest
to every proxied request when the managed transform is on
(https://developers.cloudflare.com/rules/transform/managed-transforms/reference/).
They are read into ``extra.collector.geo`` under normalised keys.

The values are only as trustworthy as the proxy in front of the collector, and
any client can send the same header names, so every value is validated and
bounded here: a value that fails is left out (and counted), never an error.

Cloudflare sends non-ASCII characters (city and region names such as
"São Paulo") as raw UTF-8 bytes. Header values are read here as bytes and
decoded as UTF-8 directly, which is what the latin-1 decoding of Starlette's
``request.headers`` would otherwise garble. The Cloudflare reference documents
no percent-encoding, but some origins have reported percent-encoded names, so
free-text values that contain a valid ``%XX`` UTF-8 sequence are decoded too.
"""

import re
from collections.abc import Callable, Iterable
from typing import Final
from urllib.parse import unquote

from evnt.observability.metrics import GEO_VALUES_REJECTED

type GeoValue = str | float
type Geo = dict[str, GeoValue]
type _Parser = Callable[[bytes], GeoValue | None]

# A city or region name; the longest real ones are well under this.
MAX_GEO_TEXT_CHARS: Final[int] = 128
# Bytes read from one header value at most: four UTF-8 bytes per character,
# times three for a fully percent-encoded name. A longer value is not decoded.
_MAX_RAW_BYTES: Final[int] = MAX_GEO_TEXT_CHARS * 12

# Continent codes (AF, AN, AS, EU, NA, OC, SA) and Cloudflare's T1 for Tor.
_CONTINENT = re.compile(r"[A-Z]{2}|T1")
# The subdivision part of an ISO 3166-2 code, e.g. "TX" or "ENG".
_REGION_CODE = re.compile(r"[A-Z0-9]{1,8}")
# A Nielsen DMA code, e.g. "635".
_METRO_CODE = re.compile(r"[0-9]{1,8}")
# Postal codes are digits, letters, spaces and dashes ("78701", "SW1A 1AA").
_POSTAL_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 -]{0,15}")
# An IANA zone name, e.g. "America/Argentina/Buenos_Aires" or "Etc/GMT+5".
_TIMEZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+/-]{0,63}")
# Decimal degrees; no exponent, "nan" or "inf", which float() would accept.
_DEGREES = re.compile(r"-?[0-9]{1,3}(?:\.[0-9]{1,15})?")


def _decode(raw: bytes) -> str | None:
    if len(raw) > _MAX_RAW_BYTES:
        return None
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None


def _text(raw: bytes) -> str | None:
    value = _decode(raw)
    if value and "%" in value:
        try:
            value = unquote(value, errors="strict").strip()
        except UnicodeDecodeError:
            return None
    if not value or len(value) > MAX_GEO_TEXT_CHARS or not value.isprintable():
        return None
    return value


def _code(pattern: re.Pattern[str], *, upper: bool = False) -> _Parser:
    def parse(raw: bytes) -> str | None:
        value = _decode(raw)
        if not value or not value.isascii():
            return None
        if upper:
            value = value.upper()
        return value if pattern.fullmatch(value) else None

    return parse


def _degrees(limit: float) -> _Parser:
    def parse(raw: bytes) -> float | None:
        value = _decode(raw)
        if not value or not _DEGREES.fullmatch(value):
            return None
        degrees = float(value)
        return degrees if -limit <= degrees <= limit else None

    return parse


# Header name (as ASGI delivers it, lowercase) -> stored key and its parser.
_FIELDS: Final[dict[bytes, tuple[str, _Parser]]] = {
    b"cf-ipcity": ("city", _text),
    b"cf-region": ("region", _text),
    b"cf-region-code": ("region_code", _code(_REGION_CODE, upper=True)),
    b"cf-ipcontinent": ("continent", _code(_CONTINENT, upper=True)),
    b"cf-iplatitude": ("latitude", _degrees(90.0)),
    b"cf-iplongitude": ("longitude", _degrees(180.0)),
    b"cf-postal-code": ("postal_code", _code(_POSTAL_CODE)),
    b"cf-metro-code": ("metro_code", _code(_METRO_CODE)),
    b"cf-timezone": ("timezone", _code(_TIMEZONE)),
}
_REJECTED: Final = {key: GEO_VALUES_REJECTED.labels(field=key) for key, _ in _FIELDS.values()}
_PREFIX: Final[bytes] = b"cf-"


def parse_cloudflare_geo(raw_headers: Iterable[tuple[bytes, bytes]]) -> Geo | None:
    """The visitor location carried by ``raw_headers``, or ``None`` if there is none.

    One pass over the raw ASGI headers: a dict lookup per ``cf-`` header is
    cheaper than nine ``request.headers`` lookups, each of which scans them
    all. A repeated header keeps its first valid value.
    """

    geo: Geo | None = None
    for raw_name, raw_value in raw_headers:
        if not raw_name.startswith(_PREFIX):
            # ASGI servers lowercase header names; tolerate one that does not.
            if raw_name[:3].lower() != _PREFIX:
                continue
            raw_name = raw_name.lower()
        field = _FIELDS.get(raw_name)
        if field is None:
            continue
        key, parse = field
        if geo is not None and key in geo:
            continue
        if not raw_value.strip():
            # Cloudflare leaves fields it does not know empty (no postal or
            # metro code outside some countries): absent, not malformed.
            continue
        value = parse(raw_value)
        if value is None:
            _REJECTED[key].inc()
            continue
        if geo is None:
            geo = {}
        geo[key] = value
    return geo
