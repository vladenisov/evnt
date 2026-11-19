"""The compact user-agent cache must reproduce the original parser output exactly."""

from ipaddress import IPv4Address
from types import SimpleNamespace
from typing import Any

import pytest
from ua_parser import parse

from evnt.tracker import useragent as useragent_module
from evnt.tracker.models import PayloadElementModel, UserAgentModel
from evnt.tracker.payload import _build_initial_model, dump_insert_model

USER_AGENTS = [
    "",
    " ",
    "-",
    "Mozilla/5.0",
    "x" * 600,
    "%%%\x00é中",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.2478.80",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 YaBrowser/24.4.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
    "SamsungBrowser/24.0 Chrome/117.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "CriOS/124.0.6367.88 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 Instagram 320.0.2.28.108 (iPhone14,2; iOS 17_1; "
    "ru_RU; ru; scale=3.00; 1170x2532; 571012359)",
    "Dalvik/2.1.0 (Linux; U; Android 12; M2101K6G Build/SKQ1.210908.001)",
    "MyShopApp/3.14.1 (iPhone15,3; iOS 17.2; Scale/3.00)",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)",
    "python-requests/2.31.0",
    "curl/8.5.0",
]

_ELEMENT = PayloadElementModel.model_construct()
_IP = IPv4Address("192.0.2.1")


def _join_version(parts: list[str | None]) -> list[str]:
    return [p for p in parts if p is not None]


def _reference_parse(string: str) -> UserAgentModel:
    """The parser as it was before the compact cache, kept as the oracle."""

    data = UserAgentModel.model_construct(user_agent=string)
    if not string:
        return data
    ua = parse(string)
    if ua is None:
        return data
    data.device_is_bot = useragent_module.crawler_detect.isCrawler(string)
    browser = ua.user_agent
    if browser is not None:
        data.browser_family = browser.family or ""
        data.browser_version = _join_version(
            [browser.major, browser.minor, browser.patch, browser.patch_minor]
        )
        data.browser_version_string = ".".join(data.browser_version)
    os = ua.os
    if os is not None:
        data.os_family = os.family or ""
        data.os_version = _join_version([os.major, os.minor, os.patch, os.patch_minor])
        data.os_version_string = ".".join(data.os_version)
    device = ua.device
    if device is not None:
        data.device_brand = device.brand or ""
        data.device_model = device.model or ""
        if device.family:
            data.device_extra["family"] = device.family
    return data


def _typed_items(model: UserAgentModel) -> list[tuple[str, type, Any]]:
    return [(key, type(value), value) for key, value in model.__dict__.items()]


def _row(model: UserAgentModel | dict[str, Any]) -> str:
    return repr(dump_insert_model(_build_initial_model(_ELEMENT, model, _IP)))


@pytest.fixture
def fresh_cache():
    useragent_module.clear_user_agent_cache()
    yield
    useragent_module.clear_user_agent_cache()


@pytest.mark.parametrize("user_agent", USER_AGENTS)
@pytest.mark.usefixtures("fresh_cache")
def test_cached_paths_match_the_reference_parser(user_agent):
    expected = _reference_parse(user_agent)

    produced = [
        useragent_module._parse_agent_uncached(user_agent),
        useragent_module.parse_agent_for_insert(user_agent),  # miss
        useragent_module.parse_agent_for_insert(user_agent),  # hit
        useragent_module.parse_agent(user_agent),
    ]

    for model in produced:
        assert _typed_items(model) == _typed_items(expected)
        assert model.model_dump() == expected.model_dump()
        assert _row(model) == _row(expected)


@pytest.mark.anyio
@pytest.mark.usefixtures("fresh_cache")
async def test_async_path_matches_the_reference_parser():
    for user_agent in USER_AGENTS:
        miss = await useragent_module.agent_fields_for_insert_async(user_agent)
        hit = await useragent_module.agent_fields_for_insert_async(user_agent)
        reference = _reference_parse(user_agent)
        expected = _typed_items(reference)
        for fields in (miss, hit):
            assert [(k, type(v), v) for k, v in fields.items()] == expected
            # The ingest path hands this dict to the row builder directly.
            assert _row(fields) == _row(reference)


@pytest.mark.usefixtures("fresh_cache")
def test_none_header_matches_an_empty_reference():
    assert _typed_items(useragent_module.parse_agent(None)) == _typed_items(_reference_parse(""))


@pytest.mark.usefixtures("fresh_cache")
def test_fast_model_is_indistinguishable_from_model_construct():
    model = useragent_module.parse_agent_for_insert(USER_AGENTS[12])
    constructed = UserAgentModel.model_construct(**model.__dict__)

    assert model == constructed
    assert model.__pydantic_fields_set__ == constructed.__pydantic_fields_set__
    assert model.__pydantic_extra__ == constructed.__pydantic_extra__
    assert model.__pydantic_private__ == constructed.__pydantic_private__
    assert model.model_copy() == constructed
    assert UserAgentModel.model_validate(model.model_dump()) == constructed

    model.lang = "en"
    assert model.lang == "en"
    # Every model owns its fields-set, so assignment on one never leaks.
    other = useragent_module.parse_agent_for_insert(USER_AGENTS[12])
    assert other.__pydantic_fields_set__ is not model.__pydantic_fields_set__
    assert other.lang == ""


@pytest.mark.usefixtures("fresh_cache")
def test_equal_parse_results_share_one_record():
    first = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 Chrome/124.0.0.0 Mobile"
    second = first + " Safari/537.36"

    useragent_module.parse_agent_for_insert(first)
    useragent_module.parse_agent_for_insert(second)

    cache = useragent_module._user_agent_cache
    assert cache[first] is cache[second]


@pytest.mark.usefixtures("fresh_cache")
def test_intern_pool_stays_bounded(monkeypatch):
    monkeypatch.setattr(useragent_module, "_INTERN_POOL_LIMIT", 8)

    for user_agent in USER_AGENTS:
        model = useragent_module.parse_agent_for_insert(user_agent)
        assert len(useragent_module._intern_pool) <= 8
        assert _typed_items(model) == _typed_items(_reference_parse(user_agent))


@pytest.mark.usefixtures("fresh_cache")
def test_warm_up_runs_the_parsers_without_touching_the_cache(monkeypatch):
    parse_calls = []
    crawler_calls = []

    def _fake_parse(value):
        parse_calls.append(value)
        return parse(value)

    def _fake_is_crawler(value):
        crawler_calls.append(value)
        return False

    monkeypatch.setattr(useragent_module, "parse", _fake_parse)
    monkeypatch.setattr(
        useragent_module,
        "crawler_detect",
        SimpleNamespace(isCrawler=_fake_is_crawler),
    )

    useragent_module.warm_user_agent_parser()

    assert parse_calls == [useragent_module.WARM_UP_USER_AGENT]
    assert crawler_calls == [useragent_module.WARM_UP_USER_AGENT]
    assert len(useragent_module._user_agent_cache) == 0
