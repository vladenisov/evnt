"""HTTP-level integration tests for the encrypted `/e` endpoint.

Same shape as ``test_routes_http.py``: a real app with the production lifespan
patched out and a fake row sink, so what is under test is the HTTP contract --
here including that a sealed payload lands in the same rows a plaintext POST
would have produced, and that every rejection looks identical from outside.
"""

import base64

import orjson
import pytest
from core.config import EncryptionKeyConfig, settings
from core.constants import CONTENT_TYPE_GIF, CONTENT_TYPE_OCTET_STREAM, TRACKING_PIXEL
from core.crypto import Keyring, generate_keypair, seal_envelope
from core.dependencies import get_db_connector
from fastapi.testclient import TestClient
from support import build_app, minimal_tp2_payload

ENDPOINT = "/e"
HTTP_NO_CONTENT = 204
HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_TOO_LARGE = 413
HTTP_UNAVAILABLE = 503
REJECTION_DETAIL = "invalid encrypted payload"


@pytest.fixture
def public_key(monkeypatch) -> bytes:
    """Enable encryption on the settings singleton with one fresh key."""
    private_b64, public_b64 = generate_keypair()

    monkeypatch.setattr(settings.encryption, "enabled", True)
    monkeypatch.setattr(
        settings.encryption,
        "keys",
        [EncryptionKeyConfig(kid="k1", private_key=private_b64)],
    )
    return base64.b64decode(public_b64)


@pytest.fixture
def client(monkeypatch, app_root, public_key, connector) -> TestClient:
    """Build an app with encryption on, a live keyring, and a fake sink."""
    app = build_app(monkeypatch, app_root)
    app.state.keyring = Keyring.from_config(settings.encryption)
    app.dependency_overrides[get_db_connector] = lambda: connector
    return TestClient(app)


def _seal(public_key: bytes, payload: dict | None = None, **kwargs) -> bytes:
    body = orjson.dumps(payload if payload is not None else minimal_tp2_payload())
    return seal_envelope(public_key, "k1", body, **kwargs)


class TestPost:
    def test_sealed_payload_produces_the_same_rows_as_plaintext(
        self,
        client,
        connector,
        public_key,
    ):
        with client:
            response = client.post(
                ENDPOINT,
                content=_seal(public_key),
                headers={"content-type": CONTENT_TYPE_OCTET_STREAM},
            )

        assert response.status_code == HTTP_NO_CONTENT
        assert response.content == b""
        assert len(connector.inserted_batches) == 1
        rows = connector.inserted_batches[0]
        assert len(rows) == 1
        assert rows[0]["aid"] == "example-app"
        assert rows[0]["e"] == "pv"

    def test_compressed_payload_is_accepted(self, client, connector, public_key):
        with client:
            response = client.post(
                ENDPOINT,
                content=_seal(public_key, compress=True),
                headers={"content-type": CONTENT_TYPE_OCTET_STREAM},
            )

        assert response.status_code == HTTP_NO_CONTENT
        assert connector.inserted_batches[0][0]["aid"] == "example-app"

    def test_base64_body_is_accepted(self, client, connector, public_key):
        """Browser transports that cannot send raw bytes post base64 text."""
        with client:
            response = client.post(
                ENDPOINT,
                content=base64.b64encode(_seal(public_key)),
                headers={"content-type": "text/plain"},
            )

        assert response.status_code == HTTP_NO_CONTENT
        assert connector.inserted_batches[0][0]["aid"] == "example-app"

    def test_empty_batch_inserts_no_rows(self, client, connector, public_key):
        payload = {
            "schema": (
                "iglu:com.snowplowanalytics.snowplow/payload_data/jsonschema/1-0-4"
            ),
            "data": [],
        }
        with client:
            response = client.post(ENDPOINT, content=_seal(public_key, payload))

        assert response.status_code == HTTP_NO_CONTENT
        assert connector.inserted_batches == [[]]


