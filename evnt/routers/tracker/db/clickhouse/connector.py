"""
ClickHouse database connector for evnt.

This module provides a connection to ClickHouse.
"""

from dataclasses import dataclass
from typing import Any

import structlog
from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.exceptions import ClickHouseError, DatabaseError
from core.constants import CLICKHOUSE_ASYNC_SETTINGS
from core.tracing import async_capture_span
from routers.tracker.db.clickhouse.schemas.snowplow import ColumnDef, TupleColumnDef

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _InsertBatchMetadata:
    """Static ClickHouse insert details for a table group."""

    full_table_name: str
    fields: tuple[ColumnDef | TupleColumnDef, ...]
    column_names: list[str]
    column_types: list[Any]


class ClickHouseConnector:
    """
    ClickHouse database connector.
    """

    def __init__(
        self,
        conn: AsyncClient,
        cluster_name: str | None = None,
        database: str = "evnt",
        insert_settings: dict[str, int] | None = None,
        **params,
    ):
        """
        Initialize the ClickHouse connector.

        Args:
            conn: The ClickHouse connection
            cluster_name: Optional cluster name for distributed tables
            database: Default database name
            **params: Additional parameters
        """
        self.conn = conn
        self.cluster = cluster_name
        self.cluster_condition = self._make_on_cluster(cluster_name)
        self.database = database
        self.params = params
        self.tables = self.params["tables"]
        self.insert_settings = insert_settings or CLICKHOUSE_ASYNC_SETTINGS.copy()
        self._insert_metadata_cache: dict[str, _InsertBatchMetadata] = {}

    @staticmethod
    def _make_on_cluster(cluster_name: str | None = None) -> str:
        """
        Create the ON CLUSTER clause for ClickHouse queries.

        Args:
            cluster_name: The cluster name

        Returns:
            The ON CLUSTER clause or empty string
        """
        if not cluster_name:
            return ""
        return f"ON CLUSTER {cluster_name}"

    async def get_full_table_name(self, table_name: str) -> str:
        """
        Get the fully qualified table name.

        Args:
            table_name: The table name

        Returns:
            The fully qualified table name
        """
        if "." in table_name:
            return table_name
        return f"{self.database}.{table_name}"

    async def command(self, query: str) -> None:
        """
        Execute a ClickHouse command.

        Args:
            query: The query to execute
        """
        try:
            await self.conn.command(query)
        except (ClickHouseError, DatabaseError) as e:
            logger.error("Database command failed", error=str(e), query=query)
            raise

    async def query(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
    ):
        """
        Execute a ClickHouse query.

        Args:
            query: The query to execute
            parameters: Optional query parameters

        Returns:
            The query results
        """
        try:
            return await self.conn.query(query, parameters=parameters)
        except (ClickHouseError, DatabaseError) as e:
            logger.error(
                "Database query failed",
                error=str(e),
                query=query,
                parameters=parameters,
            )
            raise

    async def get_table_name(self, table_group: str = "evnt") -> str:
        """
        Get the table name for a specific group.

        Args:
            table_group: The table group

        Returns:
            The table name
        """
        table_name = self.tables[table_group]["local"]["name"]
        if self.cluster:
            table_name = self.tables[table_group]["distributed"]["name"]
        return table_name

    async def _get_insert_metadata(
        self,
        table_group: str,
    ) -> _InsertBatchMetadata:
        """Resolve and cache static insert metadata for a table group."""

        if table_group in self._insert_metadata_cache:
            return self._insert_metadata_cache[table_group]

        from routers.tracker.db.clickhouse.schemas import get_fields_for_table_group

        table_name = await self.get_table_name(table_group)
        full_table_name = await self.get_full_table_name(table_name)
        fields = tuple(
            field
            for field in get_fields_for_table_group(table_group)
            if isinstance(field, TupleColumnDef) or field.payload_name is not None
        )

        metadata = _InsertBatchMetadata(
            full_table_name=full_table_name,
            fields=fields,
            column_names=[field.name for field in fields],
            column_types=[field.type for field in fields],
        )
        self._insert_metadata_cache[table_group] = metadata
        return metadata

    @async_capture_span()
    async def insert_rows(
        self,
        rows: list[dict[str, Any]],
        table_group: str = "evnt",
    ) -> None:
        """
        Insert rows into the specified table group in a single batch request.

        Args:
            rows: List of rows to insert
            table_group: The table group
        """
        await self.insert_batch(rows, table_group=table_group)

    @staticmethod
    def _sanitize_clickhouse_value(type_name: str, value: Any) -> Any:
        """Coerce `None` into a safe default for non-nullable string columns."""

        if value is not None:
            return value
        if type_name == "String" or type_name.startswith("LowCardinality(String"):
            return ""
        return value

    async def insert_batch(
        self,
        rows: list[dict[str, Any]],
        table_group: str = "evnt",
    ) -> None:
        """Insert a batch of rows in a single ClickHouse request."""

        if not rows:
            logger.debug("No rows to insert")
            return

        metadata = await self._get_insert_metadata(table_group)
        data = []

        for row in rows:
            values = []
            for field in metadata.fields:
                if isinstance(field, TupleColumnDef):
                    value = tuple(
                        self._sanitize_clickhouse_value(
                            v.type_name,
                            row.get(v.payload_name),
                        )
                        for v in field.elements
                        if v.payload_name is not None
                    )
                else:
                    value = self._sanitize_clickhouse_value(
                        field.type_name,
                        row.get(field.payload_name),
                    )
                values.append(value)
            data.append(values)

        async with async_capture_span("clickhouse_query"):
            try:
                await self.conn.insert(
                    metadata.full_table_name,
                    data=data,
                    column_names=metadata.column_names,
                    column_types=metadata.column_types,
                    settings=self.insert_settings,
                )
            except (ClickHouseError, DatabaseError) as e:
                logger.error(
                    "Insert operation failed",
                    error=str(e),
                    table_name=metadata.full_table_name,
                    rows_count=len(rows),
                    column_names=metadata.column_names,
                )
                raise
