"""An unpaired surrogate in tracker input is stored, never answered with a 500.

A truncated emoji reaches the collector as a lone ``\\ud83d`` escape, which
Python decodes into a string no sink can encode as UTF-8. These run the real
routes into the real sinks (the ClickHouse connector and the RabbitMQ
publisher) over fake transports, and check what the transport was handed.
"""

import base64
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from evnt.api.deps import get_db_connector
from evnt.config import EncryptionKeyConfig, RabbitMQConfig, settings
from evnt.constants import CONTENT_TYPE_GIF, CONTENT_TYPE_OCTET_STREAM
from evnt.crypto import Keyring, generate_keypair, seal_envelope
from evnt.ingest.rabbitmq import QueuedInsertPayload, RabbitMQPublisher
from evnt.storage.clickhouse.connector import ClickHouseConnector
from evnt.tests.support import build_app, minimal_tp2_payload

HTTP_OK = 200
HTTP_NO_CONTENT = 204
HTTP_BAD_REQUEST = 400
# A high surrogate whose low half was cut off, as JSON carries it.
LONE_ESCAPE = "\\ud83d"
LONE = "\ud83d"
REPLACED = "�"
SURROGATE_NOTE = "unpaired surrogate replaced with U+FFFD"
CONTEXTS_SCHEMA = "iglu:com.snowplowanalytics.snowplow/contexts/jsonschema/1-0-0"


class _ClickHouseClient:
    """Fake driver client: keeps the values of every insert."""

    def __init__(self) -> None:
        self.inserts: list[dict[str, Any]] = []

    async def insert(self, table_name, data, column_names, column_types, settings):
        self.inserts.append({"data": data, "column_names": column_names})


class _Exchange:
    def __init__(self) -> None:
        self.bodies: list[bytes] = []

    async def publish(self, message, routing_key, mandatory=True):
        self.bodies.append(message.body)


class _Channel:
    def __init__(self) -> None:
        self.default_exchange = _Exchange()


class _DirectSink:
    """The ClickHouse connector over a fake client."""

    # Rows come back under their column names: the title and URL columns.
    page = "title"
    url = "page"

    def __init__(self) -> None:
        self._client = _ClickHouseClient()
        self.sink = ClickHouseConnector(
            self._client,
            database="evnt",
            tables=settings.clickhouse.configuration.tables,
        )

    def stored_rows(self) -> list[dict[str, Any]]:
        return [
            dict(zip(insert["column_names"], values, strict=True))
            for insert in self._client.inserts
            for values in insert["data"]
        ]


class _RabbitMQSink:
    """The RabbitMQ publisher over a fake channel."""

    page = "page"
    url = "url"

    def __init__(self) -> None:
        self._channel = _Channel()
        self.sink = RabbitMQPublisher(
            connection=object(),
            channel=self._channel,
            config=RabbitMQConfig(queue_name="evnt.ingest"),
            tables=settings.clickhouse.configuration.tables,
            database="evnt",
        )

    def stored_rows(self) -> list[dict[str, Any]]:
        return [
            row
            for body in self._channel.default_exchange.bodies
            for row in QueuedInsertPayload.model_validate_json(body).rows
        ]


@pytest.fixture(params=[_DirectSink, _RabbitMQSink], ids=["direct", "rabbitmq"])
def backend(request):
    return request.param()


@pytest.fixture
def client(monkeypatch, backend) -> TestClient:
    app = build_app(monkeypatch)
    app.dependency_overrides[get_db_connector] = lambda: backend.sink
    return TestClient(app)


