"""Regression tests for the ClickHouse-audit fixes.

Covers four behavior-preserving fixes:
- ColumnDef.create_expression now emits a declared DEFAULT expression.
- ClickHouseConnector._sanitize_clickhouse_value is type-aware for UUID.
- ClickHouseConfiguration validates database/cluster_name as SQL identifiers.
- TableManager.create_database tolerates non-dict table-group values.
"""

from uuid import UUID

import pytest
from core.config import ClickHouseConfiguration
from routers.tracker.db.clickhouse.connector import ClickHouseConnector
from routers.tracker.db.clickhouse.schemas.snowplow import (
    JSON,
    STRING,
    ColumnDef,
    snowplow_fields,
)
from routers.tracker.db.clickhouse.table_manager import TableManager


class _RecordingConnector:
    """Minimal ClickHouseConnector stand-in capturing executed commands."""

    def __init__(self, tables: dict) -> None:
        self.tables = tables
        self.cluster_condition = ""
        self.cluster = None
        self.commands: list[str] = []

    async def command(self, query: str) -> None:
        self.commands.append(query)


def test_create_expression_emits_quoted_json_default():
    # A column that declares only default_expression still gets DEFAULT in DDL.
    col = ColumnDef(
        payload_name="x",
        name="extra",
        type=JSON,
        default_expression="'{}'",
    )
    expr = col.create_expression
    assert expr == "`extra` JSON DEFAULT '{}'"


def test_snowplow_json_defaults_are_quoted():
    expressions = {field.name: field.create_expression for field in snowplow_fields}

    for field_name in ("amp", "user_data", "geolocation", "extra"):
        assert expressions[field_name].endswith("JSON DEFAULT '{}'")


def test_create_expression_respects_explicit_default_type():
    col = ColumnDef(
        payload_name=None,
        name="app",
        type=STRING,
        default_type="MATERIALIZED",
        default_expression="'evnt'",
    )
    assert "MATERIALIZED 'evnt'" in col.create_expression


def test_create_expression_no_default_when_absent():
    col = ColumnDef(payload_name=None, name="plain", type=STRING)
    assert col.create_expression == "`plain` String"


def test_sanitize_uuid_none_becomes_zero_uuid():
    assert ClickHouseConnector._sanitize_clickhouse_value("UUID", None) == UUID(int=0)


def test_sanitize_string_none_becomes_empty():
    assert ClickHouseConnector._sanitize_clickhouse_value("String", None) == ""
    assert (
        ClickHouseConnector._sanitize_clickhouse_value("LowCardinality(String)", None)
        == ""
    )


def test_sanitize_preserves_non_none_and_other_types():
    existing = UUID(int=5)
    assert ClickHouseConnector._sanitize_clickhouse_value("UUID", existing) is existing
    # Types without an explicit zero are left as None (unchanged behavior).
    assert ClickHouseConnector._sanitize_clickhouse_value("UInt64", None) is None


@pytest.mark.parametrize("bad", ["evil'; DROP", "db name", "a-b", "x;y"])
def test_configuration_rejects_non_identifier(bad):
    with pytest.raises(ValueError):
        ClickHouseConfiguration(cluster_name=bad)
    with pytest.raises(ValueError):
        ClickHouseConfiguration(database=bad)


def test_configuration_accepts_valid_identifiers_and_empty_cluster():
    cfg = ClickHouseConfiguration(database="my_db", cluster_name="")
    assert cfg.database == "my_db"
    assert cfg.cluster_name == ""
    assert ClickHouseConfiguration(cluster_name="prod_cluster").cluster_name == (
        "prod_cluster"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"], indirect=True)
async def test_create_database_tolerates_non_dict_group(anyio_backend):
    # A malformed non-dict group value must be skipped, not crash with
    # AttributeError ('str' object has no attribute 'values').
    connector = _RecordingConnector(
        tables={
            "evnt": {"local": {"name": "evnt.local"}},
            "broken": "not-a-dict",
        },
    )
    await TableManager(connector).create_database()

    created = " ".join(connector.commands)
    assert "CREATE DATABASE IF NOT EXISTS evnt" in created
    # The "broken" scalar group is skipped, so its value never reaches DDL.
    assert "not-a-dict" not in created
