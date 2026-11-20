"""``extra.event_fingerprint``: equal for every send of one event, distinct otherwise."""

import pytest

from evnt.tracker.fingerprint import EXCLUDED_PARAMS, event_fingerprint
from evnt.tracker.models import PayloadElementModel, PayloadModel
from evnt.tracker.processing import process_data

EVENT = {
    "e": "pv",
    "aid": "example-app",
    "p": "web",
    "tv": "js-3.0.0",
    "url": "https://example.com/",
    "dtm": "1700000000000",
    "co": '{"schema":"iglu:x/y/jsonschema/1-0-0","data":[]}',
}


def test_fingerprint_is_a_32_char_hex_digest():
    value = event_fingerprint(EVENT)

    assert len(value) == 32
    int(value, 16)


def test_send_time_and_collector_params_do_not_change_it():
    first = dict(EVENT, eid="11111111-1111-4111-8111-111111111111", stm="1700000000100")
    retry = dict(
        EVENT,
        eid="22222222-2222-4222-8222-222222222222",
        stm="1700000005000",
        nuid="n",
        cv="c",
    )

    assert event_fingerprint(first) == event_fingerprint(retry) == event_fingerprint(EVENT)
    assert set(EXCLUDED_PARAMS) == {"eid", "stm", "nuid", "cv"}


def test_key_order_does_not_change_it():
    assert event_fingerprint(dict(reversed(list(EVENT.items())))) == event_fingerprint(EVENT)


@pytest.mark.parametrize(
    "change", [{"dtm": "1700000000001"}, {"url": "https://example.com/x"}, {"co": "{}"}]
)
def test_any_other_param_changes_it(change):
    assert event_fingerprint(dict(EVENT, **change)) != event_fingerprint(EVENT)


def test_the_raw_event_is_not_modified():
    raw = dict(EVENT, eid="x")
    event_fingerprint(raw)

    assert raw["eid"] == "x"


def test_values_orjson_cannot_encode_still_give_a_stable_value():
    raw = dict(EVENT, vid=2**70)

    assert event_fingerprint(raw) == event_fingerprint(dict(raw))
    assert event_fingerprint(raw) != event_fingerprint(dict(EVENT, vid=2**71))


def test_a_validated_element_is_hashed_from_its_set_fields():
    element = PayloadElementModel.model_validate(EVENT)

    assert event_fingerprint(element) == event_fingerprint(
        PayloadElementModel.model_validate(EVENT)
    )
    assert len(event_fingerprint(element)) == 32


@pytest.mark.anyio
async def test_a_resent_batch_gets_the_same_fingerprints():
    batch = [dict(EVENT, stm="1"), dict(EVENT, dtm="1700000000999", stm="1")]
    resent = [
        dict(event, stm="2", eid=f"{i}" * 8 + "-1111-4111-8111-111111111111")
        for i, event in enumerate(batch)
    ]

    first = await process_data(PayloadModel(data=batch), "", "1.2.3.4", None)
    second = await process_data(PayloadModel(data=resent), "", "1.2.3.4", None)

    fingerprints = [row["extra"]["event_fingerprint"] for row in first]
    assert fingerprints == [row["extra"]["event_fingerprint"] for row in second]
    assert fingerprints[0] != fingerprints[1]


@pytest.mark.anyio
async def test_a_pixel_event_is_fingerprinted_from_its_query_parameters():
    (row,) = await process_data(dict(EVENT), "", "1.2.3.4", None)

    assert row["extra"]["event_fingerprint"] == event_fingerprint(EVENT)
