"""
Table management operations for ClickHouse databases.
"""

import structlog
from routers.tracker.db.clickhouse.connector import ClickHouseConnector
from routers.tracker.db.clickhouse.schemas import get_fields_for_table_group

logger = structlog.get_logger(__name__)


class TableManager:
    """
    Manages ClickHouse table operations.
    """

    def __init__(self, connector: ClickHouseConnector):
        """
        Initialize the table manager.

        Args:
            connector: The ClickHouse connector
        """
        self.connector = connector

    async def create_database(self) -> None:
        """
        Create databases for all tables.
        """
        databases: set[str] = set()

        for group, tables in self.connector.tables.items():
            if not isinstance(tables, dict):
                continue
            databases.add(group)
            for table_info in tables.values():
                if not isinstance(table_info, dict):
                    continue
                if table_info["name"] and "." in table_info["name"]:
                    db = table_info["name"].split(".")[0]
                    databases.add(db)

        for db in databases:
            await self.connector.command(
                f"CREATE DATABASE IF NOT EXISTS {db} "
                f"{self.connector.cluster_condition}",
            )
            logger.info(f"Created database: {db}")

    async def create_local_table(self, table_group: str = "evnt") -> None:
        """
        Create a local table for the specified group.

        Args:
            table_group: The table group
        """
        table_data = self.connector.tables[table_group]["local"]
        fields = get_fields_for_table_group(table_group)
        columns = [field.create_expression for field in fields]

        full_table_name = await self.connector.get_full_table_name(table_data["name"])

        query = (
            f"CREATE TABLE IF NOT EXISTS {full_table_name} "
            f"{self.connector.cluster_condition} ({', '.join(columns)}) "
            f"ENGINE = {table_data['engine']} "
            f"PARTITION BY {table_data['partition_by']} "
            f"ORDER BY ({table_data['order_by']}) "
            f"SAMPLE BY {table_data['sample_by']} "
            f"SETTINGS {table_data['settings']};"
        )

        await self.connector.command(query)
        logger.info(f"Created local table: {full_table_name}")

    async def create_distributed_table(self, table_group: str = "evnt") -> None:
        """
        Create a distributed table for the specified group.

        Args:
            table_group: The table group
        """
        if not self.connector.cluster:
            logger.info(
                "Skipping distributed table creation (no cluster defined)",
            )
            return

        table_data = self.connector.tables[table_group]["distributed"]

        if "." in self.connector.tables[table_group]["local"]["name"]:
            source_db, source_table = self.connector.tables[table_group]["local"][
                "name"
            ].split(".")
        else:
            source_db = self.connector.database
            source_table = self.connector.tables[table_group]["local"]["name"]

        full_table_name = await self.connector.get_full_table_name(table_data["name"])

        query = (
            f"CREATE TABLE IF NOT EXISTS {full_table_name} "
            f"{self.connector.cluster_condition} AS {source_db}.{source_table} "
            f"ENGINE = Distributed('{self.connector.cluster}', '{source_db}', "
            f"'{source_table}', {table_data['sample_by']});"
        )

        await self.connector.command(query)
        logger.info(f"Created distributed table: {full_table_name}")

    async def create_all_tables(self) -> None:
        """
        Create all required tables.
        """
        await self.create_database()

        for table_group in self.connector.tables:
            if not isinstance(self.connector.tables[table_group], dict):
                continue

            if "local" not in self.connector.tables[table_group]:
                continue

            enabled = self.connector.tables[table_group].get("enabled", True)
            if not enabled:
                continue

            await self.create_local_table(table_group)

            if (
                self.connector.cluster
                and "distributed" in self.connector.tables[table_group]
            ):
                await self.create_distributed_table(table_group)
