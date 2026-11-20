"""The ``ua``, ``ip`` and ``tnuid`` tracker parameters, through ``process_data``."""

import importlib
from ipaddress import IPv4Address

import pytest

from evnt.tracker.models import MAX_IP_PARAM_CHARS, MAX_TNUID_PARAM_CHARS, PayloadModel
from evnt.tracker.processing import process_data

params_module = importlib.import_module("evnt.tracker.params")
useragent_module = importlib.import_module("evnt.tracker.useragent")
processing_module = importlib.import_module("evnt.tracker.processing")

HEADER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
)
CONNECTION_IP = "198.51.100.7"


def _event(**fields) -> dict:
    return {"e": "pv", "aid": "example-app", "p": "web", "tv": "js-3.0.0", **fields}


async def _rows(*events, user_agent: str | None = HEADER_UA) -> list[dict]:
    return await process_data(PayloadModel(data=list(events)), user_agent, CONNECTION_IP, None)


async def _row(**fields) -> dict:
    (row,) = await _rows(_event(**fields))
    return row


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["web", "srv", "mob"])
async def test_ua_param_replaces_the_header_for_parsing_on_any_platform(platform):
    row = await _row(p=platform, ua=IPHONE_UA)

    assert row["user_agent"] == IPHONE_UA
    assert row["os_family"] == "iOS"
    assert row["extra"]["tracker"]["ua"] == IPHONE_UA


@pytest.mark.anyio
async def test_events_without_ua_param_keep_the_header_agent():
    with_override, without = await _rows(_event(ua=IPHONE_UA), _event())

    assert with_override["os_family"] == "iOS"
    assert without["user_agent"] == HEADER_UA
    assert without["os_family"] == "Windows"
    assert "tracker" not in without["extra"]


@pytest.mark.anyio
async def test_ua_override_is_resolved_once_per_distinct_value_per_request(monkeypatch):
    real = params_module.agent_fields_for_insert_async
    calls: list[str | None] = []

    async def counting(value):
        calls.append(value)
        return await real(value)

    monkeypatch.setattr(params_module, "agent_fields_for_insert_async", counting)
    rows = await _rows(*[_event(ua=IPHONE_UA) for _ in range(10)], _event(ua="curl/8.0"))

    assert calls == [IPHONE_UA, "curl/8.0"]
    # Each row owns its mutable agent fields even though the parse is shared.
    rows[0]["browser_version"].append("mutated")
    assert "mutated" not in rows[1]["browser_version"]


def _count_parses(monkeypatch) -> list[str | None]:
    real = params_module.agent_fields_for_insert_async
    calls: list[str | None] = []

    async def counting(value):
        calls.append(value)
        return await real(value)

    monkeypatch.setattr(params_module, "agent_fields_for_insert_async", counting)
    return calls


@pytest.mark.anyio
async def test_a_request_parses_a_bounded_number_of_distinct_ua_overrides(monkeypatch):
    useragent_module.clear_user_agent_cache()
    calls = _count_parses(monkeypatch)
    limit = params_module.MAX_UA_OVERRIDES_PER_REQUEST
    values = [f"curl/8.{i}" for i in range(limit + 1)]

    rows = await _rows(*[_event(ua=value) for value in values])

    assert calls == values[:limit]
    assert rows[limit - 1]["user_agent"] == values[limit - 1]
    assert "validation" not in rows[limit - 1]["extra"]
    over = rows[limit]
    assert over["user_agent"] == HEADER_UA
    assert over["extra"]["tracker"]["ua"] == values[limit]
    assert over["extra"]["validation"] == [
        {
            "stage": "payload",
            "field": "ua",
            "error": f"ua override ignored: more than {limit} distinct ua values in one request",
        }
    ]


