"""
User agent parsing functionality.
"""

from copy import copy
from functools import lru_cache
from typing import Final

from core.tracing import capture_span
from crawlerdetect import CrawlerDetect
from routers.tracker.models.snowplow import UserAgentModel
from ua_parser import parse

USER_AGENT_CACHE_SIZE: Final[int] = 32768
_MUTABLE_USER_AGENT_FIELDS: Final[tuple[str, ...]] = (
    "browser_version",
    "browser_extra",
    "os_version",
    "device_extra",
)
crawler_detect = CrawlerDetect()


def _join_version(parts: list[str | None]) -> list[str]:
    return [p for p in parts if p is not None]


def clear_user_agent_cache() -> None:
    """Clear cached user-agent parse results."""

    _parse_agent_cached.cache_clear()


@lru_cache(maxsize=USER_AGENT_CACHE_SIZE)
def _parse_agent_cached(string: str) -> UserAgentModel:
    """Parse a non-null user-agent string into cacheable structured data."""
    data = UserAgentModel.model_construct(user_agent=string)

    if not string:
        return data

    ua = parse(string)

    if ua is None:
        return data

    data.device_is_bot = crawler_detect.isCrawler(string)

    browser = ua.user_agent
    if browser is not None:
        data.browser_family = browser.family or ""
        data.browser_version = _join_version(
            [
                browser.major,
                browser.minor,
                browser.patch,
                browser.patch_minor,
            ]
        )
        data.browser_version_string = ".".join(data.browser_version)

    os = ua.os
    if os is not None:
        data.os_family = os.family or ""
        data.os_version = _join_version(
            [
                os.major,
                os.minor,
                os.patch,
                os.patch_minor,
            ]
        )
        data.os_version_string = ".".join(data.os_version)

    device = ua.device
    if device is not None:
        data.device_brand = device.brand or ""
        data.device_model = device.model or ""
        if device.family:
            data.device_extra["family"] = device.family

    return data


def _copy_user_agent(data: UserAgentModel) -> UserAgentModel:
    """Return an isolated copy of cached user-agent data without validation."""

    copied = data.__dict__.copy()
    for field_name in _MUTABLE_USER_AGENT_FIELDS:
        copied[field_name] = copy(copied[field_name])
    return UserAgentModel.model_construct(**copied)


@capture_span()
def parse_agent(string: str | None) -> UserAgentModel:
    """Parse a user agent string into structured data."""

    return _copy_user_agent(_parse_agent_cached(string or ""))


def parse_agent_for_insert(string: str | None) -> UserAgentModel:
    """
    Parse a user-agent string for the ingest hot path.

    The returned object is the cached instance and must not be mutated directly.
    Row construction copies the mutable fields before inserting.
    """

    return _parse_agent_cached(string or "")
