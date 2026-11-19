"""Poison-message handling in the RabbitMQ batch worker.

Covers how insert failures are classified, that rows refused for a
deterministic reason are parked like the driver's DataError, that outages are
requeued without limit, and that ``max_insert_attempts`` bounds the retries of
an unexplained failure, or of bad rows no insert of their flush can vouch for,
only while other inserts succeed.
"""

import struct

import pytest
from clickhouse_connect.driver.exceptions import (
    DatabaseError,
    DataError,
    OperationalError,
    ProgrammingError,
)
from pydantic import ValidationError

from evnt.config import RabbitMQConfig
from evnt.constants import DEFAULT_RABBITMQ_MAX_INSERT_ATTEMPTS
from evnt.ingest import insert_failures as insert_failures_module
from evnt.ingest import worker as worker_module
from evnt.ingest.insert_failures import (
    InsertFailure,
    _AttemptTracker,
    classify_insert_failure,
)
from evnt.ingest.rabbitmq import QueuedInsertPayload
from evnt.ingest.worker import RabbitMQBatchWorker


class _Exchange:
    def __init__(self, fail=False):
        self.fail = fail
        self.published = []

    async def publish(self, message, routing_key, mandatory=True):
        if self.fail:
            raise RuntimeError("exchange down")
        self.published.append({"message": message, "routing_key": routing_key})


class _Channel:
    def __init__(self, exchange=None):
        self.default_exchange = exchange or _Exchange()


class _Message:
    def __init__(self, ids, *, message_id=None, headers=None, table_group="evnt"):
        payload = QueuedInsertPayload(table_group=table_group, rows=[{"id": i} for i in ids])
        self.body = payload.model_dump_json().encode("utf-8")
        self.message_id = message_id
        self.headers = headers or {}
        self.content_type = "application/json"
        self.type = "evnt.insert"
        self.acked = False
        self.requeued = False

    async def ack(self):
        self.acked = True

    async def nack(self, requeue=True):
        self.requeued = requeue

    async def reject(self, requeue=False):
        raise AssertionError("reject is only for undecodable payloads")


class _FailingRowsSink:
    """Fails any insert holding a row in ``bad_ids`` with ``error``; fails all with ``always``."""

    def __init__(self, error=None, bad_ids=(), always=None):
        self.error = error
        self.bad_ids = set(bad_ids)
        self.always = always
        self.attempts = 0
        self.inserted = []

    async def insert_batch(self, rows, table_group="evnt"):
        self.attempts += 1
        if self.always is not None:
            raise self.always
        if any(row["id"] in self.bad_ids for row in rows):
            raise self.error
        self.inserted.extend(row["id"] for row in rows)


class _UnknownTableGroupSink(_FailingRowsSink):
    """Inserts ``evnt`` rows and fails any other table group the way the schema registry does."""

    async def insert_batch(self, rows, table_group="evnt"):
        if table_group != "evnt":
            self.attempts += 1
            raise KeyError(table_group)
        await super().insert_batch(rows, table_group=table_group)


class _OutageAfterFirstInsertSink(_FailingRowsSink):
    """Refuses ``bad_ids`` until one insert succeeds, then fails every insert with ``outage``."""

    def __init__(self, error, bad_ids, outage):
        super().__init__(error, bad_ids)
        self.outage = outage

    async def insert_batch(self, rows, table_group="evnt"):
        await super().insert_batch(rows, table_group=table_group)
        self.always = self.outage


def _worker(sink, *, exchange=None, **overrides):
    config = RabbitMQConfig(
        **{"batch_size": 1000, "batch_timeout_ms": 1000, "retry_delay_ms": 1, **overrides},
    )
    return RabbitMQBatchWorker(
        connection=object(),
        channel=_Channel(exchange),
        queue=object(),
        sink=sink,
        config=config,
    )


async def _deliver(worker, messages):
    for message in messages:
        await worker.add_message(message)
    await worker.flush_all()


