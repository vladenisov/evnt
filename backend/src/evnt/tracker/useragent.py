"""
User agent parsing functionality.

Parsed agents are cached per process in an LRU keyed on the raw header. Each
entry holds a compact immutable :class:`_AgentRecord` rather than a
``UserAgentModel``: a full pydantic instance cost about 2.3 KB per entry,
roughly 75 MB per worker process with the default 32768-entry cache. A fresh
``UserAgentModel`` is assembled from the record on every lookup, so callers
own what they receive and may mutate it.

The ingest path never needs the model: ``rows.build_initial_model`` only
copies the agent's fields into the row, so ``process_data`` asks for them as a
plain dict (``agent_fields_for_insert_async``) and skips the model object.
"""

from collections import OrderedDict
from threading import Lock
from typing import Any, Final, NamedTuple

from crawlerdetect import CrawlerDetect
from ua_parser import parse

from evnt.concurrency import run_cpu_task
from evnt.config import settings
from evnt.observability.tracing import capture_span
from evnt.tracker.models import UserAgentModel

USER_AGENT_CACHE_SIZE: Final[int] = settings.performance.user_agent_cache_size

# Families, brands, versions and whole records repeat across many distinct
# user-agent strings, so equal values are shared through this pool. It is
# cleared once it reaches the limit: header values are client-controlled and
# must not grow memory without bound, and clearing never changes a result
# because pooled values are immutable and only ever compared by equality.
_INTERN_POOL_LIMIT: Final[int] = 16384

# A typical modern browser string, used to pay the parsers' one-time
# initialisation (regex compilation) at startup instead of on a request.
WARM_UP_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)

crawler_detect = CrawlerDetect()


class _AgentRecord(NamedTuple):
    """The parsed fields of one user agent, without the string itself."""

    browser_family: str
    browser_version: tuple[str, ...]
    browser_version_string: str
    os_family: str
    os_version: tuple[str, ...]
    os_version_string: str
    device_brand: str
    device_model: str
    # Empty when the parser reported no device family; ``device_extra`` is
    # then an empty dict, matching the parser's output exactly.
    device_family: str
    device_is_bot: bool


_EMPTY_RECORD: Final[_AgentRecord] = _AgentRecord("", (), "", "", (), "", "", "", "", False)

_user_agent_cache: OrderedDict[str, _AgentRecord] = OrderedDict()
_user_agent_cache_lock = Lock()
_intern_pool: dict[Any, Any] = {}

_new_object = object.__new__
_set_attribute = object.__setattr__
_USER_AGENT_FIELDS: Final[frozenset[str]] = frozenset(UserAgentModel.model_fields)


def _intern[T](value: T) -> T:
    """Return the pooled object equal to ``value``, pooling it if new."""

    pooled = _intern_pool.get(value)
    if pooled is not None:
        return pooled  # type: ignore[no-any-return]
    if len(_intern_pool) >= _INTERN_POOL_LIMIT:
        _intern_pool.clear()
    # setdefault keeps the first value when two parser threads race.
    return _intern_pool.setdefault(value, value)  # type: ignore[no-any-return]


def _version(parts: tuple[str | None, ...]) -> tuple[str, ...]:
    return _intern(tuple(_intern(part) for part in parts if part is not None))


def clear_user_agent_cache() -> None:
    """Clear cached user-agent parse results."""

    with _user_agent_cache_lock:
        _user_agent_cache.clear()
        _intern_pool.clear()


def _get_cached_agent(string: str) -> _AgentRecord | None:
    """Return and refresh an LRU entry without parsing the user agent."""

    if USER_AGENT_CACHE_SIZE <= 0:
        return None

    with _user_agent_cache_lock:
        data = _user_agent_cache.pop(string, None)
        if data is not None:
            _user_agent_cache[string] = data
        return data


def _store_cached_agent(string: str, data: _AgentRecord) -> _AgentRecord:
    """Store one parsed agent and evict the least-recently-used entry."""

    # A disabled cache would otherwise pay for the lock and the insert/evict
    # pair on every event just to throw the entry away again.
    if USER_AGENT_CACHE_SIZE <= 0:
        return data

    with _user_agent_cache_lock:
        # Concurrent misses may parse the same value in parallel; keep the
        # entry that won so every reader sees one record per string.
        cached = _user_agent_cache.pop(string, None)
        if cached is not None:
            data = cached
        _user_agent_cache[string] = data
        if len(_user_agent_cache) > USER_AGENT_CACHE_SIZE:
            _user_agent_cache.popitem(last=False)
        return data


