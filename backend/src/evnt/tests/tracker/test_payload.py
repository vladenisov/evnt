import asyncio
from pathlib import Path
from types import SimpleNamespace

from evnt.tracker import payload as payload_module

parse_cookies = payload_module.parse_cookies
parse_contexts = payload_module.parse_contexts
parse_event = payload_module.parse_event


class _RecordingLogger:
    def __init__(self):
        self.debugs = []
        self.warnings = []
        self.errors = []

    def debug(self, *args, **kwargs):
        self.debugs.append((args, kwargs))

    def warning(self, *args, **kwargs):
        self.warnings.append((args, kwargs))

    def error(self, *args, **kwargs):
        self.errors.append((args, kwargs))


def test_parse_cookies_truncated_sp_id_cookie_returns_empty_dict():
    cookies_str = "_sp_id.123=abc.def"

    result = parse_cookies(cookies_str)

    assert result == {}


def test_parse_contexts_keeps_existing_resolution_when_browser_context_has_nulls():
    model = SimpleNamespace(
        res="1920x1080",
        vp="1280x720",
        ds="1280x720",
        browser_extra={},
    )
    contexts = {
        "data": [
            {
                "schema": "iglu:com.snowplowanalytics.snowplow/browser_context/jsonschema/2-0-0",
                "data": {
                    "resolution": None,
                    "viewport": None,
                    "documentSize": "",
                },
            },
        ],
    }

    result = asyncio.run(parse_contexts(contexts, model))

    assert result.res == "1920x1080"
    assert result.vp == "1280x720"
    assert result.ds == "1280x720"


def test_parse_contexts_logs_validation_warning_but_keeps_processing(monkeypatch):
    logger = _RecordingLogger()
    monkeypatch.setattr(payload_module, "logger", logger)
    monkeypatch.setattr(
        payload_module,
        "validate_iglu_payload",
        lambda schema_uri, data: payload_module.ValidationResult(
            status="warning",
            error="invalid payload",
        ),
    )

    model = SimpleNamespace(
        res="1920x1080",
        vp="1280x720",
        ds="1280x720",
        browser_extra={},
    )
    contexts = {
        "data": [
            {
                "schema": "iglu:com.snowplowanalytics.snowplow/browser_context/jsonschema/2-0-0",
                "data": {
                    "resolution": None,
                    "viewport": None,
                    "documentSize": "",
                },
            },
        ],
    }

    result = asyncio.run(parse_contexts(contexts, model))

    assert result.res == "1920x1080"
    assert any(
        args[0] == "Iglu validation warning"
        and kwargs["validation_stage"] == "contexts"
        and kwargs["schema"]
        == "iglu:com.snowplowanalytics.snowplow/browser_context/jsonschema/2-0-0"
        for args, kwargs in logger.warnings
    )


def test_parse_contexts_logs_validation_debug_when_validation_passes(monkeypatch):
    logger = _RecordingLogger()
    monkeypatch.setattr(payload_module, "logger", logger)
    monkeypatch.setattr(
        payload_module,
        "validate_iglu_payload",
        lambda schema_uri, data: payload_module.ValidationResult(
            status="ok",
            schema_path=Path("/tmp/browser_context.json"),
        ),
    )

    model = SimpleNamespace(
        res="1920x1080",
        vp="1280x720",
        ds="1280x720",
        browser_extra={},
    )
    contexts = {
        "data": [
            {
                "schema": "iglu:com.snowplowanalytics.snowplow/browser_context/jsonschema/2-0-0",
                "data": {
                    "resolution": None,
                    "viewport": None,
                    "documentSize": "",
                },
            },
        ],
    }

    asyncio.run(parse_contexts(contexts, model))

    assert any(
        args[0] == "Iglu validation passed"
        and kwargs["validation_stage"] == "contexts"
        and kwargs["schema"]
        == "iglu:com.snowplowanalytics.snowplow/browser_context/jsonschema/2-0-0"
        and kwargs["schema_path"] == "/tmp/browser_context.json"
        for args, kwargs in logger.debugs
    )


def test_parse_event_logs_validation_warning_but_keeps_processing(monkeypatch):
    logger = _RecordingLogger()
    monkeypatch.setattr(payload_module, "logger", logger)
    monkeypatch.setattr(
        payload_module,
        "schemas",
        SimpleNamespace(u2s_data="dev.snowplow.simple/u2s_data"),
    )
    monkeypatch.setattr(
        payload_module,
        "validate_iglu_payload",
        lambda schema_uri, data: payload_module.ValidationResult(
            status="warning",
            error="invalid event payload",
        ),
    )

    model = SimpleNamespace(ue={})
    event = {
        "data": {
            "schema": "iglu:com.acme/example_event/jsonschema/1-0-0",
            "data": {"foo": "bar"},
        },
    }

    result = asyncio.run(parse_event(event, model))

    assert result.ue["example_event"] == {"foo": "bar"}
    assert any(
        args[0] == "Iglu validation warning"
        and kwargs["validation_stage"] == "event"
        and kwargs["schema"] == "iglu:com.acme/example_event/jsonschema/1-0-0"
        for args, kwargs in logger.warnings
    )


def test_parse_event_logs_validation_debug_when_validation_passes(monkeypatch):
    logger = _RecordingLogger()
    monkeypatch.setattr(payload_module, "logger", logger)
    monkeypatch.setattr(
        payload_module,
        "schemas",
        SimpleNamespace(u2s_data="dev.snowplow.simple/u2s_data"),
    )
    monkeypatch.setattr(
        payload_module,
        "validate_iglu_payload",
        lambda schema_uri, data: payload_module.ValidationResult(
            status="ok",
            schema_path=Path("/tmp/example_event.json"),
        ),
    )

    model = SimpleNamespace(ue={})
    event = {
        "data": {
            "schema": "iglu:com.acme/example_event/jsonschema/1-0-0",
            "data": {"foo": "bar"},
        },
    }

    asyncio.run(parse_event(event, model))

    assert any(
        args[0] == "Iglu validation passed"
        and kwargs["validation_stage"] == "event"
        and kwargs["schema"] == "iglu:com.acme/example_event/jsonschema/1-0-0"
        and kwargs["schema_path"] == "/tmp/example_event.json"
        for args, kwargs in logger.debugs
    )
