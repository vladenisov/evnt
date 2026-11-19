"""Per-event warnings in payload parsing and JSON decoding are rate-limited per key."""

import asyncio
from types import SimpleNamespace

import pytest

from evnt.observability.ratelimit import KeyedRateLimiter
from evnt.tracker import payload as payload_module
from evnt.tracker import utils as utils_module

SCREEN_VIEW_URI = "iglu:com.snowplowanalytics.mobile/screen_view/jsonschema/1-0-0"
OTHER_URI = "iglu:com.acme/unknown_context/jsonschema/1-0-0"
INTERVAL = 60.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict]] = []

    def debug(self, *args, **kwargs) -> None:
        pass

    def warning(self, event, **kwargs) -> None:
        self.warnings.append((event, kwargs))

    def named(self, event: str) -> list[dict]:
        return [kwargs for name, kwargs in self.warnings if name == event]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def recording_logger(monkeypatch) -> RecordingLogger:
    recording = RecordingLogger()
    monkeypatch.setattr(payload_module, "logger", recording)
    return recording


@pytest.fixture
def logger(monkeypatch, clock, recording_logger) -> RecordingLogger:
    recording = recording_logger
    monkeypatch.setattr(payload_module, "_NO_PARSER_LOG", KeyedRateLimiter(INTERVAL, clock=clock))
    monkeypatch.setattr(
        payload_module, "_MALFORMED_INPUT_LOG", KeyedRateLimiter(INTERVAL, clock=clock)
    )
    monkeypatch.setattr(
        payload_module,
        "_VALIDATION_LOG",
        {
            "contexts": KeyedRateLimiter(INTERVAL, clock=clock),
            "event": KeyedRateLimiter(INTERVAL, clock=clock),
        },
    )
    return recording


def _contexts(*schemas: str, data: dict | None = None) -> dict:
    return {"data": [{"schema": schema, "data": data or {}} for schema in schemas]}


def _parse_contexts(contexts: dict) -> None:
    asyncio.run(payload_module.parse_contexts(contexts, SimpleNamespace()))


def test_no_parser_warning_logs_first_event_then_once_per_interval(logger, clock):
    for _ in range(50):
        _parse_contexts(_contexts(SCREEN_VIEW_URI))

    lines = logger.named("Schema has no parser")
    assert len(lines) == 1
    assert lines[0]["schema_uri"] == SCREEN_VIEW_URI
    assert lines[0]["schema"] == "com.snowplowanalytics.mobile/screen_view"
    assert lines[0]["suppressed"] == 0

    clock.now += INTERVAL
    _parse_contexts(_contexts(SCREEN_VIEW_URI))

    lines = logger.named("Schema has no parser")
    assert len(lines) == 2
    assert lines[1]["suppressed"] == 49


def test_no_parser_warning_is_limited_per_schema(logger):
    for _ in range(10):
        _parse_contexts(_contexts(SCREEN_VIEW_URI, OTHER_URI))

    lines = logger.named("Schema has no parser")
    assert [line["schema_uri"] for line in lines] == [SCREEN_VIEW_URI, OTHER_URI]


def test_validation_warning_is_limited_per_stage_and_schema(logger, monkeypatch, clock):
    monkeypatch.setattr(
        payload_module,
        "validate_iglu_payload",
        lambda schema_uri, data: payload_module.ValidationResult(
            status="warning", error="invalid payload"
        ),
    )

    for _ in range(20):
        _parse_contexts(_contexts(SCREEN_VIEW_URI))
        payload_module._log_validation_result(
            payload_module.ValidationResult(status="warning", error="bad"),
            schema_uri=SCREEN_VIEW_URI,
            validation_stage="event",
        )

    lines = logger.named("Iglu validation warning")
    assert [(line["validation_stage"], line["suppressed"]) for line in lines] == [
        ("contexts", 0),
        ("event", 0),
    ]

    clock.now += INTERVAL
    _parse_contexts(_contexts(SCREEN_VIEW_URI))

    lines = logger.named("Iglu validation warning")
    assert lines[-1]["validation_stage"] == "contexts"
    assert lines[-1]["suppressed"] == 19
    assert lines[-1]["error"] == "invalid payload"


