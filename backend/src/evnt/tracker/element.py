"""Per-event validation of tracker payload elements.

A batch is validated one event at a time so that one malformed event never
costs the rest of its batch. Snowplow trackers drop a whole batch without retry
on 400/401/403/410/422 and retry anything else forever, so neither a 4xx nor a
5xx is an acceptable answer to a single bad event.

An event whose fields fail validation is still stored when it can be built:
each failing field falls back to its default, and the problem is recorded in
``extra.validation``. Only an event that cannot be built at all (not a JSON
object, or no usable event type ``e``, which the ``event_type`` Enum8 column
needs) is dropped, with a rate-limited log line and a Prometheus counter.
"""

from collections.abc import Sequence
from typing import Any, Final

import structlog
from pydantic import TypeAdapter, ValidationError
from pydantic_core import ErrorDetails

from evnt.observability.metrics import EVENTS_DROPPED
from evnt.observability.ratelimit import KeyedRateLimiter
from evnt.tracker.entities import MAX_STORED_ENTITIES, MAX_STORED_ENTITY_BYTES, StoredEntities
from evnt.tracker.iglu import schema_key
from evnt.tracker.models import PayloadElementModel

logger = structlog.stdlib.get_logger(__name__)

# ``extra.validation`` is written for attacker-controlled input, so both the
# number of entries and the length of every string in them are bounded.
MAX_VALIDATION_ENTRIES: Final[int] = 20
MAX_VALIDATION_ERROR_CHARS: Final[int] = 200
MAX_VALIDATION_NAME_CHARS: Final[int] = 200
# Raw values quoted inside an error message and the raw platform code kept in
# ``extra.tracker.platform``.
MAX_QUOTED_VALUE_CHARS: Final[int] = 80
MAX_RAW_PLATFORM_CHARS: Final[int] = 64

# The ``extra`` keys only the collector writes. ClickHouse splits JSON keys at
# dots, so a tracker key ``"tracker.ip"`` is the path ``extra.tracker.ip``: a
# key is owned when its first dotted segment is one of these.
COLLECTOR_OWNED_EXTRA_KEYS: Final[frozenset[str]] = frozenset(
    {"validation", "tracker", "contexts", "event_fingerprint", "collector"}
)
# Owned keys named in the one ``extra.validation`` entry of a stripped context.
_MAX_NAMED_OWNED_KEYS: Final[int] = 5

# Stored for an event whose ``p`` is missing or not a known platform code: the
# ``platform`` column is an Enum8, so the raw value cannot be stored there.
# ``app`` is Snowplow's "general application" code, the least specific one.
FALLBACK_PLATFORM: Final[str] = "app"

# Required fields that are not worth dropping an event for, and what to store
# when they are missing or invalid. Every other field has a default.
_REQUIRED_FALLBACKS: Final[dict[str, str]] = {"p": FALLBACK_PLATFORM, "tv": ""}
_EVENT_TYPE_FIELD: Final[str] = "e"
# Repairing removes every failing field, so a second failure is not expected;
# the bound only keeps a pathological input from looping.
_MAX_REPAIR_ROUNDS: Final[int] = 3

_DROP_LOG = KeyedRateLimiter()  # keyed by drop reason
_DROPPED = {
    reason: EVENTS_DROPPED.labels(reason=reason)
    for reason in ("not_an_object", "invalid_event_type", "unrepairable", "processing_error")
}


def reset_log_rate_limits() -> None:
    """Forget every rate-limited warning key, so tests start from a clean slate."""

    _DROP_LOG.reset()


def clip(value: str, limit: int) -> str:
    """Cut ``value`` to ``limit`` characters, marking the cut."""

    return value if len(value) <= limit else f"{value[: limit - 3]}..."


