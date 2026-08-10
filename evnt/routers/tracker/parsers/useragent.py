"""
User agent parsing functionality.
"""

from collections import OrderedDict
from copy import copy
from threading import Lock
from typing import Final

from core.concurrency import run_cpu_task
from core.config import settings
from core.tracing import capture_span
from crawlerdetect import CrawlerDetect
from routers.tracker.models.snowplow import UserAgentModel
from ua_parser import parse

USER_AGENT_CACHE_SIZE: Final[int] = settings.performance.user_agent_cache_size
_MUTABLE_USER_AGENT_FIELDS: Final[tuple[str, ...]] = (
    "browser_version",
    "browser_extra",
    "os_version",
    "device_extra",
)
crawler_detect = CrawlerDetect()
_user_agent_cache: OrderedDict[str, UserAgentModel] = OrderedDict()
_user_agent_cache_lock = Lock()


def _join_version(parts: list[str | None]) -> list[str]:
    return [p for p in parts if p is not None]


def clear_user_agent_cache() -> None:
    """Clear cached user-agent parse results."""

    with _user_agent_cache_lock:
        _user_agent_cache.clear()


def _get_cached_agent(string: str) -> UserAgentModel | None:
    """Return and refresh an LRU entry without parsing the user agent."""

    if USER_AGENT_CACHE_SIZE <= 0:
        return None

    with _user_agent_cache_lock:
        data = _user_agent_cache.pop(string, None)
        if data is not None:
            _user_agent_cache[string] = data
        return data


def _store_cached_agent(string: str, data: UserAgentModel) -> UserAgentModel:
    """Store one parsed agent and evict the least-recently-used entry."""

    # A disabled cache would otherwise pay for the lock and the insert/evict
    # pair on every event just to throw the entry away again.
    if USER_AGENT_CACHE_SIZE <= 0:
        return data

    with _user_agent_cache_lock:
        # Concurrent misses may parse the same value in parallel. Preserve the
        # first cached object so callers still share one immutable hot-path value.
        cached = _user_agent_cache.pop(string, None)
        if cached is not None:
            data = cached
        _user_agent_cache[string] = data
        if len(_user_agent_cache) > USER_AGENT_CACHE_SIZE:
            _user_agent_cache.popitem(last=False)
        return data


def _parse_agent_uncached(string: str) -> UserAgentModel:
    """Parse a non-null user-agent string without consulting the LRU cache."""

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
        data.browser_version = _join_version([
            browser.major,
            browser.minor,
            browser.patch,
            browser.patch_minor,
        ])
        data.browser_version_string = ".".join(data.browser_version)

    os = ua.os
    if os is not None:
        data.os_family = os.family or ""
        data.os_version = _join_version([
            os.major,
            os.minor,
            os.patch,
            os.patch_minor,
        ])
        data.os_version_string = ".".join(data.os_version)

    device = ua.device
    if device is not None:
        data.device_brand = device.brand or ""
        data.device_model = device.model or ""
        if device.family:
            data.device_extra["family"] = device.family

    return data


def _parse_agent_cached(string: str) -> UserAgentModel:
    """Return a cached user agent or parse and cache a missing value."""

    data = _get_cached_agent(string)
    if data is not None:
        return data
    return _store_cached_agent(string, _parse_agent_uncached(string))


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


async def parse_agent_for_insert_async(string: str | None) -> UserAgentModel:
    """Return cache hits inline and offload only real parser work to a thread."""

    normalized = string or ""
    data = _get_cached_agent(normalized)
    if data is not None:
        return data
    return await run_cpu_task(_parse_agent_cached, normalized)
