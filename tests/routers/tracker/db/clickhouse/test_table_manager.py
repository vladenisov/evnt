"""Characterization tests for the ClickHouse ``TableManager`` DDL generation.

These exercise ``create_database`` / ``create_local_table`` /
``create_distributed_table`` / ``create_all_tables`` against a *fake* connector
that records the SQL strings passed to ``.command()`` so no real ClickHouse is
needed. The assertions pin the exact DDL shape (ON CLUSTER handling, IF NOT
EXISTS, column expressions, database dedup, distributed source resolution).
"""

import pytest
from routers.tracker.db.clickhouse.schemas import register_fields
from routers.tracker.db.clickhouse.schemas.snowplow import STRING, ColumnDef
from routers.tracker.db.clickhouse.table_manager import TableManager


class _FakeConnector:
    """Records SQL strings handed to ``.command()`` without touching a DB."""

    def __init__(self, cluster=None, database="evnt", tables=None):
        self.cluster = cluster
        self.cluster_condition = f"ON CLUSTER {cluster}" if cluster else ""
        self.database = database
        self.tables = tables or {}
        self.commands: list[str] = []

    async def command(self, query: str) -> None:
        self.commands.append(query)

    async def get_full_table_name(self, table_name: str) -> str:
        if "." in table_name:
            return table_name
        return f"{self.database}.{table_name}"


def _local_table(name="events_local"):
    return {
        "name": name,
        "engine": "MergeTree()",
        "partition_by": "toYYYYMM(time)",
        "order_by": "time",
        "sample_by": "eid",
        "settings": "index_granularity=8192",
    }


