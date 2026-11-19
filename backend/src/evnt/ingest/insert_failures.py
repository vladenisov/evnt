"""Insert-failure classification and per-message attempt counting.

Holds what the RabbitMQ batch worker needs to decide what an insert failure
means for the rows it carried: the ClickHouse error-code tables,
``InsertFailure`` and ``classify_insert_failure``, and ``_AttemptTracker``,
which counts each message's failed insert attempts across redeliveries.
"""

from __future__ import annotations

import hashlib
import struct
from collections import OrderedDict
from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from aio_pika.abc import AbstractIncomingMessage
from clickhouse_connect.driver.exceptions import DataError
from clickhouse_connect.driver.exceptions import Error as ClickHouseDriverError

# Raised while turning row values into an insert (the connector's coercion or the
# driver's column serializers) when a value does not fit its column.
_VALUE_ERRORS = (TypeError, ValueError, ArithmeticError, struct.error)

# ClickHouse server error codes caused by the values of particular rows: parsing,
# conversion, range and constraint checks, often inside a materialized view. The
# code arrives in the X-ClickHouse-Exception-Code header (``Error.code``); the
# name is parsed from the response body and serves when the code is missing.
_ROW_DATA_ERROR_CODES: Final[Mapping[int, str]] = {
    6: "CANNOT_PARSE_TEXT",
    27: "CANNOT_PARSE_INPUT_ASSERTION_FAILED",
    38: "CANNOT_PARSE_DATE",
    41: "CANNOT_PARSE_DATETIME",
    69: "ARGUMENT_OUT_OF_BOUND",
    70: "CANNOT_CONVERT_TYPE",
    72: "CANNOT_PARSE_NUMBER",
    117: "INCORRECT_DATA",
    128: "TOO_LARGE_ARRAY_SIZE",
    131: "TOO_LARGE_STRING_SIZE",
    190: "SIZES_OF_ARRAYS_DONT_MATCH",
    321: "VALUE_IS_OUT_OF_RANGE_OF_DATA_TYPE",
    349: "CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN",
    395: "FUNCTION_THROW_IF_VALUE_IS_NON_ZERO",
    469: "VIOLATED_CONSTRAINT",
    691: "UNKNOWN_ELEMENT_OF_ENUM",
}

# Server error codes that fail every insert alike, whatever rows it carries: the
# server is overloaded, out of memory or disk, read-only, cut off from Keeper or
# its replicas, refusing the credentials, or the table no longer matches the
# columns the collector sends. Parking messages for any of these would empty the
# queue into the failed queue, so they wait instead.
_SYSTEMIC_ERROR_CODES: Final[Mapping[int, str]] = {
    16: "NO_SUCH_COLUMN_IN_TABLE",
    32: "ATTEMPT_TO_READ_AFTER_EOF",
    33: "CANNOT_READ_ALL_DATA",
    47: "UNKNOWN_IDENTIFIER",
    53: "TYPE_MISMATCH",
    60: "UNKNOWN_TABLE",
    81: "UNKNOWN_DATABASE",
    159: "TIMEOUT_EXCEEDED",
    164: "READONLY",
    173: "CANNOT_ALLOCATE_MEMORY",
    202: "TOO_MANY_SIMULTANEOUS_QUERIES",
    203: "NO_FREE_CONNECTION",
    209: "SOCKET_TIMEOUT",
    210: "NETWORK_ERROR",
    225: "NO_ZOOKEEPER",
    236: "ABORTED",
    241: "MEMORY_LIMIT_EXCEEDED",
    242: "TABLE_IS_READ_ONLY",
    243: "NOT_ENOUGH_SPACE",
    252: "TOO_MANY_PARTS",
    279: "ALL_CONNECTION_TRIES_FAILED",
    285: "TOO_FEW_LIVE_REPLICAS",
    319: "UNKNOWN_STATUS_OF_INSERT",
    394: "QUERY_WAS_CANCELLED",
    425: "SYSTEM_ERROR",
    439: "CANNOT_SCHEDULE_TASK",
    497: "ACCESS_DENIED",
    516: "AUTHENTICATION_FAILED",
    999: "KEEPER_EXCEPTION",
}

# Upper bound on messages whose unexplained failures are being counted. Only
# messages that failed and have not been inserted or parked since are tracked,
# which is at most a few prefetch windows in practice.
_ATTEMPT_TRACKER_MAX_ENTRIES = 10_000


class InsertFailure(StrEnum):
    """What an insert failure says about the rows being inserted."""

    # The rows themselves were refused; retrying cannot help, so they are parked
    # once an insert of the same flush proves ClickHouse takes other rows. Until
    # then the failure counts toward ``max_insert_attempts``.
    DETERMINISTIC = "deterministic"
    # Every insert would fail the same way whatever it holds (ClickHouse
    # unreachable, overloaded or refusing the table); the rows wait in the queue
    # and the failure never counts toward ``max_insert_attempts``.
    SYSTEMIC = "systemic"
    # Neither of the above: retried, and counted toward ``max_insert_attempts``.
    UNKNOWN = "unknown"


