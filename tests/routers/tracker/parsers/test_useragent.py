from types import SimpleNamespace

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
