"""
Protocol definitions for evnt.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RowSink(Protocol):
    """Protocol for event sinks used by request handlers."""

    async def insert_rows(
        self,
        rows: list[dict[str, Any]],
        table_group: str = "evnt",
    ) -> None:
        """Insert rows into the sink."""
        ...

    async def get_table_name(self, table_group: str = "evnt") -> str:
        """Get the target table name for a specific group."""
        ...


@runtime_checkable
class DatabaseConnector(RowSink, Protocol):
    """Protocol for database connectors."""

    async def command(self, query: str) -> None:
        """Execute a database command."""
        ...

    async def query(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a database query and return results."""
        ...


@runtime_checkable
class HealthChecker(Protocol):
    """Protocol for health check implementations."""

    async def check(self) -> dict[str, bool]:
        """Perform health check and return status."""
        ...