@pytest.fixture(autouse=True)
def _register_two_string_fields():
    """Give the ``tm_test`` group a tiny, deterministic schema."""

    register_fields(
        "tm_test",
        [
            ColumnDef(payload_name="foo", name="foo", type=STRING),
            ColumnDef(payload_name="bar", name="bar", type=STRING),
        ],
    )


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_create_database_emits_if_not_exists_with_on_cluster(anyio_backend):
    connector = _FakeConnector(
        cluster="prod_cluster",
        tables={"evnt": {"local": {"name": "events_local"}}},
    )
    manager = TableManager(connector)

    await manager.create_database()

    assert connector.commands == [
        "CREATE DATABASE IF NOT EXISTS evnt ON CLUSTER prod_cluster",
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_create_database_without_cluster_has_no_on_cluster_clause(anyio_backend):
    connector = _FakeConnector(
        cluster=None,
        tables={"evnt": {"local": {"name": "events_local"}}},
    )
    manager = TableManager(connector)

    await manager.create_database()

    # cluster_condition is empty, leaving a trailing space the code appends.
    assert connector.commands == ["CREATE DATABASE IF NOT EXISTS evnt "]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_create_database_collects_qualified_table_dbs_and_dedups(anyio_backend):
    """Group key + every ``db.table`` qualified name produce one CREATE each."""

    connector = _FakeConnector(
        cluster=None,
        tables={
            "evnt": {
                "local": {"name": "analytics.events_local"},
                "distributed": {"name": "analytics.events"},
                # A non-dict per-table value is skipped by the inner guard.
                "comment": "not-a-dict",
            },
        },
    )
    manager = TableManager(connector)

    await manager.create_database()

    created_dbs = sorted(
        cmd.split("IF NOT EXISTS ")[1].split(" ")[0] for cmd in connector.commands
    )
    # "evnt" (group key) and "analytics" (from the qualified table names), deduped.
    assert created_dbs == ["analytics", "evnt"]
    assert len(connector.commands) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_create_local_table_emits_full_ddl_with_columns(anyio_backend):
    connector = _FakeConnector(
        cluster="prod_cluster",
        database="evnt",
        tables={"tm_test": {"local": _local_table()}},
    )
    manager = TableManager(connector)

    await manager.create_local_table("tm_test")

    assert len(connector.commands) == 1
    ddl = connector.commands[0]
    assert ddl.startswith(
        "CREATE TABLE IF NOT EXISTS evnt.events_local ON CLUSTER prod_cluster ("
    )
    assert "`foo` String, `bar` String" in ddl
    assert "ENGINE = MergeTree()" in ddl
    assert "PARTITION BY toYYYYMM(time)" in ddl
    assert "ORDER BY (time)" in ddl
    assert "SAMPLE BY eid" in ddl
    assert ddl.endswith("SETTINGS index_granularity=8192;")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_create_local_table_qualified_name_is_not_reprefixed(anyio_backend):
    connector = _FakeConnector(
        cluster=None,
        database="evnt",
        tables={"tm_test": {"local": _local_table(name="other_db.events_local")}},
    )
    manager = TableManager(connector)

    await manager.create_local_table("tm_test")

    ddl = connector.commands[0]
    # Already-qualified name kept as-is; no ON CLUSTER (no cluster), so just a space.
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS other_db.events_local  (")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_create_distributed_table_uses_local_as_source(anyio_backend):
    connector = _FakeConnector(
        cluster="prod_cluster",
        database="evnt",
        tables={
            "tm_test": {
                "local": _local_table(),
                "distributed": {"name": "events", "sample_by": "rand()"},
            },
        },
    )
    manager = TableManager(connector)

    await manager.create_distributed_table("tm_test")

    assert connector.commands == [
        "CREATE TABLE IF NOT EXISTS evnt.events ON CLUSTER prod_cluster "
        "AS evnt.events_local "
        "ENGINE = Distributed('prod_cluster', 'evnt', 'events_local', rand());",
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_create_distributed_table_splits_qualified_local_source(anyio_backend):
    connector = _FakeConnector(
        cluster="prod_cluster",
        database="evnt",
        tables={
            "tm_test": {
                "local": _local_table(name="raw.events_local"),
                "distributed": {"name": "events", "sample_by": "rand()"},
            },
        },
    )
    manager = TableManager(connector)

    await manager.create_distributed_table("tm_test")

    ddl = connector.commands[0]
    assert "AS raw.events_local" in ddl
    assert "Distributed('prod_cluster', 'raw', 'events_local', rand())" in ddl


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_create_distributed_table_noop_without_cluster(anyio_backend):
    connector = _FakeConnector(
        cluster=None,
        tables={
            "tm_test": {
                "local": _local_table(),
                "distributed": {"name": "events", "sample_by": "rand()"},
            },
        },
    )
    manager = TableManager(connector)

    await manager.create_distributed_table("tm_test")

    assert connector.commands == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_create_all_tables_creates_db_then_local_and_distributed(anyio_backend):
    connector = _FakeConnector(
        cluster="prod_cluster",
        database="evnt",
        tables={
            "tm_test": {
                "local": _local_table(),
                "distributed": {"name": "events", "sample_by": "rand()"},
            },
        },
    )
    manager = TableManager(connector)

    await manager.create_all_tables()

    kinds = [cmd.split(" IF NOT EXISTS")[0] for cmd in connector.commands]
    assert kinds == ["CREATE DATABASE", "CREATE TABLE", "CREATE TABLE"]
    assert "AS evnt.events_local" in connector.commands[2]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_create_all_tables_skips_disabled_and_no_local(anyio_backend):
    connector = _FakeConnector(
        cluster=None,
        database="evnt",
        tables={
            "disabled_group": {"local": _local_table(), "enabled": False},
            "no_local_group": {"distributed": {"name": "x", "sample_by": "rand()"}},
            "tm_test": {"local": _local_table()},
        },
    )
    manager = TableManager(connector)

    await manager.create_all_tables()

    create_tables = [c for c in connector.commands if c.startswith("CREATE TABLE")]
    # Only the enabled ``tm_test`` group with a "local" table produces a table.
    assert len(create_tables) == 1
    assert "evnt.events_local" in create_tables[0]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_create_all_tables_without_cluster_skips_distributed(anyio_backend):
    connector = _FakeConnector(
        cluster=None,
        database="evnt",
        tables={
            "tm_test": {
                "local": _local_table(),
                "distributed": {"name": "events", "sample_by": "rand()"},
            },
        },
    )
    manager = TableManager(connector)

    await manager.create_all_tables()

    create_tables = [c for c in connector.commands if c.startswith("CREATE TABLE")]
    assert len(create_tables) == 1
    assert "Distributed(" not in create_tables[0]
