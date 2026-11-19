"""Cloudflare visitor location headers through the collector endpoints."""

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from evnt.api.deps import get_client_network
from evnt.config import settings
from evnt.tests.support import build_app, minimal_tp2_payload

LOCATION_HEADERS = {
    "CF-Connecting-IP": "203.0.113.5",
    "CF-IPCity": "Austin",
    "CF-Region": "Texas",
    "CF-Region-Code": "TX",
    "CF-IPContinent": "NA",
    "CF-IPLatitude": "30.27130",
    "CF-IPLongitude": "-97.74260",
    "CF-Postal-Code": "78701",
    "CF-Metro-Code": "635",
    "CF-Timezone": "America/Chicago",
}
EXPECTED_GEO = {
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


def _configure(monkeypatch, *, trust: bool = True, geo: bool = True) -> None:
    monkeypatch.setattr(settings.security, "trust_proxy_headers", trust)
    monkeypatch.setattr(
        settings.common,
        "snowplow",
        settings.common.snowplow.model_copy(
            update={"user_ip_header": "CF-Connecting-IP", "cloudflare_geo_headers": geo}
        ),
    )


def _collector(monkeypatch, connector, method: str, headers: dict[str, str]) -> dict:
    app = build_app(monkeypatch)
    app.state.connector = connector
    payload = minimal_tp2_payload()
    with TestClient(app) as client:
        response = (
            client.post("/tracker", json=payload, headers=headers)
            if method == "POST"
            else client.get("/i", params=payload["data"][0], headers=headers)
        )
    assert response.status_code in (200, 204)
    collector: dict = connector.inserted_batches[0][0]["extra"]["collector"]
    return collector


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_location_headers_are_stored_when_enabled(monkeypatch, connector, method):
    _configure(monkeypatch)
    collector = _collector(monkeypatch, connector, method, LOCATION_HEADERS)
    assert collector == {"client_ip": "203.0.113.5", "ip_country": "", "geo": EXPECTED_GEO}


def test_location_headers_are_ignored_by_default(monkeypatch, connector):
    monkeypatch.setattr(settings.security, "trust_proxy_headers", True)
    assert settings.common.snowplow.cloudflare_geo_headers is False
    collector = _collector(monkeypatch, connector, "POST", LOCATION_HEADERS)
    assert "geo" not in collector


def test_location_headers_need_proxy_trust(monkeypatch, connector):
    _configure(monkeypatch, trust=False)
    collector = _collector(monkeypatch, connector, "POST", LOCATION_HEADERS)
    assert "geo" not in collector


def test_malformed_values_are_left_out_without_failing_the_request(monkeypatch, connector):
    _configure(monkeypatch)
    headers = {
        **LOCATION_HEADERS,
        "CF-IPLatitude": "123.4",
        "CF-IPLongitude": "nan",
        "CF-IPContinent": "Europe",
        "CF-Timezone": "x" * 500,
        "CF-IPCity": "%C3%28",
    }
    collector = _collector(monkeypatch, connector, "POST", headers)
    assert collector["geo"] == {
        "region": "Texas",
        "region_code": "TX",
        "postal_code": "78701",
        "metro_code": "635",
    }


def test_only_malformed_values_store_no_geo(monkeypatch, connector):
    _configure(monkeypatch)
    collector = _collector(monkeypatch, connector, "POST", {"CF-IPLatitude": "north"})
    assert "geo" not in collector


@pytest.mark.anyio
async def test_raw_utf8_city_is_decoded(monkeypatch, anyio_backend):
    # TestClient re-encodes header values, so build the ASGI scope directly to
    # pass the raw UTF-8 bytes Cloudflare sends.
    _configure(monkeypatch)
    request = Request({"type": "http", "headers": [(b"cf-ipcity", "São Paulo".encode())]})
    network = await get_client_network(request)
    assert network.geo == {"city": "São Paulo"}
