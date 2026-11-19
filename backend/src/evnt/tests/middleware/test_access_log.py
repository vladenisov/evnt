"""The access-log middleware skips its work only when the line would be dropped."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
import structlog
from fastapi_structlog.middleware import AccessLogMiddleware
from fastapi_structlog.middleware import access_log as parent_module
from fastapi_structlog.middleware.context_scope import current_scope

from evnt.middleware.access_log import PathSkippingAccessLogMiddleware

ACCESS_LOGGER = "evnt.tests.access_log"


class HandlerError(RuntimeError):
    pass


class RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture(autouse=True)
def _structlog_configured(monkeypatch) -> None:
    # Unconfigured, the parent attaches a stdout handler to its logger.
    monkeypatch.setattr(structlog, "is_configured", lambda: True)


@pytest.fixture
def handler() -> RecordingHandler:
    logger = logging.getLogger(ACCESS_LOGGER)
    recording = RecordingHandler()
    logger.addHandler(recording)
    logger.propagate = False
    yield recording
    logger.removeHandler(recording)
    logger.setLevel(logging.NOTSET)


def _scope(path: str = "/com.snowplowanalytics.snowplow/tp2") -> dict[str, Any]:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "root_path": "",
        "query_string": b"",
        "headers": [(b"user-agent", b"tracker/1.0"), (b"content-length", b"2")],
        "client": ("203.0.113.7", 51234),
        "path_params": {"vendor": "acme"},
    }


async def _endpoint() -> None:
    """Stands in for a route, so the parent's api_source lookup has a target."""


async def ok_app(scope, receive, send) -> None:
    scope["endpoint"] = _endpoint
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def failing_app(scope, receive, send) -> None:
    scope["endpoint"] = _endpoint
    raise HandlerError("handler failed")


def _middleware(cls: type, app: Callable, level: int, **kwargs: Any):
    middleware = cls(app, **kwargs)
    middleware.logger = logging.getLogger(ACCESS_LOGGER)
    middleware.logger.setLevel(level)
    return middleware


def _run(middleware, scope: dict[str, Any] | None = None) -> dict[str, Any]:
    """Drive one request like StructlogMiddleware does and report what it saw."""

    sent: list[dict[str, Any]] = []
    outcome: dict[str, Any] = {"sent": sent, "error": None}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def request() -> None:
        structlog.contextvars.clear_contextvars()
        try:
            await middleware(scope or _scope(), receive, send)
        except HandlerError as exc:
            outcome["error"] = exc
        outcome["context"] = structlog.contextvars.get_contextvars()
        outcome["scope"] = current_scope.get(None)

    asyncio.run(request())
    return outcome


@pytest.mark.parametrize("app", [ok_app, failing_app])
def test_enabled_access_line_matches_the_parent(handler, app):
    ours = _run(_middleware(PathSkippingAccessLogMiddleware, app, logging.INFO))
    ours_records = list(handler.records)
    handler.records.clear()
    parents = _run(_middleware(AccessLogMiddleware, app, logging.INFO))

    assert len(ours_records) == len(handler.records) == 1
    ours_atoms, parent_atoms = ours_records[0].args, handler.records[0].args
    assert ours_atoms["s"] == parent_atoms["s"] == (204 if app is ok_app else 500)
    assert ours_atoms["r"] == parent_atoms["r"]
    assert ours_atoms["a"] == parent_atoms["a"] == "tracker/1.0"
    assert ours["context"] == parents["context"]
    assert ours["sent"] == parents["sent"]
    assert type(ours["error"]) is type(parents["error"])


def test_disabled_access_line_passes_the_response_through_without_building_it(handler, monkeypatch):
    def fail_if_built(*args, **kwargs):
        raise AssertionError("access line built while the logger is disabled")

    monkeypatch.setattr(parent_module, "AccessLogAtoms", fail_if_built)
    scope = _scope()

    outcome = _run(_middleware(PathSkippingAccessLogMiddleware, ok_app, logging.WARNING), scope)

    assert handler.records == []
    assert outcome["error"] is None
    assert [message["type"] for message in outcome["sent"]] == [
        "http.response.start",
        "http.response.body",
    ]
    assert outcome["sent"][0]["status"] == 204
    assert outcome["scope"] is scope
    assert outcome["context"] == {}


def test_disabled_access_line_keeps_the_failure_context_for_the_error_log(handler):
    ours = _run(_middleware(PathSkippingAccessLogMiddleware, failing_app, logging.WARNING))
    parents = _run(_middleware(AccessLogMiddleware, failing_app, logging.WARNING))

    assert handler.records == []
    assert isinstance(ours["error"], HandlerError)
    assert ours["context"] == parents["context"]
    assert ours["context"]["response"] == {"status_code": 500}
    assert ours["context"]["path_params"] == {"vendor": "acme"}
    assert ours["context"]["api_source"]["function"] == "_endpoint"


def test_disabled_access_line_still_binds_the_session(handler):
    scope = {**_scope(), "session": {"user": "u1"}}

    outcome = _run(_middleware(PathSkippingAccessLogMiddleware, ok_app, logging.WARNING), scope)

    assert outcome["context"] == {"session": {"user": "u1"}}


def test_level_change_takes_effect_on_the_next_request(handler):
    middleware = _middleware(PathSkippingAccessLogMiddleware, ok_app, logging.WARNING)
    _run(middleware)
    assert handler.records == []

    middleware.logger.setLevel(logging.INFO)
    _run(middleware)

    assert len(handler.records) == 1


@pytest.mark.parametrize("level", [logging.INFO, logging.WARNING])
def test_excluded_paths_never_log(handler, level):
    middleware = _middleware(
        PathSkippingAccessLogMiddleware, ok_app, level, excluded_paths=("/health",)
    )

    outcome = _run(middleware, _scope("/health"))

    assert handler.records == []
    assert outcome["sent"][0]["status"] == 204


@pytest.mark.parametrize("level", [logging.INFO, logging.WARNING])
def test_methods_filter_is_respected(handler, level):
    middleware = _middleware(PathSkippingAccessLogMiddleware, ok_app, level)
    middleware.methods = {"GET"}

    outcome = _run(middleware)

    assert handler.records == []
    assert outcome["sent"][0]["status"] == 204


def test_non_http_scopes_pass_through(handler):
    seen: list[str] = []

    async def lifespan_app(scope, receive, send) -> None:
        seen.append(scope["type"])

    middleware = _middleware(PathSkippingAccessLogMiddleware, lifespan_app, logging.WARNING)

    asyncio.run(middleware({"type": "lifespan"}, None, None))

    assert seen == ["lifespan"]
    assert handler.records == []
