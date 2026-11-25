"""``create_elastic_apm_client`` builds a real agent from the evnt settings."""

from collections.abc import Iterator

import pytest

pytest.importorskip("elasticapm", reason="needs the optional `apm` extra")

import elasticapm
from elasticapm import Client

from evnt.config import settings
from evnt.observability.apm import create_elastic_apm_client
from evnt.tests.observability.apm_support import (
    RecordingTransport,
    offline_apm_env,
)


@pytest.fixture
def apm_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Client]:
    offline_apm_env(monkeypatch)
    monkeypatch.setattr(settings.common, "service_name", "evnt-under-test")
    client = create_elastic_apm_client()
    yield client
    client.close()


def test_client_is_named_after_the_service(apm_client: Client) -> None:
    assert apm_client.config.service_name == "evnt-under-test"
    assert apm_client.config.framework_name == "starlette"
    assert apm_client.config.errors == {}


def test_client_becomes_the_process_wide_agent(apm_client: Client) -> None:
    assert elasticapm.get_client() is apm_client


def test_client_reports_through_the_configured_transport(apm_client: Client) -> None:
    apm_client.begin_transaction("request")
    apm_client.end_transaction("GET /probe", "HTTP 2xx")
    apm_client._transport.flush()

    assert [t["name"] for t in RecordingTransport.of_type("transaction")] == ["GET /probe"]
    (metadata,) = RecordingTransport.of_type("metadata")
    assert metadata["service"]["name"] == "evnt-under-test"


def test_enabled_flag_is_not_forwarded_to_the_agent(apm_client: Client) -> None:
    # `enabled` is evnt's switch; the agent itself must stay recording.
    assert apm_client.config.enabled is True
    assert apm_client.config.is_recording is True


def test_server_url_and_service_name_settings_reach_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offline_apm_env(monkeypatch)
    monkeypatch.setattr(settings.common, "service_name", "evnt-under-test")
    monkeypatch.setattr(settings.elastic_apm, "server_url", "http://apm.invalid:8200")
    monkeypatch.setattr(settings.elastic_apm, "service_name", "evnt-apm-name")

    client = create_elastic_apm_client()
    try:
        assert client.config.server_url == "http://apm.invalid:8200"
        assert client.config.service_name == "evnt-apm-name"
    finally:
        client.close()


def test_an_unset_server_url_leaves_the_agent_default(
    monkeypatch: pytest.MonkeyPatch, apm_client: Client
) -> None:
    assert settings.elastic_apm.server_url is None
    assert apm_client.config.server_url == "http://127.0.0.1:8200"
