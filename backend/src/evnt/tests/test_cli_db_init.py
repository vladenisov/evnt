"""``evnt db init`` over a fake ClickHouse client; connector and DDL are the real ones."""

import pytest

import evnt.cli as cli_module
from evnt.storage.clickhouse import client as clickhouse_client


class _FakeClickHouse:
    def __init__(self, *, fail_on=None):
        self.fail_on = fail_on
        self.commands = []
        self.closed = False

    async def command(self, query):
        if self.fail_on is not None and self.fail_on in query:
            raise RuntimeError("ddl refused")
        self.commands.append(query)

    async def close(self):
        self.closed = True


def _patch_client(monkeypatch, client):
    client_kwargs = {}

    async def _get_async_client(**kwargs):
        client_kwargs.update(kwargs)
        return client

    monkeypatch.setattr(cli_module, "init_logging", lambda *args: None)
    monkeypatch.setattr(clickhouse_client, "get_async_client", _get_async_client)
    return client_kwargs


def test_db_init_creates_database_and_tables_then_closes_client(monkeypatch):
    client = _FakeClickHouse()
    client_kwargs = _patch_client(monkeypatch, client)

    result = cli_module.DBCommands().init()

    assert result == "ClickHouse initialization complete"
    assert client.closed is True
    assert client_kwargs["connector_limit"] == cli_module.settings.performance.db_pool_size

    config = cli_module.settings.clickhouse.configuration
    assert f"CREATE DATABASE IF NOT EXISTS {config.database}" in client.commands[0]
    created = [query for query in client.commands if "CREATE TABLE IF NOT EXISTS" in query]
    enabled_groups = [
        group
        for group in config.tables.values()
        if isinstance(group, dict) and "local" in group and group.get("enabled", True)
    ]
    assert enabled_groups
    for group in enabled_groups:
        assert any(group["local"]["name"] in query for query in created)


def test_db_init_closes_client_when_ddl_fails(monkeypatch):
    client = _FakeClickHouse(fail_on="CREATE TABLE")
    _patch_client(monkeypatch, client)

    with pytest.raises(RuntimeError, match="ddl refused"):
        cli_module.DBCommands().init()

    assert client.closed is True
    assert any("CREATE DATABASE" in query for query in client.commands)
