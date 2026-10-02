"""HTTP-level integration tests for the Snowplow tracker endpoints.

These exercise the real FastAPI routing/response-model contract for the
POST batch endpoint and the GET tracking-pixel endpoint without needing a
real ClickHouse/RabbitMQ backend. The app is built via ``create_app()`` with
the production lifespan patched out (the ``_no_op_lifespan`` pattern), and the
``DbConnector`` dependency is overridden with a fake row sink so the only thing
under test here is the HTTP/status/response-model contract -- payload parsing
itself is covered by the unit tests under ``tests/tracker/``.
"""

from fastapi.testclient import TestClient

from evnt.api.deps import get_db_connector
from evnt.constants import CONTENT_TYPE_GIF, TRACKING_PIXEL
from evnt.tests.support import RecordingConnector, build_app, minimal_tp2_payload

POST_ENDPOINT = "/tracker"
GET_ENDPOINT = "/i"
HTTP_NO_CONTENT = 204
HTTP_OK = 200


def _build_client(monkeypatch, connector):
    """Create a TestClient with a no-op lifespan and an overridden connector."""
    app = build_app(monkeypatch)
    app.dependency_overrides[get_db_connector] = lambda: connector
    return TestClient(app)


def test_liveness_returns_204_without_initialized_backend(monkeypatch):
    client = _build_client(monkeypatch, RecordingConnector())

    with client:
        response = client.get("/live")

    assert response.status_code == HTTP_NO_CONTENT
    assert response.content == b""


def test_tracker_post_returns_204_and_forwards_rows(monkeypatch):
    connector = RecordingConnector()
    client = _build_client(monkeypatch, connector)

    with client:
        response = client.post(POST_ENDPOINT, json=minimal_tp2_payload())

    assert response.status_code == HTTP_NO_CONTENT
    assert response.content == b""
    # The connector received exactly one batch with one row.
    assert len(connector.inserted_batches) == 1
    rows = connector.inserted_batches[0]
    assert len(rows) == 1
    assert rows[0]["aid"] == "example-app"
    assert rows[0]["e"] == "pv"


def test_tracker_get_returns_gif_pixel_and_forwards_rows(monkeypatch):
    connector = RecordingConnector()
    client = _build_client(monkeypatch, connector)

    with client:
        response = client.get(
            GET_ENDPOINT,
            params={
                "e": "pv",
                "aid": "example-app",
                "p": "web",
                "tv": "js-3.0.0",
                "res": "1920x1080",
            },
        )

    assert response.status_code == HTTP_OK
    assert response.headers["content-type"] == CONTENT_TYPE_GIF
    assert response.content == TRACKING_PIXEL
    assert len(connector.inserted_batches) == 1
    assert connector.inserted_batches[0][0]["aid"] == "example-app"


def test_tracker_get_fills_defaults_for_fields_trackers_do_not_send(monkeypatch):
    """Regression: binding the model with ``Depends()`` turned every
    ``default_factory`` field (eid/dtm/stm/rtm) into a required query param,
    so real pixels, which never carry ``rtm``, were rejected with 422."""
    connector = RecordingConnector()
    client = _build_client(monkeypatch, connector)

    with client:
        response = client.get(
            GET_ENDPOINT,
            params={"e": "pv", "aid": "example-app", "p": "web", "tv": "js-3.0.0", "res": "1x1"},
        )

    assert response.status_code == HTTP_OK
    row = connector.inserted_batches[0][0]
    assert row["eid"] is not None
    assert row["rtm"] is not None


def test_tracker_post_with_empty_batch_inserts_no_rows(monkeypatch):
    connector = RecordingConnector()
    client = _build_client(monkeypatch, connector)

    with client:
        response = client.post(
            POST_ENDPOINT,
            json={
                "schema": ("iglu:com.snowplowanalytics.snowplow/payload_data/jsonschema/1-0-4"),
                "data": [],
            },
        )

    assert response.status_code == HTTP_NO_CONTENT
    # The handler still calls the connector, but with an empty row list.
    assert connector.inserted_batches == [[]]


def test_tracker_post_rejects_invalid_payload(monkeypatch):
    connector = RecordingConnector()
    client = _build_client(monkeypatch, connector)

    with client:
        # Missing required fields (e.g. ``e``/``tv``) on the element.
        response = client.post(
            POST_ENDPOINT,
            json={"data": [{"aid": "example-app"}]},
        )

    assert response.status_code == 422
    # No rows should have been forwarded to the connector on a validation error.
    assert connector.inserted_batches == []