def _published(worker):
    return worker.channel.default_exchange.published


def _parked_ids(entry):
    return [
        row["id"] for row in QueuedInsertPayload.model_validate_json(entry["message"].body).rows
    ]


# --- classification -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        DataError("bad value"),
        TypeError("unsupported operand"),
        ValueError("badly formed hexadecimal UUID string"),
        OverflowError("int too big to convert"),
        struct.error("argument out of range"),
        UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed"),
        DatabaseError("Code: 6. Cannot parse text", code=6, name="CANNOT_PARSE_TEXT"),
        DatabaseError("Code: 469. Constraint violated", code=469),
        DatabaseError("Constraint violated", name="VIOLATED_CONSTRAINT"),
        OperationalError("Code: 27 after retry", code=27),
    ],
    ids=repr,
)
def test_row_failures_are_deterministic(error):
    assert classify_insert_failure(error) is InsertFailure.DETERMINISTIC


def test_pydantic_validation_error_is_deterministic():
    with pytest.raises(ValidationError) as caught:
        QueuedInsertPayload.model_validate({"rows": "not a list"})

    assert classify_insert_failure(caught.value) is InsertFailure.DETERMINISTIC


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ConnectionRefusedError(111, "Connection refused"),
        ConnectionResetError(),
        OSError("Network is unreachable"),
        OperationalError("Network Error: Cannot connect to host clickhouse:8123"),
        DatabaseError("HTTP driver received HTTP status 502"),
        ProgrammingError("Client session is unavailable; the client may have been closed."),
        DatabaseError("Code: 252. Too many parts", code=252),
        DatabaseError("Code: 241. Memory limit exceeded", code=241),
        DatabaseError("Code: 164. Readonly", code=164),
        DatabaseError("Code: 242. Table is in readonly mode", code=242),
        DatabaseError("Code: 999. Keeper exception", code=999),
        DatabaseError("Code: 516. Authentication failed", code=516),
        DatabaseError("Code: 16. No such column", code=16),
        DatabaseError("Code: 53. Type mismatch", code=53),
        DatabaseError("Code: 60. Unknown table", code=60),
        DatabaseError("Too many parts", name="TOO_MANY_PARTS"),
        OperationalError("Code: 159 after retry", code=159),
    ],
    ids=repr,
)
def test_outages_and_schema_drift_are_systemic(error):
    assert classify_insert_failure(error) is InsertFailure.SYSTEMIC


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("boom"),
        KeyError("column"),
        AttributeError("'int' object has no attribute 'encode'"),
        DatabaseError("Code: 36. Bad arguments", code=36),
        DatabaseError("Code: 1234. Something new", code=1234),
        DatabaseError("Something new", name="SOMETHING_NEW"),
    ],
    ids=repr,
)
def test_unexplained_failures_are_unknown(error):
    assert classify_insert_failure(error) is InsertFailure.UNKNOWN


def test_error_code_tables_do_not_overlap():
    row_codes = insert_failures_module._ROW_DATA_ERROR_CODES
    systemic_codes = insert_failures_module._SYSTEMIC_ERROR_CODES

    assert not set(row_codes) & set(systemic_codes)
    assert not set(row_codes.values()) & set(systemic_codes.values())


# --- attempt tracker --------------------------------------------------------------------------


def test_tracker_counts_redeliveries_by_body():
    tracker = _AttemptTracker(limit=3)
    first, redelivered, other = _Message([1]), _Message([1]), _Message([2])

    tracker.record_failure(first)
    tracker.record_failure(redelivered)

    assert tracker.failures(first) == 2
    assert tracker.failures(other) == 0
    assert tracker.is_last_attempt(first)
    assert not tracker.is_last_attempt(other)

    tracker.forget(redelivered)
    assert tracker.failures(first) == 0
    assert len(tracker) == 0


