"""The pure-Python twin of the native `KV` table.

Selected when the extension is absent or `WREATH_PURE=1` is set. It answers
every question the native one does, in the same order, with the same counters
-- the parity suite drives identical operation sequences at both and compares
after each step, so a divergence is a test failure rather than a surprise in
production.

`now`, `ttl`, `keep_deadline` and `cost` are positional-or-keyword here exactly
as they are on the native arm, and that is load-bearing rather than cosmetic:
the wrappers in `wreath.cache` and `wreath.store` pass `now` **positionally** to
avoid the keyword dict a C method would otherwise build per call, so a
keyword-only parameter here would raise `TypeError` under `WREATH_PURE=1` while
every native-arm test stayed green. `ty` caught that; the parity suite could not,
because it calls both arms the same way.

What it does *not* reproduce is the layout. There is no control-byte array and
no group probing here: an `OrderedDict` is the right pure structure for an LRU,
because CPython already services its moves and pops in C, and reimplementing
open addressing in Python would be slower as well as longer. The observable
behaviour is the contract; the SwissTable is an implementation detail of the
arm that has one.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from time import monotonic
from typing import Any

#: What `set(ttl=None)` means on a table with no default lifetime.
_NEVER = math.inf


class KV:
    """A bounded key/value table with LRU eviction and lazy TTL expiry.

    Args:
        max_entries: hard ceiling; the least recently used entry goes once a
            write would exceed it. Must be positive.
        ttl: default seconds an entry stays live, or `None` to never expire by
            time. Expiry is lazy -- checked on read -- so nothing runs in the
            background.
        max_bytes: ceiling on the summed `cost` of the live entries, or `None`
            to bound by entry count alone.
        track_evictions: record evicted `(key, value)` pairs for `take_evicted`.
        clock: a time source, or `None` for the built-in monotonic clock.
    """

    __slots__ = ("_clock", "_data", "_evicted", "_max_bytes", "_max_entries", "_ttl",
                 "bytes", "evictions", "expirations", "hits", "misses")

    def __init__(
        self,
        max_entries: int = 1024,
        ttl: float | None = None,
        *,
        max_bytes: int | None = None,
        track_evictions: bool = False,
        clock: Any = None,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if ttl is not None and not ttl > 0:
            raise ValueError("ttl must be positive or None")
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be positive or 0 for unbounded")
        self._max_entries = max_entries
        self._ttl = ttl
        self._max_bytes = max_bytes or 0
        #: key -> (value, deadline, cost); ordered least-recently-used first,
        #: which is the reverse of what `items()` reports and the order eviction
        #: reads.
        self._data: OrderedDict[Any, tuple[Any, float, int]] = OrderedDict()
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable or None")
        self._clock = clock
        self._evicted: list[tuple[Any, Any]] | None = [] if track_evictions else None
        self.bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.expirations = 0

    @property
    def max_entries(self) -> int:
        """The configured ceiling."""
        return self._max_entries

    @property
    def ttl(self) -> float | None:
        """The default lifetime, or None."""
        return self._ttl

    @property
    def clock(self) -> Any:
        """The injected time source, or None for the built-in monotonic clock."""
        return self._clock

    def _now(self, now: float | None = None) -> float:
        """The time for one operation: explicit, then injected, then monotonic.

        The three-step order is what lets one table serve both a caller that
        threads its own time through every call and one that installs a clock
        once.
        """
        if now is not None:
            return now
        return monotonic() if self._clock is None else self._clock()

    @property
    def max_bytes(self) -> int | None:
        """The byte ceiling, or None when only the entry count is bounded."""
        return self._max_bytes or None

    @property
    def slots(self) -> int:
        """Slots allocated.

        A dict has no fixed slot count to report, so this is the live entry
        count -- enough for the one thing callers use it for, which is noticing
        that a table has grown. The native twin reports its real table size.
        """
        return len(self._data)

    def _deadline(self, now: float, ttl: float | None) -> float:
        if ttl is None:
            return _NEVER if self._ttl is None else now + self._ttl
        if not ttl > 0:
            raise ValueError("ttl must be positive")
        return now + ttl

    def _live(self, key: Any, now: float) -> tuple[Any, float, int] | None:
        """The entry if present and unexpired; drops it and returns None if not."""
        entry = self._data.get(key)
        if entry is None:
            return None
        if now >= entry[1]:
            self._drop(key)
            self.expirations += 1
            return None
        return entry

    def _drop(self, key: Any) -> tuple[Any, float, int]:
        """Remove one entry and give back what it retained."""
        entry = self._data.pop(key)
        self.bytes -= entry[2]
        return entry

    def _evict_tail(self) -> None:
        """Drop the least recently used entry, recording it if asked to.

        The recording is what lets a cache whose entries own something outside
        the table use one -- an evicted prepared plan still exists on the
        database backend until a Close goes out for it, so a table that evicted
        silently would leak a server-side statement per eviction.
        """
        key = next(iter(self._data))
        entry = self._drop(key)
        if self._evicted is not None:
            self._evicted.append((key, entry[0]))
        self.evictions += 1

    def _enforce_budget(self) -> None:
        """Bring the table back inside both ceilings after a write."""
        while self._data and (
            len(self._data) > self._max_entries
            or (self._max_bytes and self.bytes > self._max_bytes)
        ):
            self._evict_tail()

    def get(self, key: Any, default: Any = None, now: float | None = None) -> Any:
        """The value stored under `key`, or `default` when absent or expired."""
        entry = self._live(key, self._now(now))
        if entry is None:
            self.misses += 1
            return default
        self.hits += 1
        self._data.move_to_end(key)
        return entry[0]

    def peek(self, key: Any, default: Any = None, now: float | None = None) -> Any:
        """The value under `key` without counting, touching, or expiring it.

        The read that does not disturb what it is reading. `get` is the one that
        means "I am using this value", and only that one should shape eviction.
        """
        entry = self._data.get(key)
        clock = self._now(now)
        if entry is None or clock >= entry[1]:
            return default
        return entry[0]

    def set(
        self,
        key: Any,
        value: Any,
        ttl: float | None = None,
        now: float | None = None,
        keep_deadline: bool = False,
        cost: int = 0,
    ) -> None:
        """Store `value` under `key`, evicting the least recently used if full.

        `keep_deadline` preserves the deadline a live key already has rather
        than starting a fresh window -- the rule a claim ledger needs so that a
        holder which keeps writing cannot extend its own key indefinitely.

        `cost` is what this entry retains in bytes, for a table built with
        `max_bytes`. It is the caller's number because only the caller knows
        what a value really holds.
        """
        if cost < 0:
            raise ValueError("cost cannot be negative")
        clock = self._now(now)
        deadline = self._deadline(clock, ttl)
        existing = self._data.get(key)
        if existing is not None:
            if clock >= existing[1]:
                # Expired in place: the window restarts even under
                # keep_deadline, because there is no live window left to keep.
                self.expirations += 1
            elif keep_deadline:
                deadline = existing[1]
            self.bytes += cost - existing[2]
            self._data[key] = (value, deadline, cost)
            self._data.move_to_end(key)
            self._enforce_budget()
            return
        self._evict_for_one(clock)
        self._data[key] = (value, deadline, cost)
        self.bytes += cost
        # Enforced after the insert, not before: the entry's own cost is what
        # may have breached the byte budget, and one whose cost alone exceeds it
        # is evicted again immediately -- leaving the table empty rather than
        # over its bound, which is what the caches this replaces both did.
        self._enforce_budget()

    def _evict_for_one(self, now: float) -> None:
        """Make room for one more key, expired entries first."""
        if len(self._data) < self._max_entries:
            return
        for stale in [k for k, entry in self._data.items() if now >= entry[1]]:
            self._drop(stale)
            self.expirations += 1
        while len(self._data) >= self._max_entries:
            self._evict_tail()

    def claim(
        self,
        key: Any,
        value: Any = None,
        ttl: float | None = None,
        now: float | None = None,
    ) -> bool:
        """Store `value` under `key` only if nothing live is there.

        Reports whether this call was the one that took it. There is no await
        between the lookup and the write, which is what makes it atomic against
        every other task on this loop.
        """
        clock = self._now(now)
        if self._live(key, clock) is not None:
            return False
        self._evict_for_one(clock)
        self._data[key] = (value, self._deadline(clock, ttl), 0)
        self._data.move_to_end(key)
        return True

    def delete(self, key: Any) -> bool:
        """Drop `key`, reporting whether it was there."""
        if self._live(key, self._now()) is None:
            return False
        self._drop(key)
        return True

    def pop(self, key: Any, default: Any = None, *, now: float | None = None) -> Any:
        """Remove `key` and return its value, or `default` when absent."""
        entry = self._live(key, self._now(now))
        if entry is None:
            self.misses += 1
            return default
        self.hits += 1
        self._drop(key)
        return entry[0]

    def touch(
        self, key: Any, *, ttl: float | None = None, now: float | None = None
    ) -> bool:
        """Start a fresh window for a live `key`; report whether there was one."""
        clock = self._now(now)
        entry = self._live(key, clock)
        if entry is None:
            return False
        self._data[key] = (entry[0], self._deadline(clock, ttl), entry[2])
        self._data.move_to_end(key)
        return True

    def take_evicted(self) -> list[tuple[Any, Any]]:
        """The `(key, value)` pairs evicted since the last call; clears the record.

        Empty unless the table was built with `track_evictions=True`. Only
        evictions appear: an expiry or an explicit delete does not, because the
        caller already knows about those, and an eviction is the one the table
        decided on its own.
        """
        if self._evicted is None:
            return []
        taken = self._evicted
        self._evicted = []
        return taken

    def purge(self, *, now: float | None = None) -> int:
        """Drop every entry whose deadline has passed, returning how many went."""
        clock = self._now(now)
        stale = [key for key, entry in self._data.items() if clock >= entry[1]]
        for key in stale:
            self._drop(key)
        self.expirations += len(stale)
        return len(stale)

    def count(self, *, now: float | None = None) -> int:
        """Live entries as of `now`. `len(table)` is this against the real clock.

        It takes a time because `len()` cannot, and a caller with an injected
        clock needs to ask at *its* time -- a store built on a test clock is
        entirely expired as far as the real one is concerned.
        """
        clock = self._now(now)
        return sum(1 for entry in self._data.values() if clock < entry[1])

    def clear(self) -> int:
        """Drop every entry, live or expired, and report how many went.

        Counters are left alone: they describe the table's history. The count
        is returned because `wreath.queue`'s `clear` returns one, and one
        family should not have two answers to what `clear` gives back.
        """
        held = len(self._data)
        self._data.clear()
        self.bytes = 0
        if self._evicted is not None:
            self._evicted.clear()
        return held

    def keys(self, *, now: float | None = None) -> list[Any]:
        """Live keys, most recently used first."""
        return [key for key, _value in self.items(now=now)]

    def values(self, *, now: float | None = None) -> list[Any]:
        """Live values, most recently used first."""
        return [value for _key, value in self.items(now=now)]

    def items(self, *, now: float | None = None) -> list[tuple[Any, Any]]:
        """Live (key, value) pairs, most recently used first."""
        clock = self._now(now)
        return [
            (key, entry[0])
            for key, entry in reversed(self._data.items())
            if clock < entry[1]
        ]

    def snapshot(self, *, now: float | None = None) -> dict[Any, Any]:
        """The live entries as a plain dict.

        A copy, and deliberately not a view: a caller counting or inspecting
        should not have the order disturbed by the recency bookkeeping a `get`
        performs.
        """
        return dict(self.items(now=now))

    def __contains__(self, key: Any) -> bool:
        return self._live(key, self._now()) is not None

    def __len__(self) -> int:
        """How many entries the table still honours.

        Expired entries are dropped lazily, so counting the raw dict would
        report keys this table refuses to return -- which makes `len()` a
        measure of debris rather than of what is held.
        """
        return self.count()


__all__ = ["KV"]