def test_validation_passing_is_not_counted(logger, monkeypatch):
    monkeypatch.setattr(
        payload_module,
        "validate_iglu_payload",
        lambda schema_uri, data: payload_module.ValidationResult(status="ok"),
    )

    _parse_contexts(_contexts(SCREEN_VIEW_URI))

    assert len(payload_module._VALIDATION_LOG["contexts"]) == 0


def test_malformed_context_warnings_are_limited_per_message(logger, clock):
    contexts = {
        "data": [
            "not a dict",
            {"schema": SCREEN_VIEW_URI},
            {"schema": 42, "data": {}},
            {"schema": SCREEN_VIEW_URI, "data": ["not", "a", "dict"]},
        ]
    }
    for _ in range(5):
        _parse_contexts(contexts)

    events = [event for event, _ in logger.warnings if event != "Iglu validation warning"]
    assert events == [
        "Context is not a dict",
        "Context is missing required keys",
        "Wrong schema type",
        "Wrong data type",
    ]
    assert all(kwargs["suppressed"] == 0 for _, kwargs in logger.warnings)

    clock.now += INTERVAL
    _parse_contexts(contexts)

    for event in events:
        assert [line["suppressed"] for line in logger.named(event)] == [0, 4]


def test_cookie_and_value_warnings_are_limited(logger):
    for _ in range(3):
        assert payload_module.parse_cookies("_sp_id.1fff=abc.def") == {}
        assert payload_module._safe_uuid("not-a-uuid") == payload_module.DEFAULT_UUID
        assert payload_module._safe_datetime("yesterday") == payload_module.DEFAULT_DATE

    assert [event for event, _ in logger.warnings] == [
        "Incomplete _sp_id cookie",
        "Failed to parse UUID",
        "Failed to parse datetime",
    ]
    assert logger.warnings[1][1]["value"] == "not-a-uuid"


def test_reset_log_rate_limits_forgets_every_key(recording_logger):
    logger = recording_logger
    _parse_contexts(_contexts(SCREEN_VIEW_URI))
    payload_module._safe_uuid("not-a-uuid")

    payload_module.reset_log_rate_limits()

    _parse_contexts(_contexts(SCREEN_VIEW_URI))
    payload_module._safe_uuid("not-a-uuid")
    assert len(logger.named("Schema has no parser")) == 2
    assert len(logger.named("Failed to parse UUID")) == 2


def test_module_limiters_are_all_reset_by_the_helper():
    limiters = {
        id(payload_module._NO_PARSER_LOG),
        id(payload_module._MALFORMED_INPUT_LOG),
        *(id(limiter) for limiter in payload_module._VALIDATION_LOG.values()),
    }

    assert {id(limiter) for limiter in payload_module._LOG_LIMITERS} == limiters


def test_evicted_schema_reports_its_held_back_count_under_its_own_uri(
    monkeypatch, clock, recording_logger
):
    logger = recording_logger
    monkeypatch.setattr(
        payload_module, "_NO_PARSER_LOG", KeyedRateLimiter(INTERVAL, max_keys=1, clock=clock)
    )
    for _ in range(6):
        _parse_contexts(_contexts(SCREEN_VIEW_URI))

    clock.now += INTERVAL
    _parse_contexts(_contexts(OTHER_URI))

    lines = logger.named("Schema has no parser")
    assert [(line["schema_uri"], line["suppressed"], line.get("evicted")) for line in lines] == [
        (SCREEN_VIEW_URI, 0, None),
        (SCREEN_VIEW_URI, 5, True),
        (OTHER_URI, 0, None),
    ]