def test_tracker_prefers_message_id_over_body():
    tracker = _AttemptTracker(limit=5)

    tracker.record_failure(_Message([1], message_id="m-1"))

    assert tracker.failures(_Message([2], message_id="m-1")) == 1
    assert tracker.failures(_Message([1])) == 0


def test_tracker_is_bounded_and_evicts_the_oldest():
    tracker = _AttemptTracker(limit=5, max_entries=3)
    messages = [_Message([i]) for i in range(5)]

    for message in messages:
        tracker.record_failure(message)

    assert len(tracker) == 3
    assert tracker.failures(messages[0]) == 0
    assert tracker.failures(messages[4]) == 1


def test_tracker_with_limit_zero_keeps_nothing():
    tracker = _AttemptTracker(limit=0)
    message = _Message([1])

    for _ in range(5):
        tracker.record_failure(message)

    assert len(tracker) == 0
    assert not tracker.is_last_attempt(message)


def test_max_insert_attempts_setting():
    assert RabbitMQConfig().max_insert_attempts == DEFAULT_RABBITMQ_MAX_INSERT_ATTEMPTS
    assert RabbitMQConfig(max_insert_attempts=0).max_insert_attempts == 0
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        RabbitMQConfig(max_insert_attempts=-1)


# --- deterministic failures -------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        TypeError("unsupported type for UUID column"),
        DatabaseError("Code: 469. Constraint violated", code=469, name="VIOLATED_CONSTRAINT"),
    ],
    ids=["serialization", "server_row_error"],
)
async def test_deterministic_failure_parks_only_the_bad_message(anyio_backend, error):
    sink = _FailingRowsSink(error, bad_ids={2})
    worker = _worker(sink)
    messages = [_Message([1]), _Message([2]), _Message([3])]

    await _deliver(worker, messages)

    assert sorted(sink.inserted) == [1, 3]
    assert all(message.acked for message in messages)
    assert not any(message.requeued for message in messages)
    published = _published(worker)
    assert len(published) == 1
    assert published[0]["routing_key"] == "evnt.ingest.failed"
    assert published[0]["message"].body == messages[1].body
    headers = published[0]["message"].headers
    assert headers["x-evnt-failure"] == "deterministic"
    assert headers["x-evnt-error-type"] == type(error).__name__
    assert headers["x-evnt-source-queue"] == "evnt.ingest"
    assert headers["x-evnt-table-group"] == "evnt"
    assert headers["x-evnt-error"] == str(error)
    assert "x-evnt-insert-attempts" not in headers
    if isinstance(error, DatabaseError):
        assert headers["x-evnt-error-code"] == "469"
    else:
        assert "x-evnt-error-code" not in headers
    assert "evnt" not in worker.failure_counts


@pytest.mark.anyio
async def test_deterministic_failure_parks_only_the_bad_row_of_a_merged_message(anyio_backend):
    sink = _FailingRowsSink(ValueError("badly formed hexadecimal UUID string"), bad_ids={5})
    worker = _worker(sink)
    message = _Message(range(1, 9), headers={"x-origin": "kept"})

    await _deliver(worker, [message])

    assert sorted(sink.inserted) == [1, 2, 3, 4, 6, 7, 8]
    assert message.acked is True
    assert message.requeued is False
    published = _published(worker)
    assert len(published) == 1
    assert _parked_ids(published[0]) == [5]
    headers = published[0]["message"].headers
    assert headers["x-evnt-failure"] == "deterministic"
    assert headers["x-evnt-error-type"] == "ValueError"
    assert headers["x-origin"] == "kept"


@pytest.mark.anyio
async def test_deterministic_failure_of_every_insert_is_not_parked(anyio_backend):
    # A failure that no insert escapes (a code bug, a schema change) is the
    # table's or the code's, not the rows'.
    sink = _FailingRowsSink(always=TypeError("every row fails"))
    worker = _worker(sink)
    messages = [_Message([1]), _Message([2]), _Message([3])]

    await _deliver(worker, messages)

    assert _published(worker) == []
    assert all(message.requeued for message in messages)
    assert not any(message.acked for message in messages)
    assert worker.failure_counts["evnt"] == 1


