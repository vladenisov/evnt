"""HTTP-level integration tests for the Snowplow tracker endpoints.

These exercise the real FastAPI routing/response-model contract for the
POST batch endpoint and the GET tracking-pixel endpoint without needing a
real ClickHouse/RabbitMQ backend. The app is built via ``create_app()`` with
the production lifespan patched out (the ``_no_op_lifespan`` pattern), and the
``DbConnector`` dependency is overridden with a fake row sink so the only thing
under test here is the HTTP/status/response-model contract -- payload parsing
itself is covered by the unit tests under ``tests/routers/tracker/parsers/``.
"""

import importlib
from contextlib import asynccontextmanager

from core.constants import CONTENT_TYPE_GIF, TRACKING_PIXEL
from core.dependencies import get_db_connector
from fastapi.testclient import TestClient

POST_ENDPOINT = "/tracker"
GET_ENDPOINT = "/i"
HTTP_NO_CONTENT = 204
HTTP_OK = 200


def _reload_main_module():
    return importlib.import_module("evnt.main")


def _no_op_lifespan(_app):
    @asynccontextmanager
    async def _lifespan(_application):
        yield

    return _lifespan(_app)


class _RecordingConnector:
    """Fake RowSink that records the rows handed to ``insert_rows``."""

    def __init__(self):
        self.inserted_batches: list[list[dict]] = []

    async def insert_rows(self, rows, table_group: str = "evnt") -> None:
        self.inserted_batches.append(rows)

    async def get_table_name(self, table_group: str = "evnt") -> str:
        return "local"


def _build_client(monkeypatch, app_root, connector):
    """Create a TestClient with a no-op lifespan and an overridden connector."""

    monkeypatch.chdir(app_root)
    main_module = _reload_main_module()
    monkeypatch.setattr(main_module, "lifespan", _no_op_lifespan)

    app = main_module.create_app()
    app.dependency_overrides[get_db_connector] = lambda: connector
    return TestClient(app)


def _minimal_tp2_payload() -> dict:
    """A minimal-but-valid Snowplow tp2 batch body (required fields only)."""

    return {
        "schema": ("iglu:com.snowplowanalytics.snowplow/payload_data/jsonschema/1-0-4"),
        "data": [
            {
                "e": "pv",
                "aid": "example-app",
                "p": "web",
                "tv": "js-3.0.0",
                "res": "1920x1080",
            },
        ],
    }


def test_liveness_returns_204_without_initialized_backend(monkeypatch, app_root):
    client = _build_client(monkeypatch, app_root, _RecordingConnector())

    with client:
        response = client.get("/live")

    assert response.status_code == HTTP_NO_CONTENT
    assert response.content == b""


def test_tracker_post_returns_204_and_forwards_rows(monkeypatch, app_root):
    connector = _RecordingConnector()
    client = _build_client(monkeypatch, app_root, connector)

    with client:
        response = client.post(POST_ENDPOINT, json=_minimal_tp2_payload())

    assert response.status_code == HTTP_NO_CONTENT
    assert response.content == b""
    # The connector received exactly one batch with one row.
    assert len(connector.inserted_batches) == 1
    rows = connector.inserted_batches[0]
    assert len(rows) == 1
    assert rows[0]["aid"] == "example-app"
    assert rows[0]["e"] == "pv"


def test_tracker_get_returns_gif_pixel_and_forwards_rows(monkeypatch, app_root):
    connector = _RecordingConnector()
    client = _build_client(monkeypatch, app_root, connector)

    with client:
        response = client.get(
            GET_ENDPOINT,
            params={
                "e": "pv",
                "aid": "example-app",
                "p": "web",
                "tv": "js-3.0.0",
                "res": "1920x1080",
                # The GET endpoint binds PayloadElementModel via Depends(), and
                # FastAPI surfaces fields with a default_factory (eid/dtm/stm/rtm)
                # as required query params, so they must be supplied explicitly.
                "eid": "00000000-0000-0000-0000-000000000000",
                "dtm": "2024-01-01T00:00:00Z",
                "stm": "2024-01-01T00:00:00Z",
                "rtm": "2024-01-01T00:00:00Z",
            },
        )

    assert response.status_code == HTTP_OK
    assert response.headers["content-type"] == CONTENT_TYPE_GIF
    assert response.content == TRACKING_PIXEL
    assert len(connector.inserted_batches) == 1
    assert connector.inserted_batches[0][0]["aid"] == "example-app"


def test_tracker_post_with_empty_batch_inserts_no_rows(monkeypatch, app_root):
    connector = _RecordingConnector()
    client = _build_client(monkeypatch, app_root, connector)

    with client:
        response = client.post(
            POST_ENDPOINT,
            json={
                "schema": (
                    "iglu:com.snowplowanalytics.snowplow/payload_data/jsonschema/1-0-4"
                ),
                "data": [],
            },
        )

    assert response.status_code == HTTP_NO_CONTENT
    # The handler still calls the connector, but with an empty row list.
    assert connector.inserted_batches == [[]]


def test_tracker_post_rejects_invalid_payload(monkeypatch, app_root):
    connector = _RecordingConnector()
    client = _build_client(monkeypatch, app_root, connector)

    with client:
        # Missing required fields (e.g. ``e``/``tv``) on the element.
        response = client.post(
            POST_ENDPOINT,
            json={"data": [{"aid": "example-app"}]},
        )

    assert response.status_code == 422
    # No rows should have been forwarded to the connector on a validation error.
    assert connector.inserted_batches == []