def test_overflow_line_does_not_pin_other_schemas_counts_on_this_one(
    monkeypatch, clock, recording_logger
):
    logger = recording_logger
    monkeypatch.setattr(
        payload_module, "_NO_PARSER_LOG", KeyedRateLimiter(INTERVAL, max_keys=1, clock=clock)
    )
    third_uri = "iglu:com.acme/third_context/jsonschema/1-0-0"
    _parse_contexts(_contexts(SCREEN_VIEW_URI))
    _parse_contexts(_contexts(OTHER_URI))
    for _ in range(3):
        _parse_contexts(_contexts(third_uri))

    clock.now += INTERVAL / 2
    _parse_contexts(_contexts(OTHER_URI))  # tracked key still fresh, so overflow is still closed
    clock.now += INTERVAL / 2
    _parse_contexts(_contexts(SCREEN_VIEW_URI, OTHER_URI))

    lines = logger.named("Schema has no parser")
    assert lines[1] == {
        "untracked_suppressed": 0,
        "schema": "com.acme/unknown_context",
        "schema_uri": OTHER_URI,
    }
    assert "suppressed" not in lines[-1]
    assert lines[-1]["schema_uri"] == OTHER_URI
    assert lines[-1]["untracked_suppressed"] == 4


def test_evicted_validation_and_malformed_keys_are_reported_by_name(
    monkeypatch, clock, recording_logger
):
    logger = recording_logger
    monkeypatch.setattr(
        payload_module,
        "_VALIDATION_LOG",
        {
            "contexts": KeyedRateLimiter(INTERVAL, max_keys=1, clock=clock),
            "event": KeyedRateLimiter(INTERVAL, max_keys=1, clock=clock),
        },
    )
    monkeypatch.setattr(
        payload_module, "_MALFORMED_INPUT_LOG", KeyedRateLimiter(INTERVAL, max_keys=1, clock=clock)
    )
    warning = payload_module.ValidationResult(status="warning", error="bad")
    for _ in range(3):
        payload_module._log_validation_result(
            warning, schema_uri=SCREEN_VIEW_URI, validation_stage="event"
        )
        payload_module._safe_uuid("not-a-uuid")

    clock.now += INTERVAL
    payload_module._log_validation_result(warning, schema_uri=OTHER_URI, validation_stage="event")
    payload_module._safe_datetime("yesterday")

    evicted = [(event, kwargs) for event, kwargs in logger.warnings if kwargs.get("evicted")]
    assert evicted == [
        (
            "Iglu validation warning",
            {
                "suppressed": 2,
                "evicted": True,
                "schema": SCREEN_VIEW_URI,
                "validation_stage": "event",
            },
        ),
        ("Failed to parse UUID", {"suppressed": 2, "evicted": True}),
    ]


def test_undecodable_json_payload_warning_is_limited(monkeypatch, clock):
    logger = RecordingLogger()
    monkeypatch.setattr(utils_module, "logger", logger)
    monkeypatch.setattr(
        utils_module, "_MALFORMED_JSON_LOG", KeyedRateLimiter(INTERVAL, clock=clock)
    )
    for _ in range(5):
        assert utils_module.find_available("not-json", None) is None
        assert utils_module.find_available("[1, 2]", None) is None

    decode_event = "Failed to decode Snowplow JSON payload"
    type_event = "Snowplow JSON payload has unexpected type"
    assert [event for event, _ in logger.warnings] == [decode_event, type_event]
    assert logger.named(decode_event)[0]["suppressed"] == 0
    assert logger.named(type_event)[0] == {"suppressed": 0, "decoded_type": "list"}

    clock.now += INTERVAL
    utils_module.find_available("not-json", None)
    utils_module.find_available("[1, 2]", None)

    assert [line["suppressed"] for line in logger.named(decode_event)] == [0, 4]
    assert [line["suppressed"] for line in logger.named(type_event)] == [0, 4]


def test_reset_log_rate_limits_forgets_json_payload_warnings(monkeypatch):
    logger = RecordingLogger()
    monkeypatch.setattr(utils_module, "logger", logger)
    utils_module.find_available("not-json", None)

    utils_module.reset_log_rate_limits()

    utils_module.find_available("not-json", None)
    assert len(logger.named("Failed to decode Snowplow JSON payload")) == 2
