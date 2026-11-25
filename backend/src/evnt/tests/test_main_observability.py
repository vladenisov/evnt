"""``create_app`` wiring of the optional Elastic APM and Sentry integrations."""

import sys
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from evnt import __version__
from evnt.config import settings
from evnt.tests.support import build_app


def _middleware_named(app: FastAPI, name: str) -> list[Any]:
    return [m for m in app.user_middleware if m.cls.__name__ == name]


@pytest.fixture
def apm_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    pytest.importorskip("elasticapm", reason="needs the optional `apm` extra")
    import elasticapm
    from elasticapm.instrumentation import control

    from evnt.tests.observability.apm_support import offline_apm_env

    offline_apm_env(monkeypatch)
    monkeypatch.setattr(settings.elastic_apm, "enabled", True)
    monkeypatch.setattr(settings.common, "service_name", "evnt-under-test")
    yield
    client = elasticapm.get_client()
    if client is not None:
        client.close()
    # The middleware always instruments Starlette's error handler.
    control.uninstrument()


@pytest.fixture
def sentry_init_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Enable Sentry and record ``sentry_sdk.init`` instead of starting a client."""
    sentry_sdk = pytest.importorskip("sentry_sdk", reason="needs the optional `sentry` extra")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(settings.sentry, "enabled", True)
    monkeypatch.setattr(settings.common, "service_name", "evnt-under-test")
    return calls


def test_integrations_are_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.elastic_apm, "enabled", False)
    monkeypatch.setattr(settings.sentry, "enabled", False)

    app = build_app(monkeypatch)

    assert _middleware_named(app, "ElasticAPM") == []


@pytest.mark.usefixtures("apm_enabled")
def test_elastic_apm_middleware_is_added_with_a_configured_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elasticapm import Client

    app = build_app(monkeypatch)

    (middleware,) = _middleware_named(app, "ElasticAPM")
    client = middleware.kwargs["client"]
    assert isinstance(client, Client)
    assert client.config.service_name == "evnt-under-test"


@pytest.mark.usefixtures("apm_enabled")
def test_elastic_apm_records_a_transaction_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import elasticapm

    from evnt.tests.observability.apm_support import RecordingTransport

    app = build_app(monkeypatch)

    with TestClient(app) as http:
        assert http.get("/live").status_code == 204

    client = elasticapm.get_client()
    assert client is not None
    client._transport.flush()
    transactions = RecordingTransport.of_type("transaction")
    assert [t["name"] for t in transactions] == ["GET /live"]
    assert transactions[0]["context"]["response"]["status_code"] == 204


def test_elastic_apm_enabled_without_the_extra_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.elastic_apm, "enabled", True)
    # A None entry makes the import machinery raise ImportError.
    monkeypatch.setitem(sys.modules, "elasticapm.contrib.starlette", None)

    with pytest.raises(RuntimeError, match="uv sync --extra apm") as excinfo:
        build_app(monkeypatch)

    assert isinstance(excinfo.value.__cause__, ImportError)


def test_sentry_is_initialised_from_settings(
    monkeypatch: pytest.MonkeyPatch,
    sentry_init_calls: list[dict[str, Any]],
) -> None:
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    monkeypatch.setattr(settings.sentry, "dsn", "https://public@sentry.invalid/1")
    monkeypatch.setattr(settings.sentry, "environment", "prod")
    monkeypatch.setattr(settings.sentry, "traces_sample_rate", 0.25)

    build_app(monkeypatch)

    (call,) = sentry_init_calls
    assert call["dsn"] == "https://public@sentry.invalid/1"
    assert call["environment"] == "prod"
    assert call["traces_sample_rate"] == 0.25
    assert call["release"] == f"evnt-under-test@{__version__}"
    integration_types = {type(integration) for integration in call["integrations"]}
    assert {FastApiIntegration, StarletteIntegration} <= integration_types


def test_sentry_enabled_without_dsn_starts_no_client(
    monkeypatch: pytest.MonkeyPatch,
    sentry_init_calls: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(settings.sentry, "dsn", None)

    app = build_app(monkeypatch)

    assert isinstance(app, FastAPI)
    assert sentry_init_calls == []


def test_sentry_enabled_without_the_extra_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.sentry, "enabled", True)
    # A None entry makes the import machinery raise ImportError.
    monkeypatch.setitem(sys.modules, "fastapi_structlog.sentry", None)

    with pytest.raises(RuntimeError, match="uv sync --extra sentry") as excinfo:
        build_app(monkeypatch)

    assert isinstance(excinfo.value.__cause__, ImportError)
