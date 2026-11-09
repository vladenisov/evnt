"""Performance configuration defaults and environment overrides."""

import pytest

from evnt.config import Settings


def test_performance_defaults():
    performance = Settings().performance

    assert performance.user_agent_cache_size == 32768
    assert performance.cpu_task_concurrency == 8


def test_user_agent_cache_size_can_be_overridden(monkeypatch):
    monkeypatch.setenv("EVNT_PERFORMANCE__USER_AGENT_CACHE_SIZE", "4096")

    assert Settings().performance.user_agent_cache_size == 4096


def test_user_agent_cache_can_be_disabled(monkeypatch):
    monkeypatch.setenv("EVNT_PERFORMANCE__USER_AGENT_CACHE_SIZE", "0")

    assert Settings().performance.user_agent_cache_size == 0


def test_user_ip_header_accepts_an_ordered_list(monkeypatch):
    assert Settings().common.snowplow.user_ip_headers == ("X-Forwarded-For",)
    monkeypatch.setenv(
        "EVNT_COMMON__SNOWPLOW__USER_IP_HEADER", " CF-Connecting-IP , X-Forwarded-For "
    )
    snowplow = Settings().common.snowplow
    assert snowplow.user_ip_header == "CF-Connecting-IP,X-Forwarded-For"
    assert snowplow.user_ip_headers == ("CF-Connecting-IP", "X-Forwarded-For")


@pytest.mark.parametrize("value", ["  ", "CF-Connecting-IP,", ",X-Forwarded-For", "A,,B"])
def test_user_ip_header_rejects_empty_entries(monkeypatch, value):
    monkeypatch.setenv("EVNT_COMMON__SNOWPLOW__USER_IP_HEADER", value)
    with pytest.raises(ValueError, match="user_ip_header"):
        Settings()


def test_country_header_is_opt_in_and_can_be_configured(monkeypatch):
    assert Settings().common.snowplow.user_country_header is None
    monkeypatch.setenv("EVNT_COMMON__SNOWPLOW__USER_COUNTRY_HEADER", " CF-IPCountry ")
    assert Settings().common.snowplow.user_country_header == "CF-IPCountry"


def test_country_header_rejects_empty_configuration(monkeypatch):
    monkeypatch.setenv("EVNT_COMMON__SNOWPLOW__USER_COUNTRY_HEADER", "  ")
    with pytest.raises(ValueError, match="user_country_header"):
        Settings()
