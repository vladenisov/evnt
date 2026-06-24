"""Behavioral test for the SendGrid webhook route handler.

Calls ``sendgrid_event`` directly with a fake connector (RowSink) so the
204-response contract and the row forwarding to the ``sendgrid`` table group
are verified without HTTP plumbing or a real ClickHouse/RabbitMQ backend.
"""

import pytest
from routers.tracker.models.sendgrid import SendgridElementBaseModel
from routers.tracker.routes.sendgrid import sendgrid_event


class _RecordingConnector:
    def __init__(self):
        self.calls = []

    async def insert_rows(self, rows, table_group="evnt"):
        self.calls.append((table_group, rows))


def _event(email="user@example.com"):
    return SendgridElementBaseModel.model_validate(
        {
            "email": email,
            "timestamp": 1700000000,
            "smtp-id": "<abc@sendgrid>",
            "event": "delivered",
            "category": ["welcome"],
            "sg_event_id": "evt-1",
            "sg_message_id": "msg-1",
        },
    )


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_sendgrid_event_forwards_rows_and_returns_204(anyio_backend):
    connector = _RecordingConnector()
    body = [_event("a@example.com"), _event("b@example.com")]

    response = await sendgrid_event(connector, body)

    assert response.status_code == 204
    assert len(connector.calls) == 1
    table_group, rows = connector.calls[0]
    assert table_group == "sendgrid"
    assert [row["email"] for row in rows] == ["a@example.com", "b@example.com"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_sendgrid_event_empty_body_still_inserts_empty_batch(anyio_backend):
    connector = _RecordingConnector()

    response = await sendgrid_event(connector, [])

    assert response.status_code == 204
    assert connector.calls == [("sendgrid", [])]