def classify_insert_failure(error: BaseException) -> InsertFailure:
    """Classify an exception raised by an insert attempt.

    Errs toward ``UNKNOWN``: a failure is ``DETERMINISTIC`` only when it is known
    to come from row values, and ``SYSTEMIC`` only when it is known to hit every
    insert.
    """

    if isinstance(error, DataError):
        return InsertFailure.DETERMINISTIC
    if isinstance(error, ClickHouseDriverError):
        return _classify_clickhouse_error(error)
    # TimeoutError (the insert deadline) and connection errors are OSErrors.
    if isinstance(error, OSError):
        return InsertFailure.SYSTEMIC
    if isinstance(error, _VALUE_ERRORS):
        return InsertFailure.DETERMINISTIC
    return InsertFailure.UNKNOWN


def _classify_clickhouse_error(error: ClickHouseDriverError) -> InsertFailure:
    if error.code is None and error.name is None:
        # No server exception: the driver lost the connection (OperationalError
        # "Network Error"), a proxy answered with an HTTP error status, or the
        # client refused to run (closed session, column count mismatch).
        return InsertFailure.SYSTEMIC
    if error.code in _ROW_DATA_ERROR_CODES or error.name in _ROW_DATA_ERROR_CODES.values():
        return InsertFailure.DETERMINISTIC
    if error.code in _SYSTEMIC_ERROR_CODES or error.name in _SYSTEMIC_ERROR_CODES.values():
        return InsertFailure.SYSTEMIC
    return InsertFailure.UNKNOWN


class _AttemptTracker:
    """Counts each message's failed insert attempts across redeliveries.

    RabbitMQ counts redeliveries only on quorum queues (``x-delivery-count``);
    the ingest queue is a classic queue, and its arguments cannot change once
    it exists. The count therefore lives in the worker: it restarts from zero
    when the worker does, and each worker counts on its own. A message is
    identified by its ``message_id`` when the publisher set one, else by a hash
    of its body, which a redelivery leaves unchanged.

    The tracker also counts the worker's successful inserts, in any table
    group, and remembers that count at each message's latest failure. A message
    on its last attempt is parked only if some insert succeeded since then:
    during a failure that hits every insert nothing does, so the limit cannot
    empty the queue into the failed queue.
    """

    def __init__(self, limit: int, max_entries: int = _ATTEMPT_TRACKER_MAX_ENTRIES) -> None:
        self.limit = limit
        self._max_entries = max_entries
        # key -> (failures, successful inserts counted at the latest failure)
        self._failures: OrderedDict[str, tuple[int, int]] = OrderedDict()
        self._successes = 0

    def __len__(self) -> int:
        return len(self._failures)

    @staticmethod
    def _key(message: AbstractIncomingMessage) -> str:
        message_id = getattr(message, "message_id", None)
        if message_id:
            return f"id:{message_id}"
        return "body:" + hashlib.blake2b(message.body, digest_size=16).hexdigest()

    def note_insert_success(self) -> None:
        """Count one successful insert of this worker."""

        self._successes += 1

    def failures(self, message: AbstractIncomingMessage) -> int:
        """Return the failed attempts recorded for ``message`` so far."""

        if not self._failures:
            return 0
        entry = self._failures.get(self._key(message))
        return 0 if entry is None else entry[0]

    def is_last_attempt(self, message: AbstractIncomingMessage) -> bool:
        """Return whether one more failed attempt reaches the limit."""

        return self.limit > 0 and self.failures(message) + 1 >= self.limit

    def inserted_since_last_failure(self, message: AbstractIncomingMessage) -> bool:
        """Return whether any insert succeeded after ``message`` last failed."""

        if not self._failures:
            return False
        entry = self._failures.get(self._key(message))
        return entry is not None and self._successes > entry[1]

    def may_park(self, message: AbstractIncomingMessage) -> bool:
        """Return whether a failure now reaches the limit while other inserts succeed."""

        return self.is_last_attempt(message) and self.inserted_since_last_failure(message)

    def record_failure(self, message: AbstractIncomingMessage) -> None:
        """Count one more failed attempt for ``message``."""

        if self.limit <= 0:
            return
        key = self._key(message)
        failures, _ = self._failures.get(key, (0, 0))
        self._failures[key] = (failures + 1, self._successes)
        self._failures.move_to_end(key)
        while len(self._failures) > self._max_entries:
            self._failures.popitem(last=False)

    def forget(self, message: AbstractIncomingMessage) -> None:
        """Drop the count of a message that left the queue."""

        if self._failures:
            self._failures.pop(self._key(message), None)
