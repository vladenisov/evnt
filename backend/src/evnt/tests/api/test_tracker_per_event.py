"""Response semantics of the collector endpoints when some events are bad.

Snowplow trackers drop a whole batch without retry on 400/401/403/410/422 and
resend anything else forever, so a bad event must cost only itself: the
request still gets the normal success response.
"""

import pytest
from fastapi.testclient import TestClient

from evnt.constants import TRACKING_PIXEL
from evnt.tests.support import build_app, minimal_tp2_payload


@pytest.fixture
def client(monkeypatch, connector) -> TestClient:
    app = build_app(monkeypatch)
    app.state.connector = connector
    return TestClient(app)


def test_post_batch_stores_invalid_but_buildable_events_with_their_errors(client, connector):
    payload = minimal_tp2_payload()
    good = payload["data"][0]
    payload["data"] += [
        dict(good, duid="custom-id", vid="", aid="buildable"),
        {"aid": "no-event-type"},
        dict(good, p="headset", aid="vision"),
    ]
    with client:
        response = client.post("/tracker", json=payload)

    assert response.status_code == 204
    rows = connector.inserted_batches[0]
    assert [row["aid"] for row in rows] == ["example-app", "buildable", "vision"]
    assert [entry["field"] for entry in rows[1]["extra"]["validation"]] == ["duid", "vid"]
    assert rows[2]["p"] == "mob"
    assert rows[2]["extra"]["tracker"] == {"platform": "headset"}


def test_post_batch_with_a_crashing_screen_view_is_stored(client, connector):
    payload = minimal_tp2_payload()
    payload["data"][0].update(
        e="ue",
        p="mob",
        ue_pr=(
            '{"schema":"iglu:com.snowplowanalytics.snowplow/unstruct_event/jsonschema/1-0-0",'
            '"data":{"schema":"iglu:com.snowplowanalytics.mobile/screen_view/jsonschema/1-0-0",'
            '"data":{"name":"Home"}}}'
        ),
    )
    with client:
        response = client.post("/tracker", json=payload)

    assert response.status_code == 204
    row = connector.inserted_batches[0][0]
    assert (row["e"], row["url"]) == ("pv", "Home")


def test_get_pixel_stores_an_invalid_but_buildable_event(client, connector):
    params = {"e": "pv", "p": "mob", "tv": "ios-6.0.0", "duid": "legacy", "ttm": "1700000000000"}
    with client:
        response = client.get("/i", params=params)

    assert response.status_code == 200
    assert response.content == TRACKING_PIXEL
    row = connector.inserted_batches[0][0]
    assert row["aid"] == ""
    assert row["res"] == ""
    assert row["duid"] is None
    assert row["extra"]["validation"][0]["field"] == "duid"
    assert row["extra"]["tracker"] == {"true_tstamp": "2023-11-14T22:13:20.000Z"}
    assert row["dtm"].year == 2023


SRV_PARAMS = {
    "e": "pv",
    "p": "srv",
    "tv": "py-1.0.0",
    "ua": "curl/8.0",
    "ip": "203.0.113.9",
    "tnuid": "ecdff4d0-9175-40ac-a8bb-325c49733607",
}


def test_get_pixel_and_post_read_the_same_tracker_params(client, connector):
    payload = minimal_tp2_payload()
    payload["data"][0].update(SRV_PARAMS)
    with client:
        client.get("/i", params=SRV_PARAMS)
        client.post("/tracker", json=payload)

    pixel_row, post_row = connector.inserted_batches[0][0], connector.inserted_batches[1][0]
    for row in (pixel_row, post_row):
        assert str(row["user_ip"]) == "203.0.113.9"
        assert row["user_agent"] == "curl/8.0"
        assert row["extra"]["tracker"] == {
            "ua": "curl/8.0",
            "ip": "203.0.113.9",
            "network_userid": "ecdff4d0-9175-40ac-a8bb-325c49733607",
        }
        assert len(row["extra"]["event_fingerprint"]) == 32


def test_unknown_context_entity_is_stored_from_a_pixel(client, connector):
    co = (
        '{"schema":"iglu:com.snowplowanalytics.snowplow/contexts/jsonschema/1-0-0",'
        '"data":[{"schema":"iglu:com.acme/thing/jsonschema/1-0-0","data":{"a":1}}]}'
    )
    with client:
        client.get("/i", params={"e": "pv", "p": "web", "tv": "js-3.0.0", "co": co})

    row = connector.inserted_batches[0][0]
    assert row["extra"]["contexts"] == {"com.acme/thing": [{"version": "1-0-0", "data": '{"a":1}'}]}
