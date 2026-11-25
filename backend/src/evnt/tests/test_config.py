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


def test_geo_and_anonymity_settings_are_off_by_default_and_overridable(monkeypatch):
    snowplow = Settings().common.snowplow
    assert snowplow.cloudflare_geo_headers is False
    assert snowplow.honor_sp_anonymous is False
    monkeypatch.setenv("EVNT_COMMON__SNOWPLOW__CLOUDFLARE_GEO_HEADERS", "true")
    monkeypatch.setenv("EVNT_COMMON__SNOWPLOW__HONOR_SP_ANONYMOUS", "true")
    snowplow = Settings().common.snowplow
    assert snowplow.cloudflare_geo_headers is True
    assert snowplow.honor_sp_anonymous is True


def test_hostname_counts_as_configured_only_when_set(monkeypatch):
    monkeypatch.delenv("EVNT_COMMON__HOSTNAME", raising=False)
    monkeypatch.setenv("EVNT_COMMON__DEMO", "true")
    common = Settings().common
    assert common.hostname_configured is False
    assert str(common.hostname) == "http://localhost:8000/"

    monkeypatch.setenv("EVNT_COMMON__HOSTNAME", "http://localhost:8000")
    common = Settings().common
    assert common.hostname_configured is True
    assert str(common.hostname) == "http://localhost:8000/"


def test_removed_clickhouse_tables_setting_does_not_break_startup(monkeypatch):
    # `clickhouse.tables` was never read; a leftover env var must stay harmless.
    monkeypatch.setenv("EVNT_CLICKHOUSE__TABLES", '{"evnt": {"local": {"name": "x"}}}')
    clickhouse = Settings().clickhouse
    assert not hasattr(clickhouse, "tables")
    assert clickhouse.configuration.tables["evnt"]["local"]["name"] != "x"
