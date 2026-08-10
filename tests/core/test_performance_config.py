"""Performance configuration defaults and environment overrides."""

from core.config import Settings


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
