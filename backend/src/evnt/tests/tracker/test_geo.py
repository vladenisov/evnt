"""Cloudflare visitor location headers: validation and bounds of every value."""

import pytest
from prometheus_client import REGISTRY

from evnt.tracker.geo import MAX_GEO_TEXT_CHARS, parse_cloudflare_geo

FULL_SET = [
    (b"cf-ipcity", b"Austin"),
    (b"cf-region", b"Texas"),
    (b"cf-region-code", b"TX"),
    (b"cf-ipcontinent", b"NA"),
    (b"cf-iplatitude", b"30.27130"),
    (b"cf-iplongitude", b"-97.74260"),
    (b"cf-postal-code", b"78701"),
    (b"cf-metro-code", b"635"),
    (b"cf-timezone", b"America/Chicago"),
]


def _rejected(field: str) -> float:
    return (
        REGISTRY.get_sample_value("evnt_geo_header_values_rejected_total", {"field": field}) or 0.0
    )


def test_full_header_set_maps_to_normalised_keys():
    assert parse_cloudflare_geo(FULL_SET) == {
        "city": "Austin",
        "region": "Texas",
        "region_code": "TX",
        "continent": "NA",
        "latitude": 30.2713,
        "longitude": -97.7426,
        "postal_code": "78701",
        "metro_code": "635",
        "timezone": "America/Chicago",
    }


def test_no_location_headers_is_none():
    headers = [(b"host", b"example.com"), (b"cf-ipcountry", b"US"), (b"cf-ray", b"abc")]
    assert parse_cloudflare_geo(headers) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Cloudflare sends non-ASCII names as raw UTF-8 bytes.
        pytest.param("São Paulo".encode(), "São Paulo", id="raw-utf8"),
        pytest.param("Zürich".encode(), "Zürich", id="raw-utf8-umlaut"),
        pytest.param(b"S%C3%A3o%20Paulo", "São Paulo", id="percent-encoded"),
        pytest.param(b"  Austin  ", "Austin", id="surrounding-space"),
        pytest.param(b"100% City", "100% City", id="literal-percent-kept"),
    ],
)
def test_city_names_are_decoded(raw, expected):
    assert parse_cloudflare_geo([(b"cf-ipcity", raw)]) == {"city": expected}


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"S\xe3o Paulo", id="latin1-not-utf8"),
        pytest.param(b"%C3%28", id="percent-encoded-invalid-utf8"),
        pytest.param(b"Austin\x00", id="nul"),
        pytest.param(b"Aus%0Atin", id="percent-encoded-newline"),
        pytest.param(b"x" * (MAX_GEO_TEXT_CHARS + 1), id="too-long"),
        pytest.param(b"x" * 100_000, id="huge"),
    ],
)
def test_malformed_city_is_dropped_and_counted(raw):
    before = _rejected("city")
    assert parse_cloudflare_geo([(b"cf-ipcity", raw), (b"cf-region", b"Texas")]) == {
        "region": "Texas"
    }
    assert _rejected("city") == before + 1


@pytest.mark.parametrize("raw", [pytest.param(b"", id="empty"), pytest.param(b"   ", id="blank")])
def test_empty_values_are_absent_not_rejected(raw):
    # Cloudflare sends empty values for fields it has no data for; counting
    # them would make the rejection counter rise on normal traffic.
    before = _rejected("postal_code")
    assert parse_cloudflare_geo([(b"cf-postal-code", raw), (b"cf-region", b"Texas")]) == {
        "region": "Texas"
    }
    assert _rejected("postal_code") == before


@pytest.mark.parametrize(
    "header,raw",
    [
        (b"cf-iplatitude", b"90.1"),
        (b"cf-iplatitude", b"-91"),
        (b"cf-iplatitude", b"nan"),
        (b"cf-iplatitude", b"inf"),
        (b"cf-iplatitude", b"1e1"),
        (b"cf-iplatitude", b"1_0"),
        (b"cf-iplatitude", b"\xd9\xa1"),  # Arabic-Indic one, which float() accepts
        (b"cf-iplongitude", b"180.5"),
        (b"cf-iplongitude", b"0x10"),
        (b"cf-ipcontinent", b"Europe"),
        (b"cf-ipcontinent", b"\xc3\x9f"),
        (b"cf-region-code", b"T X"),
        (b"cf-region-code", b"TOOLONGCODE"),
        (b"cf-metro-code", b"12a"),
        (b"cf-postal-code", b"-1234"),
        (b"cf-postal-code", "78701é".encode()),
        (b"cf-timezone", b"America/../etc"),
        (b"cf-timezone", b"x" * 65),
    ],
)
def test_out_of_range_or_malformed_values_are_dropped(header, raw):
    assert parse_cloudflare_geo([(header, raw)]) is None


@pytest.mark.parametrize(
    "header,raw,key,expected",
    [
        (b"cf-iplatitude", b"-90", "latitude", -90.0),
        (b"cf-iplongitude", b"180.0", "longitude", 180.0),
        (b"cf-ipcontinent", b"eu", "continent", "EU"),
        (b"cf-ipcontinent", b"T1", "continent", "T1"),
        (b"cf-region-code", b"eng", "region_code", "ENG"),
        (b"cf-postal-code", b"SW1A 1AA", "postal_code", "SW1A 1AA"),
        (b"cf-timezone", b"Etc/GMT+5", "timezone", "Etc/GMT+5"),
    ],
)
def test_boundary_values_are_kept(header, raw, key, expected):
    assert parse_cloudflare_geo([(header, raw)]) == {key: expected}


def test_repeated_header_keeps_first_valid_value():
    headers = [(b"cf-ipcity", b"\xff"), (b"cf-ipcity", b"Austin"), (b"cf-ipcity", b"Dallas")]
    assert parse_cloudflare_geo(headers) == {"city": "Austin"}


def test_header_names_are_matched_case_insensitively():
    assert parse_cloudflare_geo([(b"CF-IPCity", b"Austin")]) == {"city": "Austin"}
