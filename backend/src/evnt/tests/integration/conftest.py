"""Fixtures for tests that run against a real ClickHouse (and RabbitMQ).

Every other test fakes the backends, so nothing else proves the generated DDL
is accepted, that rows written by the collector come back intact, or that the
RabbitMQ worker really delivers. These tests SKIP when the backends are
unreachable; CI sets ``EVNT_IT_REQUIRED=1``, which turns a skip into a failure
so the job cannot pass while testing nothing.

    EVNT_IT_CLICKHOUSE_HOST / _PORT / _USER / _PASSWORD   (default localhost:8123)
    EVNT_IT_RABBITMQ_HOST / _PORT / _USER / _PASSWORD     (default localhost:5672)
"""

import os
import socket
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import clickhouse_connect
import pytest
from clickhouse_connect.driver.client import Client
from pydantic import SecretStr

from evnt.config import settings

REQUIRED = os.environ.get("EVNT_IT_REQUIRED") == "1"


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    user: str
    password: str


def _endpoint(name: str, port: int, user: str, password: str) -> Endpoint:
    prefix = f"EVNT_IT_{name}_"
    return Endpoint(
        host=os.environ.get(prefix + "HOST", "localhost"),
        port=int(os.environ.get(prefix + "PORT", port)),
        user=os.environ.get(prefix + "USER", user),
        password=os.environ.get(prefix + "PASSWORD", password),
    )


def _unavailable(reason: str) -> None:
    if REQUIRED:
        pytest.fail(f"EVNT_IT_REQUIRED=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def clickhouse_endpoint() -> Endpoint:
    return _endpoint("CLICKHOUSE", 8123, "default", "")


@pytest.fixture(scope="session")
def database(clickhouse_endpoint: Endpoint) -> Iterator[str]:
    """A throwaway database name; dropped after the session."""
    try:
        client = clickhouse_connect.get_client(
            host=clickhouse_endpoint.host,
            port=clickhouse_endpoint.port,
            username=clickhouse_endpoint.user,
            password=clickhouse_endpoint.password,
        )
    except Exception as exc:
        _unavailable(f"ClickHouse at {clickhouse_endpoint.host}:{clickhouse_endpoint.port}: {exc}")
    name = f"evnt_it_{uuid.uuid4().hex[:8]}"
    yield name
    client.command(f"DROP DATABASE IF EXISTS {name}")
    client.close()


@pytest.fixture
def ch(clickhouse_endpoint: Endpoint, database: str) -> Iterator[Client]:
    """A synchronous client for assertions, independent of the code under test."""
    client = clickhouse_connect.get_client(
        host=clickhouse_endpoint.host,
        port=clickhouse_endpoint.port,
        username=clickhouse_endpoint.user,
        password=clickhouse_endpoint.password,
    )
    yield client
    client.close()


@pytest.fixture
def clickhouse_settings(
    monkeypatch: pytest.MonkeyPatch,
    clickhouse_endpoint: Endpoint,
    database: str,
) -> None:
    """Point the global settings at the test ClickHouse and database."""
    connection = settings.clickhouse.connection
    monkeypatch.setattr(connection, "host", clickhouse_endpoint.host)
    monkeypatch.setattr(connection, "port", clickhouse_endpoint.port)
    monkeypatch.setattr(connection, "username", clickhouse_endpoint.user)
    monkeypatch.setattr(connection, "password", SecretStr(clickhouse_endpoint.password))
    monkeypatch.setattr(settings.clickhouse.configuration, "database", database)
    monkeypatch.setattr(settings.clickhouse, "startup_timeout_seconds", 5)
    monkeypatch.setattr(settings.ingest, "mode", "direct")


@pytest.fixture
def rabbitmq_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the global settings at the test RabbitMQ, on a queue of its own."""
    endpoint = _endpoint("RABBITMQ", 5672, "guest", "guest")
    try:
        socket.create_connection((endpoint.host, endpoint.port), timeout=3).close()
    except OSError as exc:
        _unavailable(f"RabbitMQ at {endpoint.host}:{endpoint.port}: {exc}")

    rabbitmq = settings.ingest.rabbitmq
    queue = f"evnt.it.{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(rabbitmq, "host", endpoint.host)
    monkeypatch.setattr(rabbitmq, "port", endpoint.port)
    monkeypatch.setattr(rabbitmq, "username", endpoint.user)
    monkeypatch.setattr(rabbitmq, "password", SecretStr(endpoint.password))
    monkeypatch.setattr(rabbitmq, "queue_name", queue)
    monkeypatch.setattr(rabbitmq, "failed_queue_name", f"{queue}.failed")
    monkeypatch.setattr(rabbitmq, "batch_timeout_ms", 200)
    monkeypatch.setattr(rabbitmq, "startup_timeout_seconds", 5)