def _assert_utf8(value: Any) -> None:
    """Fail on any string, at any depth, that cannot be encoded as UTF-8."""

    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            _assert_utf8(key)
            _assert_utf8(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_utf8(item)


def _post_body(**params: str) -> bytes:
    """A tp2 batch as raw JSON, so ``LONE_ESCAPE`` stays an escape on the wire."""

    payload = minimal_tp2_payload()
    payload["data"][0].update(params)
    return json.dumps(payload).replace("\\\\ud83d", LONE_ESCAPE).encode("ascii")


def _only_row(backend) -> dict[str, Any]:
    rows = backend.stored_rows()
    assert len(rows) == 1
    _assert_utf8(rows[0])
    return rows[0]


def test_post_with_a_lone_surrogate_is_stored_with_a_replacement(client, backend):
    body = _post_body(page=f"x{LONE_ESCAPE}y", url=f"https://example.com/{LONE_ESCAPE}")
    assert LONE_ESCAPE.encode() in body

    with client:
        response = client.post(
            "/tracker", content=body, headers={"content-type": "application/json"}
        )

    assert response.status_code == HTTP_NO_CONTENT
    row = _only_row(backend)
    assert row[backend.page] == f"x{REPLACED}y"
    assert row[backend.url] == f"https://example.com/{REPLACED}"
    # One note per scrubbed column, named as the tracker model names it.
    notes = sorted(row["extra"]["validation"], key=lambda note: note["field"])
    assert notes == [
        {"stage": "payload", "field": field, "error": SURROGATE_NOTE} for field in ("page", "url")
    ]


def test_post_with_a_lone_surrogate_in_the_platform_is_stored(client, backend):
    # A rejected ``p`` is kept as sent in ``extra.tracker.platform``.
    body = _post_body(p=f"web{LONE_ESCAPE}")

    with client:
        response = client.post(
            "/tracker", content=body, headers={"content-type": "application/json"}
        )

    assert response.status_code == HTTP_NO_CONTENT
    row = _only_row(backend)
    assert row["extra"]["tracker"] == {"platform": f"web{REPLACED}"}


def test_post_keeps_a_complete_emoji(client, backend):
    body = _post_body(page="ok \\ud83d\\ude00").replace(b"\\\\ude00", b"\\ude00")

    with client:
        response = client.post(
            "/tracker", content=body, headers={"content-type": "application/json"}
        )

    assert response.status_code == HTTP_NO_CONTENT
    assert _only_row(backend)[backend.page] == "ok \U0001f600"


def test_post_with_a_poisoned_event_does_not_fail_its_neighbour(client, backend):
    payload = minimal_tp2_payload()
    payload["data"].append({**payload["data"][0], "page": f"x{LONE}y"})
    body = json.dumps(payload).encode("ascii")

    with client:
        response = client.post(
            "/tracker", content=body, headers={"content-type": "application/json"}
        )

    assert response.status_code == HTTP_NO_CONTENT
    rows = backend.stored_rows()
    _assert_utf8(rows)
    assert [row[backend.page] for row in rows] == ["", f"x{REPLACED}y"]


def test_get_pixel_never_delivers_a_lone_surrogate(client, backend):
    """Pins the GET parsing, which leaves the row scrubber nothing to replace.

    A percent-encoded surrogate is decoded to U+FFFD with the query string and
    ``se_pr`` holding the escape is rejected as JSON, so this passes without
    the scrubber. It fails if either starts to let a surrogate through.
    """

    contexts = json.dumps({"schema": CONTEXTS_SCHEMA, "data": []}).encode()
    params = {
        **minimal_tp2_payload()["data"][0],
        # ``se_pr`` carries JSON text, the one way a pixel can hold the escape.
        "se_pr": f'{{"label": "x{LONE_ESCAPE}y"}}',
        "cx": base64.urlsafe_b64encode(contexts).decode(),
    }
    query = "&".join(f"{name}={value}" for name, value in params.items())
    # The UTF-8-style bytes of the same surrogate, percent-encoded.
    query += "&page=x%ED%A0%BDy"

    with client:
        response = client.get(f"/i?{query}")

    assert response.status_code == HTTP_OK
    assert response.headers["content-type"] == CONTENT_TYPE_GIF
    row = _only_row(backend)
    page = row[backend.page]
    assert LONE not in page
    assert page.startswith("x")
    assert page.endswith("y")


@pytest.fixture
def sealing_key(monkeypatch) -> bytes:
    private_b64, public_b64 = generate_keypair()
    monkeypatch.setattr(settings.encryption, "enabled", True)
    monkeypatch.setattr(
        settings.encryption,
        "keys",
        [EncryptionKeyConfig(kid="k1", private_key=private_b64)],
    )
    return base64.b64decode(public_b64)


def test_sealed_batch_with_a_lone_surrogate_is_stored_with_a_replacement(
    monkeypatch, sealing_key, backend
):
    """A sealed batch is treated like the plaintext POST it wraps.

    ``orjson`` refuses the lone escape; ``/e`` then parses with the stdlib, so
    the event is stored instead of the whole batch being answered with a 400.
    """

    app = build_app(monkeypatch)
    app.state.keyring = Keyring.from_config(settings.encryption)
    app.dependency_overrides[get_db_connector] = lambda: backend.sink
    envelope = seal_envelope(sealing_key, "k1", _post_body(page=f"x{LONE_ESCAPE}y"))

    with TestClient(app) as client:
        response = client.post(
            "/e", content=envelope, headers={"content-type": CONTENT_TYPE_OCTET_STREAM}
        )

    assert response.status_code == HTTP_NO_CONTENT
    assert _only_row(backend)[backend.page] == f"x{REPLACED}y"


def test_sealed_plaintext_that_is_not_json_is_rejected(monkeypatch, sealing_key, backend):
    app = build_app(monkeypatch)
    app.state.keyring = Keyring.from_config(settings.encryption)
    app.dependency_overrides[get_db_connector] = lambda: backend.sink
    envelope = seal_envelope(sealing_key, "k1", b'{"data": [')

    with TestClient(app) as client:
        response = client.post(
            "/e", content=envelope, headers={"content-type": CONTENT_TYPE_OCTET_STREAM}
        )

    assert response.status_code == HTTP_BAD_REQUEST
    assert backend.stored_rows() == []


def test_sealed_batch_with_a_complete_emoji_is_stored(monkeypatch, sealing_key, backend):
    app = build_app(monkeypatch)
    app.state.keyring = Keyring.from_config(settings.encryption)
    app.dependency_overrides[get_db_connector] = lambda: backend.sink
    body = _post_body(page="ok \\ud83d\\ude00").replace(b"\\\\ude00", b"\\ude00")
    envelope = seal_envelope(sealing_key, "k1", body)

    with TestClient(app) as client:
        response = client.post(
            "/e", content=envelope, headers={"content-type": CONTENT_TYPE_OCTET_STREAM}
        )

    assert response.status_code < HTTP_BAD_REQUEST
    assert _only_row(backend)[backend.page] == "ok \U0001f600"
