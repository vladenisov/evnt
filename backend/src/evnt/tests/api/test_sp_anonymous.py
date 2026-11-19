"""The ``SP-Anonymous`` header: ignored by default, honoured when enabled.

Covers the plain POST and GET endpoints and both forms of the encrypted one,
since each binds the request metadata through its own route.
"""

import base64
from ipaddress import IPv4Address
from uuid import UUID

import orjson
import pytest
from fastapi.testclient import TestClient

from evnt.config import EncryptionKeyConfig, settings
from evnt.crypto import Keyring, generate_keypair, seal_envelope
from evnt.tests.support import TP2_SCHEMA, build_app

DEVICE_ID = "88888888-8888-8888-8888-888888888888"
SP_ID_COOKIE = (
    f"_sp_id.1234={DEVICE_ID}.1700000000.1.1700000100.1700000050."
    "99999999-9999-9999-9999-999999999999"
)
# IPv4 on purpose: convert_ip stores any IPv6 address as 0.0.0.0 anyway, so an
# IPv6 connection address could not show a leak into user_ip.
CONNECTION_IP = "203.0.113.9"
PARAM_IP = "198.51.100.1"
HEADERS = {
    "SP-Anonymous": "*",
    "Cookie": SP_ID_COOKIE,
    "CF-Connecting-IP": CONNECTION_IP,
    "CF-IPCountry": "DE",
    "CF-IPCity": "Berlin",
    "CF-IPLatitude": "52.52",
}
# A server-side event, so the ``ip`` param would override ``user_ip``.
EVENT = {
    "e": "pv",
    "aid": "example-app",
    "p": "srv",
    "tv": "py-1.0.0",
    "ip": PARAM_IP,
    "tnuid": "n-1",
}


def _configure(monkeypatch, *, honor: bool) -> None:
    monkeypatch.setattr(settings.security, "trust_proxy_headers", True)
    monkeypatch.setattr(
        settings.common,
        "snowplow",
        settings.common.snowplow.model_copy(
            update={
                "user_ip_header": "CF-Connecting-IP",
                "user_country_header": "CF-IPCountry",
                "cloudflare_geo_headers": True,
                "honor_sp_anonymous": honor,
            }
        ),
    )


def _send(client: TestClient, method: str, headers: dict[str, str]):
    if method == "POST":
        return client.post(
            "/tracker", json={"schema": TP2_SCHEMA, "data": [EVENT]}, headers=headers
        )
    return client.get("/i", params=EVENT, headers=headers)


def _assert_anonymous(row: dict) -> None:
    assert row["user_ip"] == IPv4Address("0.0.0.0")
    assert row["duid"] is None
    assert row["extra"]["collector"] == {"anonymous": True}
    # The ``ip`` param is neither used nor kept; ``tnuid`` was sent on purpose.
    assert row["extra"]["tracker"] == {"network_userid": "n-1"}


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_header_is_ignored_by_default(monkeypatch, connector, method):
    monkeypatch.setattr(settings.security, "trust_proxy_headers", True)
    monkeypatch.setattr(
        settings.common,
        "snowplow",
        settings.common.snowplow.model_copy(
            update={"user_ip_header": "CF-Connecting-IP", "user_country_header": "CF-IPCountry"}
        ),
    )
    assert settings.common.snowplow.honor_sp_anonymous is False
    app = build_app(monkeypatch)
    app.state.connector = connector
    with TestClient(app) as client:
        response = _send(client, method, HEADERS)
    assert response.status_code in (200, 204)
    row = connector.inserted_batches[0][0]
    assert row["user_ip"] == IPv4Address(PARAM_IP)
    assert row["duid"] == UUID(DEVICE_ID)
    assert row["extra"]["collector"] == {"client_ip": CONNECTION_IP, "ip_country": "DE"}
    assert row["extra"]["tracker"]["ip"] == PARAM_IP


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_honoured_header_drops_ip_country_geo_and_cookie(monkeypatch, connector, method):
    _configure(monkeypatch, honor=True)
    app = build_app(monkeypatch)
    app.state.connector = connector
    with TestClient(app) as client:
        response = _send(client, method, HEADERS)
    assert response.status_code in (200, 204)
    _assert_anonymous(connector.inserted_batches[0][0])


@pytest.mark.parametrize("value", ["*", "", "1"])
def test_any_header_value_counts(monkeypatch, connector, value):
    _configure(monkeypatch, honor=True)
    app = build_app(monkeypatch)
    app.state.connector = connector
    with TestClient(app) as client:
        response = _send(client, "POST", {**HEADERS, "SP-Anonymous": value})
    assert response.status_code == 204
    _assert_anonymous(connector.inserted_batches[0][0])


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_requests_without_the_header_keep_network_metadata(monkeypatch, connector, method):
    _configure(monkeypatch, honor=True)
    app = build_app(monkeypatch)
    app.state.connector = connector
    headers = {key: value for key, value in HEADERS.items() if key != "SP-Anonymous"}
    with TestClient(app) as client:
        response = _send(client, method, headers)
    assert response.status_code in (200, 204)
    row = connector.inserted_batches[0][0]
    assert row["user_ip"] == IPv4Address(PARAM_IP)
    assert row["duid"] == UUID(DEVICE_ID)
    assert row["extra"]["collector"] == {
        "client_ip": CONNECTION_IP,
        "ip_country": "DE",
        "geo": {"city": "Berlin", "latitude": 52.52},
    }


@pytest.mark.parametrize("path", ["/tracker", "/i"])
def test_preflight_allows_the_header(monkeypatch, path):
    _configure(monkeypatch, honor=True)
    app = build_app(monkeypatch)
    with TestClient(app) as client:
        response = client.options(
            path,
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type, sp-anonymous",
            },
        )
    assert response.status_code == 200
    allowed = {
        h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")
    }
    assert "sp-anonymous" in allowed


@pytest.fixture
def encrypted_client(monkeypatch, connector):
    private_b64, public_b64 = generate_keypair()
    monkeypatch.setattr(settings.encryption, "enabled", True)
    monkeypatch.setattr(
        settings.encryption, "keys", [EncryptionKeyConfig(kid="k1", private_key=private_b64)]
    )
    _configure(monkeypatch, honor=True)
    app = build_app(monkeypatch)
    app.state.keyring = Keyring.from_config(settings.encryption)
    app.state.connector = connector
    envelope = seal_envelope(
        base64.b64decode(public_b64),
        "k1",
        orjson.dumps({"schema": TP2_SCHEMA, "data": [EVENT]}),
    )
    return TestClient(app), envelope


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_encrypted_endpoint_honours_the_header(encrypted_client, connector, method):
    client, envelope = encrypted_client
    with client:
        response = (
            client.post("/e", content=envelope, headers=HEADERS)
            if method == "POST"
            else client.get(
                "/e", params={"d": base64.urlsafe_b64encode(envelope).decode()}, headers=HEADERS
            )
        )
    assert response.status_code in (200, 204)
    _assert_anonymous(connector.inserted_batches[0][0])


def test_encrypted_preflight_allows_the_header(encrypted_client):
    client, _ = encrypted_client
    with client:
        response = client.options(
            "/e",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "sp-anonymous",
            },
        )
    assert response.status_code == 200
    assert "sp-anonymous" in response.headers["access-control-allow-headers"].lower()