class EventIssues:
    """Collector-owned ``extra`` keys gathered while one event is processed.

    ``validation`` becomes ``extra.validation``, ``tracker`` ``extra.tracker``,
    ``contexts`` ``extra.contexts`` and ``fingerprint``
    ``extra.event_fingerprint``. The first three stay ``None`` until something
    is recorded, which is the case for almost every event.
    """

    __slots__ = ("contexts", "fingerprint", "tracker", "validation")

    def __init__(self) -> None:
        self.validation: list[dict[str, str]] | None = None
        self.tracker: dict[str, str] | None = None
        self.contexts: StoredEntities | None = None
        self.fingerprint: str | None = None

    def add(
        self,
        stage: str,
        error: str,
        *,
        field: str | None = None,
        schema: str | None = None,
    ) -> None:
        """Record a problem of an event that is still stored, up to the cap."""

        entries = self.validation
        if entries is None:
            entries = self.validation = []
        elif len(entries) >= MAX_VALIDATION_ENTRIES:
            return
        entry = {"stage": stage}
        if field is not None:
            entry["field"] = clip(field, MAX_VALIDATION_NAME_CHARS)
        if schema is not None:
            entry["schema"] = clip(schema, MAX_VALIDATION_NAME_CHARS)
        entry["error"] = clip(error, MAX_VALIDATION_ERROR_CHARS)
        entries.append(entry)

    def track(self, key: str, value: str) -> None:
        """Keep a tracker-sent value the stored row does not carry as sent."""

        if self.tracker is None:
            self.tracker = {}
        self.tracker[key] = value

    def keep_entity(self, schema_uri: str, data: Any) -> None:
        """Keep a context entity no handler consumed in ``extra.contexts``.

        An entity whose schema is not an Iglu URI has no ``vendor/name`` key to
        go under, so it is recorded as a problem instead.
        """

        key = schema_key(schema_uri)
        if key is None:
            self.add("contexts", "schema is not an Iglu URI", schema=schema_uri)
            return
        contexts = self.contexts
        if contexts is None:
            contexts = self.contexts = StoredEntities()
        contexts.add(key[0], key[1], data)

    def note_dropped_entities(self) -> None:
        """Record, once per event, the entities left out of ``extra.contexts``."""

        contexts = self.contexts
        if contexts is not None and contexts.dropped:
            self.add(
                "contexts",
                f"{contexts.dropped} context entities not stored: the limit is "
                f"{MAX_STORED_ENTITIES} entities and {MAX_STORED_ENTITY_BYTES} bytes per event",
            )

    def apply(self, extra: dict[str, Any]) -> None:
        """Write the collector-owned keys into ``extra``.

        Tracker data reaches ``extra`` only through ``com.acme/static_context``,
        which ``without_collector_keys`` strips of owned keys. These keys are
        still replaced or removed here, never merged, so that a reader can trust
        the collector wrote them whatever a future handler merges.
        """

        if self.validation:
            extra["validation"] = self.validation
        else:
            extra.pop("validation", None)
        if self.tracker:
            extra["tracker"] = self.tracker
        else:
            extra.pop("tracker", None)
        if self.contexts is not None and self.contexts.by_schema:
            extra["contexts"] = self.contexts.by_schema
        else:
            extra.pop("contexts", None)
        if self.fingerprint is not None:
            extra["event_fingerprint"] = self.fingerprint
        else:
            extra.pop("event_fingerprint", None)


def without_collector_keys(
    data: dict[str, Any], schema_uri: str, issues: EventIssues | None
) -> dict[str, Any]:
    """``data`` without the keys that would land on a collector-owned path.

    Such a key would either spoof a value analysts trust (``"tracker.ip"``) or,
    next to the same path written by the collector, make ClickHouse reject the
    whole insert as a duplicate path. Dropped keys are recorded once.
    """

    owned = [key for key in data if key.partition(".")[0] in COLLECTOR_OWNED_EXTRA_KEYS]
    if not owned:
        return data
    if issues is not None:
        named = ", ".join(quote_value(key) for key in owned[:_MAX_NAMED_OWNED_KEYS])
        issues.add(
            "contexts",
            f"{len(owned)} keys on collector-owned extra paths not merged: {named}",
            schema=schema_uri,
        )
    dropped = frozenset(owned)
    return {key: value for key, value in data.items() if key not in dropped}


