"""Behavioral tests for the health probe endpoint and ClickHouse checker.

``probe`` reads ``request.app.state`` for the health checker, ingest mode and
connector, so a tiny fake request/app-state stands in for the real app. These
cover the healthy (200 + table), unhealthy (502), and table-lookup-failure
branches, plus ``ClickHouseHealthChecker.check`` success and failure.
"""

from types import SimpleNamespace

import pytest
from core.healthcheck import ClickHouseHealthChecker, probe


class _FakeChecker:
    def __init__(self, status):
        self._status = status

    async def check(self):
        return dict(self._status)


class _FakeConnector:
    def __init__(self, table_name="evnt.events_local", raises=False):
        self._table_name = table_name
        self._raises = raises

    async def get_table_name(self, table_group="evnt"):
        if self._raises:
            raise RuntimeError("no table")
        return self._table_name


def _request(checker, ingest_mode="clickhouse", connector=None):
    state = SimpleNamespace(
        health_checker=checker,
        ingest_mode=ingest_mode,
        connector=connector or _FakeConnector(),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_probe_healthy_returns_200_with_table(anyio_backend):
    request = _request(_FakeChecker({"clickhouse": True}))

    response = await probe(request)

    assert response.status_code == 200
    assert b'"healthy":true' in response.body
    assert b"evnt.events_local" in response.body
    assert b'"ingest_mode":"clickhouse"' in response.body


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_probe_unhealthy_returns_502(anyio_backend):
    request = _request(_FakeChecker({"clickhouse": False}))

    response = await probe(request)

    assert response.status_code == 502
    assert b'"healthy":false' in response.body
    # No table key is added when unhealthy.
    assert b"events_local" not in response.body


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_probe_healthy_but_table_lookup_fails_omits_table(anyio_backend):
    request = _request(
        _FakeChecker({"clickhouse": True}),
        connector=_FakeConnector(raises=True),
    )

    response = await probe(request)

    assert response.status_code == 200
    assert b'"healthy":true' in response.body
    assert b'"table"' not in response.body


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_clickhouse_checker_reports_healthy_on_select_one(anyio_backend):
    class _Client:
        async def query(self, sql):
            assert sql == "SELECT 1"
            return SimpleNamespace(first_row=[1])

    status = await ClickHouseHealthChecker(_Client()).check()

    assert status == {"clickhouse": True}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_clickhouse_checker_reports_unhealthy_on_error(anyio_backend):
    class _Client:
        async def query(self, sql):
            raise RuntimeError("connection refused")

    status = await ClickHouseHealthChecker(_Client()).check()

    assert status == {"clickhouse": False}
