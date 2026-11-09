"""Original network metadata survives while the IPv4 column stays compatible."""

from ipaddress import IPv4Address

import pytest
from fastapi.testclient import TestClient

from evnt.config import settings
from evnt.tests.support import build_app, minimal_tp2_payload


def _configure(monkeypatch, *, trust=True, country_header="CF-IPCountry"):
    monkeypatch.setattr(settings.security, "trust_proxy_headers", trust)
    monkeypatch.setattr(
        settings.common,
        "snowplow",
        settings.common.snowplow.model_copy(
            update={
                "user_ip_header": "CF-Connecting-IP",
                "user_country_header": country_header,
            }
        ),
    )


@pytest.mark.parametrize("method", ["POST", "GET"])
@pytest.mark.parametrize(
    "source_ip,legacy_ip",
    [
        ("2001:db8::7", "0.0.0.0"),
        ("198.51.100.7", "198.51.100.7"),
    ],
)
def test_cloudflare_metadata_keeps_real_ip_and_country(
    monkeypatch,
    connector,
    method,
    source_ip,
    legacy_ip,
):
    _configure(monkeypatch)
    app = build_app(monkeypatch)
    app.state.connector = connector
    headers = {
        "CF-Connecting-IP": source_ip,
        "CF-IPCountry": "cn",
        "CF-Pseudo-IPv4": "240.0.0.1",
        "X-Forwarded-For": "203.0.113.99",
    }
    payload = minimal_tp2_payload()
    with TestClient(app) as client:
        response = (
            client.post("/tracker", json=payload, headers=headers)
            if method == "POST"
            else client.get("/i", params=payload["data"][0], headers=headers)
        )
    assert response.status_code in (200, 204)
    row = connector.inserted_batches[0][0]
    assert row["user_ip"] == IPv4Address(legacy_ip)
    assert row["extra"]["collector"] == {"client_ip": source_ip, "ip_country": "CN"}


@pytest.mark.parametrize(
    "country,expected",
    [("US", "US"), ("XX", "XX"), ("T1", "T1"), ("China", ""), ("", ""), ("12", "")],
)
def test_country_header_accepts_only_country_codes(monkeypatch, connector, country, expected):
    _configure(monkeypatch)
    app = build_app(monkeypatch)
    app.state.connector = connector
    with TestClient(app) as client:
        response = client.post(
            "/tracker",
            json=minimal_tp2_payload(),
            headers={
                "CF-Connecting-IP": "2001:db8::7",
                "CF-IPCountry": country,
            },
        )
    assert response.status_code == 204
    assert connector.inserted_batches[0][0]["extra"]["collector"]["ip_country"] == expected


@pytest.mark.parametrize("country_header", [None, "CF-IPCountry"])
def test_country_is_not_trusted_without_opt_in_or_proxy_trust(
    monkeypatch,
    connector,
    country_header,
):
    _configure(monkeypatch, trust=country_header is None, country_header=country_header)
    app = build_app(monkeypatch)
    app.state.connector = connector
    with TestClient(app, client=("203.0.113.9", 50000)) as client:
        response = client.post(
            "/tracker",
            json=minimal_tp2_payload(),
            headers={
                "CF-Connecting-IP": "2001:db8::7",
                "CF-IPCountry": "CN",
            },
        )
    assert response.status_code == 204
    metadata = connector.inserted_batches[0][0]["extra"]["collector"]
    assert metadata["ip_country"] == ""
    if country_header is not None:
        assert metadata["client_ip"] == "203.0.113.9"