@pytest.mark.anyio
async def test_past_the_bound_a_ua_override_already_cached_is_used(monkeypatch):
    useragent_module.clear_user_agent_cache()
    await _rows(_event(ua=IPHONE_UA))
    calls = _count_parses(monkeypatch)
    values = [f"curl/8.{i}" for i in range(params_module.MAX_UA_OVERRIDES_PER_REQUEST)]

    rows = await _rows(*[_event(ua=value) for value in values], _event(ua=IPHONE_UA))

    assert calls == values
    assert rows[-1]["os_family"] == "iOS"
    assert "validation" not in rows[-1]["extra"]


@pytest.mark.anyio
async def test_ip_param_sets_user_ip_of_server_side_events_only():
    srv = await _row(p="srv", ip="203.0.113.9")
    web = await _row(p="web", ip="203.0.113.9")

    assert srv["user_ip"] == IPv4Address("203.0.113.9")
    assert web["user_ip"] == IPv4Address(CONNECTION_IP)
    for row in (srv, web):
        # client_ip stays what the collector saw; the param is kept as sent.
        assert row["extra"]["collector"]["client_ip"] == CONNECTION_IP
        assert row["extra"]["tracker"]["ip"] == "203.0.113.9"


@pytest.mark.anyio
async def test_ipv6_ip_param_is_mapped_like_a_connection_address():
    mapped = await _row(p="srv", ip="::ffff:203.0.113.9")
    native = await _row(p="srv", ip="2001:db8::1")

    assert mapped["user_ip"] == IPv4Address("203.0.113.9")
    assert native["user_ip"] == IPv4Address("0.0.0.0")
    assert native["extra"]["tracker"]["ip"] == "2001:db8::1"


@pytest.mark.anyio
async def test_invalid_ip_param_keeps_the_connection_address_and_is_recorded():
    row = await _row(p="srv", ip="not-an-ip")

    assert row["user_ip"] == IPv4Address(CONNECTION_IP)
    assert row["extra"]["tracker"]["ip"] == "not-an-ip"
    (entry,) = row["extra"]["validation"]
    assert entry == {
        "stage": "payload",
        "field": "ip",
        "error": "not an IP address, got 'not-an-ip'",
    }


@pytest.mark.anyio
async def test_tnuid_is_kept_as_network_userid():
    row = await _row(tnuid="ecdff4d0-9175-40ac-a8bb-325c49733607")

    assert row["extra"]["tracker"] == {"network_userid": "ecdff4d0-9175-40ac-a8bb-325c49733607"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "limit"), [("ip", MAX_IP_PARAM_CHARS), ("tnuid", MAX_TNUID_PARAM_CHARS)]
)
async def test_oversized_params_are_dropped_and_recorded(field, limit):
    row = await _row(p="srv", **{field: "1" * (limit + 1)})

    assert row["user_ip"] == IPv4Address(CONNECTION_IP)
    assert "tracker" not in row["extra"]
    assert [entry["field"] for entry in row["extra"]["validation"]] == [field]


@pytest.mark.anyio
async def test_non_string_params_are_dropped_and_recorded():
    row = await _row(ua=42, ip=["203.0.113.9"])

    assert row["user_agent"] == HEADER_UA
    assert sorted(entry["field"] for entry in row["extra"]["validation"]) == ["ip", "ua"]


@pytest.mark.anyio
async def test_empty_params_count_as_not_sent():
    row = await _row(p="srv", ua="", ip="", tnuid="")

    assert row["user_agent"] == HEADER_UA
    assert "tracker" not in row["extra"]


@pytest.mark.anyio
async def test_fallback_row_of_a_crashing_event_keeps_the_overrides(monkeypatch):
    async def crash(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(processing_module, "parse_payload", crash)
    row = await _row(p="srv", ua=IPHONE_UA, ip="203.0.113.9", tnuid="n-1")

    assert row["user_ip"] == IPv4Address("203.0.113.9")
    assert row["os_family"] == "iOS"
    assert row["extra"]["tracker"] == {
        "ua": IPHONE_UA,
        "ip": "203.0.113.9",
        "network_userid": "n-1",
    }
    assert row["extra"]["validation"][-1]["stage"] == "processing"
    assert len(row["extra"]["event_fingerprint"]) == 32
