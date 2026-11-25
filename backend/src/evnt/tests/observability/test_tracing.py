"""Span helpers: the real agent's when APM is enabled and installed, no-ops otherwise."""

import importlib.util
import sys
from types import ModuleType

import pytest

from evnt.config import settings
from evnt.observability import tracing


def _load_copy(name: str) -> ModuleType:
    """Execute tracing.py as a private copy, so it reads the settings again."""
    assert tracing.__file__ is not None
    spec = importlib.util.spec_from_file_location(name, tracing.__file__)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_without_elastic_apm(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """A copy of tracing.py with APM enabled and ``elasticapm`` unimportable."""
    monkeypatch.setattr(settings.elastic_apm, "enabled", True)
    # A None entry makes the import machinery raise ImportError.
    monkeypatch.setitem(sys.modules, "elasticapm.contrib.asyncio.traces", None)
    return _load_copy("_tracing_without_apm")


def test_exports_agent_spans_when_apm_is_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    traces = pytest.importorskip(
        "elasticapm.contrib.asyncio.traces",
        reason="needs the optional `apm` extra",
    )
    monkeypatch.setattr(settings.elastic_apm, "enabled", True)

    enabled = _load_copy("_tracing_with_apm")

    assert enabled.async_capture_span is traces.async_capture_span
    assert enabled.capture_span is traces.capture_span


def test_spans_are_noops_when_apm_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The extra being installed is not enough: a disabled agent costs nothing."""
    monkeypatch.setattr(settings.elastic_apm, "enabled", False)

    disabled = _load_copy("_tracing_apm_disabled")

    async def work() -> str:
        return "done"

    assert disabled.async_capture_span()(work) is work
    assert disabled.capture_span()(work) is work


def test_noop_span_decorator_returns_the_function_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = _load_without_elastic_apm(monkeypatch)

    def sync_work() -> str:
        return "sync"

    async def async_work() -> str:
        return "async"

    assert fallback.capture_span()(sync_work) is sync_work
    assert fallback.capture_span("named", span_type="db")(sync_work) is sync_work
    assert fallback.async_capture_span()(async_work) is async_work


def test_noop_sync_span_runs_the_body_and_propagates_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = _load_without_elastic_apm(monkeypatch)

    with fallback.capture_span("work") as span:
        assert isinstance(span, fallback.capture_span)

    with pytest.raises(ValueError, match="boom"), fallback.capture_span("work"):
        raise ValueError("boom")


@pytest.mark.anyio
async def test_noop_async_span_runs_the_body_and_propagates_errors(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: str,
) -> None:
    fallback = _load_without_elastic_apm(monkeypatch)

    async with fallback.async_capture_span("work") as span:
        assert isinstance(span, fallback.async_capture_span)

    with pytest.raises(ValueError, match="boom"):
        async with fallback.async_capture_span("work"):
            raise ValueError("boom")


def test_fallback_does_not_replace_the_imported_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = (tracing.async_capture_span, tracing.capture_span)

    _load_without_elastic_apm(monkeypatch)

    assert (tracing.async_capture_span, tracing.capture_span) == before
