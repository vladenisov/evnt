"""Validation, proxy trust and delivery guarantees at the HTTP boundary."""

from ipaddress import IPv4Address

import pytest
from fastapi.testclient import TestClient

from evnt.config import settings
from evnt.tests.support import build_app, minimal_tp2_payload


def _request(client, method, payload, *, headers=None):
    if method == "POST":
        return client.post("/tracker", json=payload, headers=headers)
    return client.get("/i", params=payload["data"][0], headers=headers)


@pytest.mark.parametrize("method", ["POST", "GET"])
@pytest.mark.parametrize("trust_proxy", [False, True])
def test_proxy_trust_controls_the_stored_ip(monkeypatch, connector, method, trust_proxy):
    monkeypatch.setattr(settings.security, "trust_proxy_headers", trust_proxy)
    monkeypatch.setattr(settings.common.snowplow, "user_ip_header", "X-Forwarded-For")
    app = build_app(monkeypatch)
    app.state.connector = connector
    with TestClient(app, client=("203.0.113.9", 50000)) as client:
        response = _request(
            client,
            method,
            minimal_tp2_payload(),
            headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1"},
        )
    assert response.status_code in (200, 204)
    expected = "198.51.100.7" if trust_proxy else "203.0.113.9"
    assert connector.inserted_batches[0][0]["user_ip"] == IPv4Address(expected)


@pytest.mark.parametrize("method", ["POST", "GET"])
@pytest.mark.parametrize(
    "forwarded,stored",
    [
        ("198.51.100.7", "198.51.100.7"),
        ("::ffff:198.51.100.7", "198.51.100.7"),
        ("2001:db8::7", "0.0.0.0"),
    ],
)
def test_custom_ip_header_skips_invalid_entries(monkeypatch, connector, method, forwarded, stored):
    monkeypatch.setattr(settings.security, "trust_proxy_headers", True)
    monkeypatch.setattr(settings.common.snowplow, "user_ip_header", "X-Client-IP")
    app = build_app(monkeypatch)
    app.state.connector = connector
    headers = [
        ("X-Client-IP", "unknown"),
        ("X-Client-IP", forwarded),
        ("X-Forwarded-For", "198.51.100.1"),
    ]
    with TestClient(app) as client:
        response = _request(client, method, minimal_tp2_payload(), headers=headers)
    assert response.status_code in (200, 204)
    # The existing ClickHouse column stores IPv4; native IPv6 uses the unknown-IP sentinel.
    assert connector.inserted_batches[0][0]["user_ip"] == IPv4Address(stored)


def test_invalid_element_rejects_the_entire_batch(monkeypatch, connector):
    app = build_app(monkeypatch)
    app.state.connector = connector
    payload = minimal_tp2_payload()
    payload["data"].append({"aid": "invalid"})
    with TestClient(app) as client:
        response = client.post("/tracker", json=payload)
    assert response.status_code == 422
    assert connector.inserted_batches == []


@pytest.mark.parametrize("body", [b"{", b"null", b"[]", b'"not an object"'])
def test_malformed_batch_never_reaches_storage(monkeypatch, connector, body):
    app = build_app(monkeypatch)
    app.state.connector = connector
    with TestClient(app) as client:
        response = client.post(
            "/tracker", content=body, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 422
    assert connector.inserted_batches == []


def test_pixel_validation_failure_does_not_insert_a_row(monkeypatch, connector):
    app = build_app(monkeypatch)
    app.state.connector = connector
    with TestClient(app) as client:
        response = client.get("/i", params={"aid": "invalid"})
    assert response.status_code == 422
    assert connector.inserted_batches == []


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_storage_failure_is_not_acknowledged_as_a_success(monkeypatch, method):
    class FailingConnector:
        async def insert_rows(self, rows, table_group="evnt"):
            raise RuntimeError("backend unavailable")

    app = build_app(monkeypatch)
    app.state.connector = FailingConnector()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = _request(client, method, minimal_tp2_payload())
    assert response.status_code == 500


def test_batch_retains_event_order_and_separate_contexts(monkeypatch, connector):
    app = build_app(monkeypatch)
    app.state.connector = connector
    payload = minimal_tp2_payload()
    payload["data"][0].update(
        {"eid": "11111111-1111-4111-8111-111111111111", "url": "https://example.com/first"}
    )
    second = dict(
        payload["data"][0],
        eid="22222222-2222-4222-8222-222222222222",
        url="https://example.com/second",
    )
    payload["data"].append(second)
    with TestClient(app) as client:
        assert client.post("/tracker", json=payload).status_code == 204
    assert len(connector.inserted_batches) == 1
    rows = connector.inserted_batches[0]
    assert [str(row["eid"]) for row in rows] == [event["eid"] for event in payload["data"]]
    assert [row["url"] for row in rows] == [
        "https://example.com/first",
        "https://example.com/second",
    ]
    assert rows[0]["extra"] is not rows[1]["extra"]