@pytest.mark.anyio
async def test_failure_of_every_insert_stops_bisecting_early(anyio_backend):
    sink = _FailingRowsSink(always=TypeError("every row fails"))
    worker = _worker(sink)
    messages = [_Message([i]) for i in range(500)]

    await _deliver(worker, messages)

    assert all(message.requeued for message in messages)
    # A full bisection would cost 999 inserts.
    assert sink.attempts < 3 * worker_module._MAX_FAILED_INSERTS_WITHOUT_SUCCESS


@pytest.mark.anyio
async def test_lone_bad_message_waits_for_a_successful_insert_to_be_parked(anyio_backend):
    sink = _FailingRowsSink(TypeError("bad value"), bad_ids={1})
    worker = _worker(sink)

    alone = _Message([1])
    await _deliver(worker, [alone])
    assert alone.requeued is True
    assert _published(worker) == []

    redelivered, good = _Message([1]), _Message([2])
    await _deliver(worker, [redelivered, good])
    assert redelivered.acked is True
    assert good.acked is True
    assert sink.inserted == [2]
    assert len(_published(worker)) == 1


@pytest.mark.anyio
async def test_driver_data_error_alone_is_still_parked_at_once(anyio_backend):
    sink = _FailingRowsSink(DataError("bad value"), bad_ids={1})
    worker = _worker(sink)
    message = _Message([1])

    await _deliver(worker, [message])

    assert message.acked is True
    assert len(_published(worker)) == 1


# --- transient failures and the attempt limit -------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        OperationalError("Network Error"),
        TimeoutError(),
        DatabaseError("Code: 252. Too many parts", code=252),
        RuntimeError("unexplained"),
    ],
    ids=repr,
)
async def test_transient_failure_requeues_the_batch(anyio_backend, error):
    sink = _FailingRowsSink(always=error)
    worker = _worker(sink)
    messages = [_Message([1]), _Message([2])]

    await _deliver(worker, messages)

    assert sink.attempts == 1
    assert all(message.requeued for message in messages)
    assert not any(message.acked for message in messages)
    assert _published(worker) == []
    assert worker.failure_counts["evnt"] == 1


@pytest.mark.anyio
async def test_unexplained_failure_parks_after_max_insert_attempts(anyio_backend):
    sink = _FailingRowsSink(RuntimeError("unexplained"), bad_ids={1})
    worker = _worker(sink, max_insert_attempts=3)

    for _ in range(2):
        batch = [_Message([1]), _Message([2])]
        await _deliver(worker, batch)
        assert all(message.requeued for message in batch)
        assert _published(worker) == []
    assert sink.inserted == []

    bad, good = _Message([1]), _Message([2])
    await _deliver(worker, [bad, good])

    assert sink.inserted == [2]
    assert bad.acked is True
    assert good.acked is True
    published = _published(worker)
    assert len(published) == 1
    assert published[0]["message"].body == bad.body
    headers = published[0]["message"].headers
    assert headers["x-evnt-failure"] == "max_insert_attempts"
    assert headers["x-evnt-insert-attempts"] == "3"
    assert headers["x-evnt-error-type"] == "RuntimeError"
    assert headers["x-evnt-error"] == "unexplained"
    assert len(worker._attempts) == 0
    assert "evnt" not in worker.failure_counts


@pytest.mark.anyio
async def test_attempt_limit_parks_only_the_failing_row_of_a_merged_message(anyio_backend):
    sink = _FailingRowsSink(RuntimeError("unexplained"), bad_ids={3})
    worker = _worker(sink, max_insert_attempts=2)

    first = _Message(range(1, 5))
    await _deliver(worker, [first])
    assert first.requeued is True

    redelivered = _Message(range(1, 5))
    await _deliver(worker, [redelivered])

    assert sorted(sink.inserted) == [1, 2, 4]
    assert redelivered.acked is True
    published = _published(worker)
    assert len(published) == 1
    assert _parked_ids(published[0]) == [3]
    assert published[0]["message"].headers["x-evnt-failure"] == "max_insert_attempts"


