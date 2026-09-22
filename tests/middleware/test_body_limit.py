"""Tests for the global request body size limit.

The limit exists so no single unauthenticated caller can make a worker buffer
and parse an unbounded payload, so both the declared-size and the streamed
path have to hold.
"""

import pytest
from core.config import settings
from core.dependencies import get_db_connector
from fastapi.testclient import TestClient
from middleware.body_limit import BodySizeLimitMiddleware
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from support import build_app

HTTP_OK = 200
HTTP_NO_CONTENT = 204
HTTP_TOO_LARGE = 413
LIMIT = 1024


@pytest.fixture
def echo_client() -> TestClient:
    """A minimal app whose handler reports how many bytes it actually read."""

    async def echo(request):
        body = await request.body()
        return PlainTextResponse(str(len(body)))

    app = Starlette(routes=[Route("/echo", echo, methods=["POST"])])
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=LIMIT)
    return TestClient(app)


class TestDeclaredSize:
    def test_body_under_the_limit_passes_through(self, echo_client):
        response = echo_client.post("/echo", content=b"x" * (LIMIT - 1))

        assert response.status_code == HTTP_OK
        assert response.text == str(LIMIT - 1)

    def test_body_exactly_at_the_limit_passes_through(self, echo_client):
        response = echo_client.post("/echo", content=b"x" * LIMIT)

        assert response.status_code == HTTP_OK
        assert response.text == str(LIMIT)

    def test_oversized_body_is_refused(self, echo_client):
        response = echo_client.post("/echo", content=b"x" * (LIMIT + 1))

        assert response.status_code == HTTP_TOO_LARGE
        assert response.json()["detail"] == "request body too large"

    def test_refusal_happens_before_the_handler_runs(self, echo_client):
        """The point is to avoid the work, not to report it afterwards."""
        response = echo_client.post("/echo", content=b"x" * (LIMIT * 100))

        assert response.status_code == HTTP_TOO_LARGE


class TestStreamedSize:
    def test_oversized_chunked_body_is_refused(self, echo_client):
        """A chunked upload declares no size, so only the running total stops it."""

        def chunks():
            for _ in range(LIMIT // 64 + 4):
                yield b"x" * 64

        response = echo_client.post("/echo", content=chunks())

        assert response.status_code == HTTP_TOO_LARGE

    def test_chunked_body_under_the_limit_passes_through(self, echo_client):
        def chunks():
            for _ in range(4):
                yield b"x" * 64

        response = echo_client.post("/echo", content=chunks())

        assert response.status_code == HTTP_OK
        assert response.text == "256"


class TestPassThrough:
    def test_requests_without_a_body_are_untouched(self, echo_client):
        response = echo_client.get("/echo")

        assert response.status_code != HTTP_TOO_LARGE


class TestWiredIntoTheApp:
    def test_tracker_refuses_an_oversized_payload(
        self,
        monkeypatch,
        app_root,
        connector,
    ):
        """The plaintext endpoint had no ceiling of its own before this."""
        monkeypatch.setattr(settings.security, "max_request_body_bytes", 2048)
        app = build_app(monkeypatch, app_root)
        app.dependency_overrides[get_db_connector] = lambda: connector

        with TestClient(app) as client:
            response = client.post(
                "/tracker",
                json={
                    "schema": (
                        "iglu:com.snowplowanalytics.snowplow/"
                        "payload_data/jsonschema/1-0-4"
                    ),
                    "data": [{"e": "pv", "aid": "x" * 4096, "p": "web", "tv": "js"}],
                },
            )

        assert response.status_code == HTTP_TOO_LARGE
        assert connector.inserted_batches == []

    def test_normal_payloads_still_get_through(self, monkeypatch, app_root, connector):
        app = build_app(monkeypatch, app_root)
        app.dependency_overrides[get_db_connector] = lambda: connector

        with TestClient(app) as client:
            response = client.post(
                "/tracker",
                json={
                    "schema": (
                        "iglu:com.snowplowanalytics.snowplow/"
                        "payload_data/jsonschema/1-0-4"
                    ),
                    "data": [
                        {
                            "e": "pv",
                            "aid": "example-app",
                            "p": "web",
                            "tv": "js-3.0.0",
                            "res": "1920x1080",
                        },
                    ],
                },
            )

        assert response.status_code == HTTP_NO_CONTENT
        assert connector.inserted_batches[0][0]["aid"] == "example-app"
