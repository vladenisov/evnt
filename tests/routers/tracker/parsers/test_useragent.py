from types import SimpleNamespace

import pytest
from routers.tracker.parsers import useragent as useragent_module


def _parsed_user_agent():
    return SimpleNamespace(
        user_agent=SimpleNamespace(
            family="Chrome",
            major="123",
            minor="0",
            patch="1",
            patch_minor=None,
        ),
        os=SimpleNamespace(
            family="Linux",
            major="6",
            minor="0",
            patch=None,
            patch_minor=None,
        ),
        device=SimpleNamespace(
            brand="Generic",
            model="Desktop",
            family="Desktop",
        ),
    )


def test_parse_agent_caches_repeated_user_agents(monkeypatch):
    user_agent = "Mozilla/5.0"
    parse_calls = []
    crawler_calls = []

    def _fake_parse(value):
        parse_calls.append(value)
        return _parsed_user_agent()

    def _fake_is_crawler(value):
        crawler_calls.append(value)
        return False

    monkeypatch.setattr(useragent_module, "parse", _fake_parse)
    monkeypatch.setattr(
        useragent_module,
        "crawler_detect",
        SimpleNamespace(isCrawler=_fake_is_crawler),
    )

    useragent_module.clear_user_agent_cache()
    first = useragent_module.parse_agent(user_agent)
    first.device_extra["family"] = "mutated"
    second = useragent_module.parse_agent(user_agent)
    useragent_module.clear_user_agent_cache()

    assert parse_calls == [user_agent]
    assert crawler_calls == [user_agent]
    assert first is not second
    assert second.device_extra == {"family": "Desktop"}
    assert second.browser_version_string == "123.0.1"
    assert second.os_version_string == "6.0"


def test_parse_agent_for_insert_reuses_cached_instance(monkeypatch):
    user_agent = "Mozilla/5.0"
    parse_calls = []

    def _fake_parse(value):
        parse_calls.append(value)
        return _parsed_user_agent()

    monkeypatch.setattr(useragent_module, "parse", _fake_parse)
    monkeypatch.setattr(
        useragent_module,
        "crawler_detect",
        SimpleNamespace(isCrawler=lambda _: False),
    )

    useragent_module.clear_user_agent_cache()
    first = useragent_module.parse_agent_for_insert(user_agent)
    second = useragent_module.parse_agent_for_insert(user_agent)
    useragent_module.clear_user_agent_cache()

    assert parse_calls == [user_agent]
    assert first is second
    assert second.device_extra == {"family": "Desktop"}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_parse_agent_for_insert_async_offloads_only_cache_miss(
    monkeypatch,
    anyio_backend,
):
    user_agent = "Mozilla/5.0"
    thread_calls = []

    async def _fake_to_thread(function, *args):
        thread_calls.append((function, args))
        return function(*args)

    monkeypatch.setattr(useragent_module.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(useragent_module, "parse", lambda _: _parsed_user_agent())
    monkeypatch.setattr(
        useragent_module,
        "crawler_detect",
        SimpleNamespace(isCrawler=lambda _: False),
    )

    useragent_module.clear_user_agent_cache()
    first = await useragent_module.parse_agent_for_insert_async(user_agent)
    second = await useragent_module.parse_agent_for_insert_async(user_agent)
    useragent_module.clear_user_agent_cache()

    assert first is second
    assert thread_calls == [
        (useragent_module._parse_agent_cached, (user_agent,)),
    ]


def test_user_agent_cache_evicts_least_recently_used_entry(monkeypatch):
    parse_calls = []

    def _fake_parse(value):
        parse_calls.append(value)
        return _parsed_user_agent()

    monkeypatch.setattr(useragent_module, "USER_AGENT_CACHE_SIZE", 2)
    monkeypatch.setattr(useragent_module, "parse", _fake_parse)
    monkeypatch.setattr(
        useragent_module,
        "crawler_detect",
        SimpleNamespace(isCrawler=lambda _: False),
    )

    useragent_module.clear_user_agent_cache()
    useragent_module.parse_agent_for_insert("ua-1")
    useragent_module.parse_agent_for_insert("ua-2")
    useragent_module.parse_agent_for_insert("ua-1")
    useragent_module.parse_agent_for_insert("ua-3")
    useragent_module.parse_agent_for_insert("ua-2")
    useragent_module.clear_user_agent_cache()

    assert parse_calls == ["ua-1", "ua-2", "ua-3", "ua-2"]


def test_parse_agent_handles_missing_header_without_parser(monkeypatch):
    def _fail_parse(value):
        raise AssertionError(f"parser should not be called for {value!r}")

    monkeypatch.setattr(useragent_module, "parse", _fail_parse)

    useragent_module.clear_user_agent_cache()
    result = useragent_module.parse_agent(None)
    useragent_module.clear_user_agent_cache()

    assert result.user_agent == ""
    assert result.browser_family == ""
    assert result.device_extra == {}
