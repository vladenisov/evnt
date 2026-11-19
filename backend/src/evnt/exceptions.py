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


class IngestUnavailableError(SimpleSnowplowError):
    """The ingest backend cannot take events right now; the client should retry.

    Raised instead of waiting indefinitely, so the route answers 503 with
    ``Retry-After`` and trackers resend the batch later.
    """

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        retry_after_seconds: int = 1,
    ):
        super().__init__(message, details)
        self.retry_after_seconds = retry_after_seconds


class DatabaseConnectionError(DatabaseError):
    """Raised when database connection fails.

    Named with a ``Database`` prefix to avoid shadowing the built-in
    ``ConnectionError``.
    """
