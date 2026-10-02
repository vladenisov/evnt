"""
Custom exception classes for evnt.
"""

from typing import Any


class SimpleSnowplowError(Exception):
    """Base exception for all evnt errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class DatabaseError(SimpleSnowplowError):
    """Base exception for database-related errors."""


class DatabaseConnectionError(DatabaseError):
    """Raised when database connection fails.

    Named with a ``Database`` prefix to avoid shadowing the built-in
    ``ConnectionError``.
    """