@pytest.mark.anyio
async def test_attempt_limit_does_not_park_while_every_insert_fails(anyio_backend):
    sink = _FailingRowsSink(always=RuntimeError("unexplained"))
    worker = _worker(sink, max_insert_attempts=2)

    for _ in range(5):
        batch = [_Message([1]), _Message([2])]
        await _deliver(worker, batch)
        assert all(message.requeued for message in batch)

    assert _published(worker) == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        OperationalError("Network Error: Connection refused"),
        ConnectionRefusedError(111, "Connection refused"),
        TimeoutError(),
        DatabaseError("Code: 241. Memory limit exceeded", code=241),
    ],
    ids=repr,
)
async def test_outage_never_counts_toward_the_attempt_limit(anyio_backend, error):
    sink = _FailingRowsSink(always=error)
    worker = _worker(sink, max_insert_attempts=2)

    for _ in range(6):
        batch = [_Message([1]), _Message([2])]
        await _deliver(worker, batch)
        assert all(message.requeued for message in batch)

    assert _published(worker) == []
    assert len(worker._attempts) == 0
    # Every round is a single whole-batch attempt: outages are never bisected.
    assert sink.attempts == 6


@pytest.mark.anyio
async def test_outage_between_failures_neither_counts_nor_resets(anyio_backend):
    sink = _FailingRowsSink(RuntimeError("unexplained"), bad_ids={1})
    worker = _worker(sink, max_insert_attempts=2)

    await _deliver(worker, [_Message([1]), _Message([2])])
    sink.always = OperationalError("Network Error")
    await _deliver(worker, [_Message([1]), _Message([2])])
    assert worker._attempts.failures(_Message([1])) == 1

    sink.always = None
    bad, good = _Message([1]), _Message([2])
    await _deliver(worker, [bad, good])

    assert bad.acked is True
    assert good.acked is True
    assert _published(worker)[0]["message"].headers["x-evnt-insert-attempts"] == "2"


@pytest.mark.anyio
async def test_max_insert_attempts_zero_retries_forever(anyio_backend):
    sink = _FailingRowsSink(RuntimeError("unexplained"), bad_ids={1})
    worker = _worker(sink, max_insert_attempts=0)

    for _ in range(25):
        batch = [_Message([1]), _Message([2])]
        await _deliver(worker, batch)
        assert all(message.requeued for message in batch)

    assert _published(worker) == []
    assert len(worker._attempts) == 0


@pytest.mark.anyio
async def test_parking_failure_requeues_and_keeps_counting(anyio_backend):
    sink = _FailingRowsSink(RuntimeError("unexplained"), bad_ids={1})
    worker = _worker(sink, exchange=_Exchange(fail=True), max_insert_attempts=1)
    bad, good = _Message([1]), _Message([2])

    await _deliver(worker, [bad, good])

    assert good.acked is True
    assert bad.requeued is True
    assert bad.acked is False
    assert worker._attempts.failures(_Message([1])) == 1
    assert worker.failure_counts["evnt"] == 1


# --- bisection order and proof across flushes -------------------------------------------------