def quote_value(value: Any) -> str:
    """Render a rejected value for an error message, bounded in length."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return clip(repr(value), MAX_QUOTED_VALUE_CHARS)
    return f"a value of type {type(value).__name__}"


def drop_event(reason: str, **fields: Any) -> None:
    """Count and log, rate-limited by reason, an event that is not stored."""

    _DROPPED[reason].inc()
    if (permit := _DROP_LOG.take(reason)) is None:
        return
    if permit.evicted is not None:
        key, suppressed = permit.evicted
        logger.warning("Dropped tracker event", reason=key, suppressed=suppressed, evicted=True)
    logger.warning("Dropped tracker event", reason=reason, **permit.log_fields(), **fields)


# Validates a whole batch in one call. Measured on a 100-event batch, one call
# per event cost about 20 us more per event than this: between two events the
# parsing code evicts the validator from the CPU caches.
_BATCH_VALIDATOR: Final = TypeAdapter(list[PayloadElementModel])

type BuiltEvent = tuple[PayloadElementModel | None, EventIssues]


def build_elements(raws: Sequence[Any]) -> list[BuiltEvent]:
    """Validate every event of a request, each with its own issue collector.

    A batch of valid events (or of already validated elements) takes a single
    validator call; only a batch with a failure is validated event by event,
    so the failing events can be repaired or dropped one at a time.
    """

    try:
        elements = _BATCH_VALIDATOR.validate_python(raws)
    except ValidationError:
        built: list[BuiltEvent] = []
        for raw in raws:
            issues = EventIssues()
            element = raw if isinstance(raw, PayloadElementModel) else build_element(raw, issues)
            built.append((element, issues))
        return built
    return [(element, EventIssues()) for element in elements]


def build_element(raw: Any, issues: EventIssues) -> PayloadElementModel | None:
    """Validate one tracker event, repairing what can be repaired.

    Valid input takes a single ``model_validate``, exactly as before, so the
    common case pays nothing for the leniency. Returns ``None`` (after logging
    and counting) for an event that cannot be built.
    """

    if not isinstance(raw, dict):
        drop_event("not_an_object", value_type=type(raw).__name__)
        return None
    try:
        return PayloadElementModel.model_validate(raw)
    except ValidationError as exc:
        return _repair(raw, exc, issues)


def _repair(
    raw: dict[str, Any],
    exc: ValidationError,
    issues: EventIssues,
) -> PayloadElementModel | None:
    """Replace every failing field with its default and validate again."""

    data = dict(raw)
    for _ in range(_MAX_REPAIR_ROUNDS):
        seen: set[str] = set()
        for error in exc.errors(include_url=False, include_context=False):
            loc = error["loc"]
            if not loc:
                drop_event("unrepairable", error=clip(error["msg"], MAX_VALIDATION_ERROR_CHARS))
                return None
            name = str(loc[0])
            if name in seen:
                # A union field reports one error per member type.
                continue
            seen.add(name)
            if name == _EVENT_TYPE_FIELD:
                drop_event("invalid_event_type", value=quote_value(raw.get(name)))
                return None
            _record_field_error(name, error, issues)
            fallback = _REQUIRED_FALLBACKS.get(name)
            if fallback is not None:
                data[name] = fallback
            else:
                data.pop(name, None)
        try:
            return PayloadElementModel.model_validate(data)
        except ValidationError as again:
            exc = again
    drop_event("unrepairable", error=clip(str(exc), MAX_VALIDATION_ERROR_CHARS))
    return None


def _record_field_error(name: str, error: ErrorDetails, issues: EventIssues) -> None:
    message = error["msg"]
    if error["type"] != "missing":
        value = error.get("input")
        message = f"{message}, got {quote_value(value)}"
        if name == "p" and value is not None:
            raw_platform = value if isinstance(value, str) else repr(value)
            issues.track("platform", clip(raw_platform, MAX_RAW_PLATFORM_CHARS))
    issues.add("payload", message, field=name)