class TestRejections:
    """Every failure must look the same from outside and insert nothing."""

    def test_plaintext_body_is_rejected(self, client, connector):
        with client:
            response = client.post(ENDPOINT, json=minimal_tp2_payload())

        assert response.status_code == HTTP_BAD_REQUEST
        assert response.json()["detail"] == REJECTION_DETAIL
        assert connector.inserted_batches == []

    def test_tampered_ciphertext_is_rejected(self, client, connector, public_key):
        sealed = bytearray(_seal(public_key))
        sealed[-1] ^= 0x01

        with client:
            response = client.post(ENDPOINT, content=bytes(sealed))

        assert response.status_code == HTTP_BAD_REQUEST
        assert response.json()["detail"] == REJECTION_DETAIL
        assert connector.inserted_batches == []

    def test_unknown_key_id_is_rejected(self, client, connector, public_key):
        with client:
            response = client.post(
                ENDPOINT,
                content=seal_envelope(public_key, "other", b"{}"),
            )

        assert response.status_code == HTTP_BAD_REQUEST
        assert response.json()["detail"] == REJECTION_DETAIL
        assert connector.inserted_batches == []

    def test_payload_sealed_to_another_collector_is_rejected(self, client, connector):
        _, foreign_public_b64 = generate_keypair()

        with client:
            response = client.post(
                ENDPOINT,
                content=_seal(base64.b64decode(foreign_public_b64)),
            )

        assert response.status_code == HTTP_BAD_REQUEST
        assert response.json()["detail"] == REJECTION_DETAIL
        assert connector.inserted_batches == []

    def test_non_json_plaintext_is_rejected(self, client, connector, public_key):
        with client:
            response = client.post(
                ENDPOINT,
                content=seal_envelope(public_key, "k1", b"not json at all"),
            )

        assert response.status_code == HTTP_BAD_REQUEST
        assert response.json()["detail"] == REJECTION_DETAIL
        assert connector.inserted_batches == []

    def test_schema_invalid_plaintext_is_rejected(self, client, connector, public_key):
        """A valid seal over a payload the tracker model refuses still 400s.

        The plaintext endpoint answers 422 here, but leaking the distinction
        would tell a prober that their key and tag were correct.
        """
        with client:
            response = client.post(
                ENDPOINT,
                content=_seal(public_key, {"data": [{"aid": "example-app"}]}),
            )

        assert response.status_code == HTTP_BAD_REQUEST
        assert response.json()["detail"] == REJECTION_DETAIL
        assert connector.inserted_batches == []

    def test_oversized_body_is_refused_from_content_length(
        self,
        client,
        connector,
        public_key,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.encryption, "max_envelope_bytes", 128)

        with client:
            response = client.post(ENDPOINT, content=_seal(public_key))

        assert response.status_code == HTTP_TOO_LARGE
        assert connector.inserted_batches == []

    def test_oversized_streamed_body_is_refused(
        self,
        client,
        connector,
        public_key,
        monkeypatch,
    ):
        """A chunked upload has no Content-Length, so the running total is
        the only thing that can stop it."""
        monkeypatch.setattr(settings.encryption, "max_envelope_bytes", 128)
        sealed = _seal(public_key)

        def chunks():
            for start in range(0, len(sealed), 32):
                yield sealed[start : start + 32]

        with client:
            response = client.post(ENDPOINT, content=chunks())

        assert response.status_code == HTTP_TOO_LARGE
        assert connector.inserted_batches == []


class TestGet:
    def test_pixel_fallback_returns_a_gif_and_forwards_rows(
        self,
        client,
        connector,
        public_key,
    ):
        envelope = base64.urlsafe_b64encode(_seal(public_key)).rstrip(b"=").decode()

        with client:
            response = client.get(ENDPOINT, params={"d": envelope})

        assert response.status_code == HTTP_OK
        assert response.headers["content-type"] == CONTENT_TYPE_GIF
        assert response.content == TRACKING_PIXEL
        assert connector.inserted_batches[0][0]["aid"] == "example-app"

    def test_missing_parameter_is_a_validation_error(self, client, connector):
        with client:
            response = client.get(ENDPOINT)

        assert response.status_code == 422
        assert connector.inserted_batches == []

    def test_oversized_query_is_refused(self, client, connector, monkeypatch):
        """The query ceiling is independent of, and far below, the POST one."""
        monkeypatch.setattr(settings.encryption, "max_query_bytes", 64)

        with client:
            response = client.get(ENDPOINT, params={"d": "A" * 128})

        assert response.status_code == HTTP_TOO_LARGE
        assert connector.inserted_batches == []

    def test_garbage_parameter_is_rejected(self, client, connector):
        with client:
            response = client.get(ENDPOINT, params={"d": "not-an-envelope"})

        assert response.status_code == HTTP_BAD_REQUEST
        assert connector.inserted_batches == []


class TestMounting:
    def test_endpoint_is_absent_when_encryption_is_disabled(
        self,
        monkeypatch,
        app_root,
    ):
        """A deployment that does not seal payloads exposes no extra surface."""
        monkeypatch.setattr(settings.encryption, "enabled", False)

        app = build_app(monkeypatch, app_root)

        assert ENDPOINT not in app.openapi()["paths"]

    def test_cors_preflight_is_answered(self, client):
        with client:
            response = client.options(
                ENDPOINT,
                headers={
                    "Origin": "https://example.com",
                    "Access-Control-Request-Method": "POST",
                },
            )

        assert response.status_code == HTTP_OK
        assert response.headers["access-control-allow-origin"] == "https://example.com"

    def test_requests_fail_cleanly_when_the_keyring_is_missing(
        self,
        monkeypatch,
        app_root,
        public_key,
        connector,
    ):
        """Startup resolves the keyring; without it the endpoint must not 500."""
        app = build_app(monkeypatch, app_root)
        app.state.keyring = None
        app.dependency_overrides[get_db_connector] = lambda: connector

        with TestClient(app) as unready_client:
            response = unready_client.post(ENDPOINT, content=_seal(public_key))

        assert response.status_code == HTTP_UNAVAILABLE
        assert connector.inserted_batches == []