def test_tracker_proof_needs_an_insert_after_the_latest_failure():
    tracker = _AttemptTracker(limit=2)
    message = _Message([1])

    assert not tracker.may_park(message)
    tracker.note_insert_success()
    # A success before the first failure proves nothing about the failures after it.
    tracker.record_failure(message)
    assert tracker.is_last_attempt(message)
    assert not tracker.inserted_since_last_failure(message)
    assert not tracker.may_park(message)

    tracker.note_insert_success()
    assert tracker.may_park(message)

    tracker.record_failure(message)
    assert not tracker.may_park(message)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        ValueError("badly formed hexadecimal UUID string"),
        DataError("bad value"),
        DatabaseError("Code: 469. Constraint violated", code=469),
    ],
    ids=repr,
)
@pytest.mark.parametrize("bad_rows", [2, 5, 100], ids=lambda n: f"{n}_bad_rows")
async def test_poison_redelivered_at_the_head_of_a_full_batch_is_parked_at_once(
    anyio_backend, error, bad_rows
):
    # A requeued message returns to the head of a classic queue. Bisecting must
    # reach a successful insert before the no-success cap however many of its
    # rows are bad, or the message is requeued on every round.
    sink = _FailingRowsSink(error, bad_ids=range(bad_rows))
    worker = _worker(sink, batch_size=RabbitMQConfig().batch_size)
    poison = _Message(range(100))
    good = [_Message([1000 + i]) for i in range(400)]

    await _deliver(worker, [poison, *good])

    assert poison.acked is True
    assert poison.requeued is False
    assert all(message.acked for message in good)
    published = _published(worker)
    assert len(published) == 1
    assert _parked_ids(published[0]) == list(range(bad_rows))
    assert published[0]["message"].headers["x-evnt-failure"] == "deterministic"
    assert sorted(sink.inserted) == [*range(bad_rows, 100), *range(1000, 1400)]
    assert "evnt" not in worker.failure_counts


@pytest.mark.anyio
async def test_unexplained_poison_at_the_head_of_a_full_batch_parks_at_the_limit(anyio_backend):
    sink = _FailingRowsSink(RuntimeError("unexplained"), bad_ids={0, 1})
    worker = _worker(sink, batch_size=RabbitMQConfig().batch_size)
    limit = worker.config.max_insert_attempts

    for attempt in range(1, limit + 1):
        poison = _Message(range(100))
        good = [_Message([1000 * attempt + i]) for i in range(400)]
        await _deliver(worker, [poison, *good])
        if attempt < limit:
            # An unexplained failure is not bisected before the limit, so the
            # batch it sits in waits with it.
            assert poison.requeued is True
            assert all(message.requeued for message in good)
            assert _published(worker) == []
    assert all(message.acked for message in good)

    assert poison.acked is True
    published = _published(worker)
    assert len(published) == 1
    assert _parked_ids(published[0]) == [0, 1]
    assert published[0]["message"].headers["x-evnt-insert-attempts"] == str(limit)


@pytest.mark.anyio
async def test_lone_message_with_every_row_bad_parks_at_the_limit(anyio_backend):
    # No insert of its own flush can succeed, so only the attempt limit, with
    # inserts succeeding in other flushes, can move it aside.
    sink = _FailingRowsSink(TypeError("bad value"), bad_ids=range(100))
    worker = _worker(sink, max_insert_attempts=3)

    for attempt in range(1, 4):
        poison = _Message(range(100))
        await _deliver(worker, [poison])
        if attempt < 3:
            assert poison.requeued is True
            assert _published(worker) == []
            assert worker._attempts.failures(poison) == attempt
        await _deliver(worker, [_Message([1000 + attempt])])

    assert poison.acked is True
    published = _published(worker)
    assert len(published) == 1
    assert _parked_ids(published[0]) == list(range(100))
    headers = published[0]["message"].headers
    assert headers["x-evnt-failure"] == "max_insert_attempts"
    assert headers["x-evnt-insert-attempts"] == "3"
    assert headers["x-evnt-error-type"] == "TypeError"
    assert len(worker._attempts) == 0


@pytest.mark.anyio
async def test_lone_bad_message_is_not_parked_at_the_limit_while_nothing_else_inserts(
    anyio_backend,
):
    sink = _FailingRowsSink(TypeError("bad value"), bad_ids=range(100))
    worker = _worker(sink, max_insert_attempts=2)

    for _ in range(6):
        poison = _Message(range(100))
        await _deliver(worker, [poison])
        assert poison.requeued is True

    assert _published(worker) == []