def _parse_record(string: str) -> _AgentRecord:
    """Parse a user-agent string into a record without consulting the cache."""

    if not string:
        return _EMPTY_RECORD

    ua = parse(string)

    if ua is None:
        return _EMPTY_RECORD

    is_bot = crawler_detect.isCrawler(string)

    browser_family = ""
    browser_version: tuple[str, ...] = ()
    browser = ua.user_agent
    if browser is not None:
        browser_family = browser.family or ""
        browser_version = _version(
            (browser.major, browser.minor, browser.patch, browser.patch_minor)
        )

    os_family = ""
    os_version: tuple[str, ...] = ()
    os = ua.os
    if os is not None:
        os_family = os.family or ""
        os_version = _version((os.major, os.minor, os.patch, os.patch_minor))

    device_brand = ""
    device_model = ""
    device_family = ""
    device = ua.device
    if device is not None:
        device_brand = device.brand or ""
        device_model = device.model or ""
        device_family = device.family or ""

    return _intern(
        _AgentRecord(
            browser_family=_intern(browser_family),
            browser_version=browser_version,
            browser_version_string=_intern(".".join(browser_version)),
            os_family=_intern(os_family),
            os_version=os_version,
            os_version_string=_intern(".".join(os_version)),
            device_brand=_intern(device_brand),
            device_model=_intern(device_model),
            device_family=_intern(device_family),
            device_is_bot=is_bot,
        )
    )


def _to_fields(string: str, record: _AgentRecord) -> dict[str, Any]:
    """Build the ``UserAgentModel`` field values for a cached record.

    The dict is new on every call, lists and dicts included, so the caller
    owns it. Keys follow the model's field order so rows built from them keep
    the same layout.
    """

    device_family = record.device_family
    return {
        "user_agent": string,
        "browser_family": record.browser_family,
        "browser_version": list(record.browser_version),
        "browser_version_string": record.browser_version_string,
        "browser_extra": {},
        "os_family": record.os_family,
        "os_version": list(record.os_version),
        "os_version_string": record.os_version_string,
        "lang": "",
        "device_brand": record.device_brand,
        "device_model": record.device_model,
        "device_extra": {"family": device_family} if device_family else {},
        "device_is_mobile": False,
        "device_is_tablet": False,
        "device_is_touch_capable": False,
        "device_is_pc": False,
        "device_is_bot": record.device_is_bot,
    }


def _to_model(string: str, record: _AgentRecord) -> UserAgentModel:
    """Build a fresh ``UserAgentModel`` from a cached record.

    This is what ``UserAgentModel.model_construct`` does once it has every
    field, minus its per-field default and alias handling, which makes it
    about three times cheaper.
    """

    model = _new_object(UserAgentModel)
    _set_attribute(model, "__dict__", _to_fields(string, record))
    _set_attribute(model, "__pydantic_fields_set__", set(_USER_AGENT_FIELDS))
    _set_attribute(model, "__pydantic_extra__", None)
    _set_attribute(model, "__pydantic_private__", None)
    return model


def _parse_agent_uncached(string: str) -> UserAgentModel:
    """Parse a non-null user-agent string without consulting the LRU cache."""

    return _to_model(string, _parse_record(string))


def _parse_record_cached(string: str) -> _AgentRecord:
    """Return a cached record or parse and cache a missing value."""

    data = _get_cached_agent(string)
    if data is not None:
        return data
    return _store_cached_agent(string, _parse_record(string))


def _parse_agent_cached(string: str) -> UserAgentModel:
    """Return a fresh model for ``string``, parsing it only on a cache miss."""

    return _to_model(string, _parse_record_cached(string))


@capture_span()
def parse_agent(string: str | None) -> UserAgentModel:
    """Parse a user agent string into structured data."""

    return _parse_agent_cached(string or "")


def parse_agent_for_insert(string: str | None) -> UserAgentModel:
    """
    Parse a user-agent string for the ingest hot path.

    Unlike ``parse_agent`` this records no tracing span. The returned model is
    built for this call alone and may be mutated.
    """

    return _parse_agent_cached(string or "")


async def agent_fields_for_insert_async(string: str | None) -> dict[str, Any]:
    """Return the parsed agent's row fields without building a model.

    Cache hits are answered inline and only real parser work goes to a
    thread. The result is the dict a ``UserAgentModel`` would hold, built for
    this call alone, so the caller may mutate it.
    """

    normalized = string or ""
    data = _get_cached_agent(normalized)
    if data is None:
        data = await run_cpu_task(_parse_record_cached, normalized)
    return _to_fields(normalized, data)


def cached_agent_fields(string: str) -> dict[str, Any] | None:
    """Return the agent's row fields only if the LRU already holds its parse.

    Never parses and never inserts, so a caller that must not spend parser
    time or evict entries other traffic relies on can still use a known value.
    """

    data = _get_cached_agent(string)
    return None if data is None else _to_fields(string, data)


def warm_user_agent_parser() -> None:
    """Pay the parsers' one-time initialisation before the first request.

    The first parse in a process compiles the ua-parser and crawler regexes
    (about 75-120 ms), which would otherwise block the event loop on the first
    request after every deploy. The LRU cache is left untouched.
    """

    _parse_record(WARM_UP_USER_AGENT)
