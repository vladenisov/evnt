"""``KeyedRateLimiter`` lets one line per key per interval through and counts the rest."""

import pytest

from evnt.observability.ratelimit import KeyedRateLimiter, Permit


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def test_first_occurrence_logs_then_repeats_are_suppressed(clock):
    limiter = KeyedRateLimiter(60.0, clock=clock)

    assert limiter.take("schema-a") == Permit(0)
    clock.now += 1
    assert limiter.take("schema-a") is None
    clock.now += 58.9
    assert limiter.take("schema-a") is None


def test_line_after_the_interval_reports_the_suppressed_count(clock):
    limiter = KeyedRateLimiter(60.0, clock=clock)
    limiter.take("schema-a")
    for _ in range(5):
        assert limiter.take("schema-a") is None

    clock.now += 60
    assert limiter.take("schema-a") == Permit(5)
    assert limiter.take("schema-a") is None

    clock.now += 60
    assert limiter.take("schema-a") == Permit(1)


def test_keys_are_limited_independently(clock):
    limiter = KeyedRateLimiter(60.0, clock=clock)

    assert limiter.take("schema-a") == Permit(0)
    assert limiter.take("schema-b") == Permit(0)
    assert limiter.take("schema-a") is None
    assert limiter.take("schema-b") is None
    assert len(limiter) == 2


def test_keys_beyond_the_bound_share_one_overflow_bucket(clock):
    limiter = KeyedRateLimiter(60.0, max_keys=2, clock=clock)
    limiter.take("a")
    limiter.take("b")

    assert limiter.take("c") == Permit(None, untracked_suppressed=0)
    assert limiter.take("d") is None
    assert limiter.take("e") is None
    assert len(limiter) == 2

    clock.now += 60
    assert limiter.take("f") == Permit(0)  # "a" was stale and made room for "f"
    assert limiter.take("g") == Permit(0)  # ... and "b" for "g"
    # The overflow line reports "d" and "e" as untracked, not as "h"'s own count.
    assert limiter.take("h") == Permit(None, untracked_suppressed=2)


def test_overlong_keys_are_never_stored(clock):
    limiter = KeyedRateLimiter(60.0, max_key_length=8, clock=clock)

    assert limiter.take("x" * 9) == Permit(None, untracked_suppressed=0)
    assert limiter.take("y" * 10_000) is None
    assert len(limiter) == 0
    assert limiter.take("short") == Permit(0)
    assert len(limiter) == 1


def test_eviction_takes_the_key_that_logged_longest_ago(clock):
    limiter = KeyedRateLimiter(60.0, max_keys=2, clock=clock)
    limiter.take("a")  # next line at 1060
    clock.now += 1
    limiter.take("b")  # next line at 1061
    clock.now = 1060.5
    assert limiter.take("a") == Permit(0)  # "a" logs again, now due at 1120.5

    clock.now = 1061.5
    assert limiter.take("c") == Permit(0)  # "b" is the stale one, so it goes
    assert limiter.take("a") is None  # "a" is still tracked and suppressed
    assert "b" not in limiter._entries
    assert "a" in limiter._entries


def test_evicted_key_hands_back_its_pending_count_under_its_own_name(clock):
    limiter = KeyedRateLimiter(60.0, max_keys=1, clock=clock)
    limiter.take("a")
    limiter.take("a")
    limiter.take("a")

    clock.now += 60
    assert limiter.take("b") == Permit(0, evicted=("a", 2))

    # Nothing of "a" lingers to surface later on another key's line.
    clock.now += 60
    assert limiter.take("c") == Permit(0)
    assert limiter.take("d") == Permit(None, untracked_suppressed=0)
    clock.now += 60
    assert limiter.take("d") == Permit(0)


def test_evicted_key_that_returns_starts_from_zero(clock):
    limiter = KeyedRateLimiter(60.0, max_keys=1, clock=clock)
    for _ in range(6):
        limiter.take("a")

    clock.now += 61
    assert limiter.take("b") == Permit(0, evicted=("a", 5))
    for _ in range(10):
        clock.now += 61
        assert limiter.take("b") == Permit(0)

    clock.now += 61
    # "a" was already reported on eviction, so its fresh line owes nothing.
    assert limiter.take("a") == Permit(0)


def test_evicting_a_key_with_nothing_held_back_reports_no_eviction(clock):
    limiter = KeyedRateLimiter(60.0, max_keys=1, clock=clock)
    limiter.take("a")

    clock.now += 60
    assert limiter.take("b") == Permit(0)


def test_log_fields_name_counts_by_whom_they_belong_to():
    assert Permit(3).log_fields() == {"suppressed": 3}
    assert Permit(0, evicted=("a", 2)).log_fields() == {"suppressed": 0}
    assert Permit(None, untracked_suppressed=4).log_fields() == {"untracked_suppressed": 4}


def test_distinct_keys_cannot_raise_the_line_rate_above_the_bound(clock):
    max_keys = 4
    limiter = KeyedRateLimiter(60.0, max_keys=max_keys, clock=clock)
    lines = 0
    for i in range(10_000):
        clock.now += 0.01  # 100 unique keys per second for 100 seconds
        if limiter.take(f"attacker-{i}") is not None:
            lines += 1

    intervals = 100 / 60
    assert lines <= (max_keys + 1) * (int(intervals) + 1)
    assert len(limiter) == max_keys


def test_every_occurrence_is_counted_once(clock):
    limiter = KeyedRateLimiter(60.0, max_keys=3, clock=clock)
    occurrences = 0
    reported = 0
    for i in range(5_000):
        clock.now += 0.05
        occurrences += 1
        permit = limiter.take(f"k{i % 7}")
        if permit is not None:
            reported += 1 + (permit.suppressed or 0) + (permit.untracked_suppressed or 0)
            if permit.evicted is not None:
                reported += permit.evicted[1]

    pending = sum(window.suppressed for window in limiter._entries.values())
    pending += limiter._overflow.suppressed
    assert reported + pending == occurrences


def test_reset_forgets_keys_and_counts(clock):
    limiter = KeyedRateLimiter(60.0, max_keys=1, clock=clock)
    limiter.take("a")
    limiter.take("b")
    limiter.take("b")

    limiter.reset()

    assert len(limiter) == 0
    assert limiter.take("a") == Permit(0)
    # "b" still overflows, but the occurrence held back before the reset is gone.
    assert limiter.take("b") == Permit(None, untracked_suppressed=0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interval_seconds": 0},
        {"interval_seconds": -1},
        {"max_keys": 0},
        {"max_key_length": 0},
    ],
)
def test_rejects_nonsensical_bounds(kwargs):
    with pytest.raises(ValueError):
        KeyedRateLimiter(**kwargs)


def test_defaults_use_the_monotonic_clock():
    limiter = KeyedRateLimiter()

    assert limiter.take("a") == Permit(0)
    assert limiter.take("a") is None