@pytest.mark.anyio
async def test_table_group_the_worker_cannot_insert_parks_at_the_limit(anyio_backend):
    sink = _UnknownTableGroupSink()
    worker = _worker(sink, max_insert_attempts=3)

    for attempt in range(1, 4):
        poison = _Message([1], table_group="ghost")
        good = _Message([attempt])
        await _deliver(worker, [poison, good])
        assert good.acked is True
        if attempt < 3:
            assert poison.requeued is True
            assert worker.failure_counts["ghost"] == attempt

    assert poison.acked is True
    published = _published(worker)
    assert len(published) == 1
    headers = published[0]["message"].headers
    assert headers["x-evnt-table-group"] == "ghost"
    assert headers["x-evnt-error-type"] == "KeyError"
    assert headers["x-evnt-failure"] == "max_insert_attempts"
    assert "ghost" not in worker.failure_counts
    assert worker._backoff_seconds() == 0


@pytest.mark.anyio
async def test_failure_of_every_insert_stays_cheap_once_messages_reach_the_limit(anyio_backend):
    sink = _FailingRowsSink(always=TypeError("every row fails"))
    worker = _worker(sink, max_insert_attempts=2)

    for _ in range(4):
        sink.attempts = 0
        messages = [_Message([i]) for i in range(500)]
        await _deliver(worker, messages)
        assert all(message.requeued for message in messages)
        assert sink.attempts < 3 * worker_module._MAX_FAILED_INSERTS_WITHOUT_SUCCESS

    assert _published(worker) == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "outage",
    [OperationalError("Network Error"), TimeoutError()],
    ids=repr,
)
async def test_outage_after_part_of_a_message_was_inserted_requeues_it_whole(anyio_backend, outage):
    # The first half of the merged message goes in, then ClickHouse goes away
    # while the second half is being bisected: the rows not yet checked must
    # not be parked even though an insert of the flush succeeded.
    sink = _OutageAfterFirstInsertSink(ValueError("bad value"), bad_ids={6}, outage=outage)
    worker = _worker(sink, max_insert_attempts=2)
    message = _Message(range(1, 9))

    await _deliver(worker, [message])

    assert sink.inserted == [1, 2, 3, 4]
    assert message.requeued is True
    assert message.acked is False
    assert _published(worker) == []
    assert worker._attempts.failures(message) == 0
    assert worker.failure_counts["evnt"] == 1


@pytest.mark.anyio
async def test_lone_merged_message_with_bad_leading_rows_is_parked_at_once(anyio_backend):
    # Alone in its flush, the message's own good rows are the only proof; they
    # must be reached before the no-success cap.
    sink = _FailingRowsSink(ValueError("bad value"), bad_ids=range(10))
    worker = _worker(sink)
    message = _Message(range(100))

    await _deliver(worker, [message])

    assert message.acked is True
    published = _published(worker)
    assert len(published) == 1
    assert _parked_ids(published[0]) == list(range(10))
    assert sorted(sink.inserted) == list(range(10, 100))


@pytest.mark.anyio
async def test_driver_data_error_on_every_row_of_a_lone_message_is_parked_at_once(anyio_backend):
    # No insert can succeed, but the driver's DataError needs no proof, so the
    # no-success cap must not stop its isolation.
    sink = _FailingRowsSink(DataError("bad value"), bad_ids=range(100))
    worker = _worker(sink)
    message = _Message(range(100))

    await _deliver(worker, [message])

    assert message.acked is True
    published = _published(worker)
    assert len(published) == 1
    assert _parked_ids(published[0]) == list(range(100))
    assert published[0]["message"].headers["x-evnt-failure"] == "deterministic"
    assert "evnt" not in worker.failure_counts
