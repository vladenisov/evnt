"""Per-key rate limiting for log lines that untrusted clients can trigger at will.

A tracker that keeps sending a payload the service cannot fully handle (a
context schema without a parser, say) would otherwise produce one warning per
event. Each emitted line costs a few hundred microseconds of CPU, so the
limiter lets the first occurrence of a key through, then at most one line per
key per interval that reports how many occurrences it held back. The signal
(which key, how often) survives while the volume drops.
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_MAX_KEYS = 256
DEFAULT_MAX_KEY_LENGTH = 512


class _Window:
    """When a key may log next and how many occurrences it held back meanwhile."""

    __slots__ = ("next_at", "suppressed")

    def __init__(self, next_at: float) -> None:
        self.next_at = next_at
        self.suppressed = 0


@dataclass(frozen=True, slots=True)
class Permit:
    """Leave to log one line, with the counts that line has to carry.

    Every count is attributed to the key it belongs to, so a log line never
    reports one key's occurrences under another key's name.
    """

    suppressed: int | None
    """Occurrences of this key held back since its previous line; ``0`` the
    first time. ``None`` on an overflow line: untracked keys share one
    counter, so no count can be pinned on this particular key."""

    untracked_suppressed: int | None = None
    """Overflow lines only: occurrences of untracked keys held back since the
    previous overflow line."""

    evicted: tuple[str, int] | None = None
    """A stale key dropped to make room for this one, with the occurrences it
    still held back. Its next occurrence would start from zero, so this is
    the only chance to report them."""

    def log_fields(self) -> dict[str, int]:
        """The counts to put on this key's own line."""

        if self.suppressed is None:
            return {"untracked_suppressed": self.untracked_suppressed or 0}
        return {"suppressed": self.suppressed}


class KeyedRateLimiter:
    """Lets one log line through per key per interval and counts the rest.

    Keys usually come from client payloads, so memory is bounded twice: at most
    ``max_keys`` keys are tracked, and a key longer than ``max_key_length`` is
    never stored. Such keys share a single overflow bucket, which is limited
    like any other key. A tracked key whose interval has run out can make room
    for a new one; it is always the oldest entry, since entries are kept in the
    order they last logged. Whatever the evicted key still held back is handed
    back on the permit that admits the new key, so the caller can report it
    under the evicted key's own name.

    Admitting a new key needs a stale slot, so the limiter grants at most
    ``max_keys + 1`` permits per interval however many distinct keys arrive.
    A permit that evicted a key may ask for one extra line to report it, which
    at most doubles that bound.
    """

    __slots__ = (
        "_clock",
        "_entries",
        "_interval",
        "_lock",
        "_max_key_length",
        "_max_keys",
        "_overflow",
    )

    def __init__(
        self,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        *,
        max_keys: int = DEFAULT_MAX_KEYS,
        max_key_length: int = DEFAULT_MAX_KEY_LENGTH,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if max_keys < 1:
            raise ValueError("max_keys must be at least 1")
        if max_key_length < 1:
            raise ValueError("max_key_length must be at least 1")
        self._interval = interval_seconds
        self._max_keys = max_keys
        self._max_key_length = max_key_length
        self._clock = clock
        self._entries: OrderedDict[str, _Window] = OrderedDict()
        self._overflow = _Window(float("-inf"))
        # Only structural changes take the lock; the suppressed path stays a
        # dict lookup and a clock compare.
        self._lock = threading.Lock()

    def take(self, key: str) -> Permit | None:
        """Return a permit if this occurrence may log, else ``None``."""

        window = self._entries.get(key)
        if window is None:
            return self._take_untracked(key)
        now = self._clock()
        if now < window.next_at:
            window.suppressed += 1
            return None
        with self._lock:
            if self._entries.get(key) is window:
                self._entries.move_to_end(key)
            return Permit(self._release(window, now))

    def reset(self) -> None:
        """Forget every key and count."""

        with self._lock:
            self._entries.clear()
            self._overflow = _Window(float("-inf"))

    def __len__(self) -> int:
        return len(self._entries)

    def _take_untracked(self, key: str) -> Permit | None:
        now = self._clock()
        with self._lock:
            window = self._entries.get(key)
            if window is not None:
                # Another thread admitted the key while this one waited.
                if now < window.next_at:
                    window.suppressed += 1
                    return None
                self._entries.move_to_end(key)
                return Permit(self._release(window, now))
            if len(key) <= self._max_key_length:
                admitted, evicted = self._make_room(now)
                if admitted:
                    self._entries[key] = _Window(now + self._interval)
                    return Permit(0, evicted=evicted)
            overflow = self._overflow
            if now < overflow.next_at:
                overflow.suppressed += 1
                return None
            return Permit(None, untracked_suppressed=self._release(overflow, now))

    def _make_room(self, now: float) -> tuple[bool, tuple[str, int] | None]:
        """Free a slot if one is due; also return the evicted key's pending count."""

        if len(self._entries) < self._max_keys:
            return True, None
        oldest_key, oldest = next(iter(self._entries.items()))
        if now < oldest.next_at:
            return False, None
        del self._entries[oldest_key]
        if oldest.suppressed:
            return True, (oldest_key, oldest.suppressed)
        return True, None

    def _release(self, window: _Window, now: float) -> int:
        suppressed = window.suppressed
        window.suppressed = 0
        window.next_at = now + self._interval
        return suppressed
