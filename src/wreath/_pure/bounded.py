"""A small, controllable in-process cache with LRU eviction and optional TTL.

Distinct from `SnapshotCache` (whole-generation, read-mostly reference
data): this is a *bounded, evictable* key/value store for hot request-path uses
— response caching, idempotency replay — where you want a hard ceiling on
entries and time, no background threads, and no external backend to operate.

You control it completely: a fixed `max_entries` (LRU eviction past it), an
optional `ttl` (lazy expiry on read), and explicit `delete`/`clear`. It is
built for single-thread (event-loop) use; there is no internal lock.

The read/write path is `OrderedDict` moves and pops that CPython already
services in C, so this pure implementation is the shipped one; a native twin can
be selected the same way `wreath.cache` selects `SnapshotCache` if a
measured one is ever added.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True, slots=True)
class CacheStats:
    """A point-in-time view of a store's activity."""

    hits: int
    misses: int
    evictions: int
    expirations: int
    size: int

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class BoundedCache[K, V]:
    """A bounded LRU cache with optional per-entry TTL.

    Args:
        max_entries: hard ceiling; the least-recently-used entry is evicted once
            a set would exceed it. Must be positive.
        ttl: seconds an entry stays fresh, or `None` to never expire by time.
            Expiry is lazy — checked on read — so nothing runs in the background.
        clock: monotonic time source, injectable for deterministic tests.
    """

    __slots__ = ("_clock", "_data", "_evictions", "_expirations", "_hits",
                 "_max_entries", "_misses", "_ttl")

    def __init__(
        self,
        max_entries: int = 1024,
        ttl: float | None = None,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl must be positive or None")
        self._max_entries = max_entries
        self._ttl = ttl
        self._clock = clock
        #: key -> (value, expiry_or_None); ordered oldest-first for LRU.
        self._data: OrderedDict[K, tuple[V, float | None]] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expirations = 0

    def _live(self, key: K) -> tuple[V, float | None] | None:
        """The entry if present and unexpired; drops it and returns None if stale."""
        entry = self._data.get(key)
        if entry is None:
            return None
        expiry = entry[1]
        if expiry is not None and self._clock() >= expiry:
            del self._data[key]
            self._expirations += 1
            return None
        return entry

    def get(self, key: K, default: V | None = None) -> V | None:
        """Return the value for `key` (refreshing its recency), or `default`."""
        entry = self._live(key)
        if entry is None:
            self._misses += 1
            return default
        self._data.move_to_end(key)
        self._hits += 1
        return entry[0]

    def set(self, key: K, value: V) -> None:
        """Store `value` under `key`, evicting the LRU entry if now over capacity."""
        expiry = None if self._ttl is None else self._clock() + self._ttl
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (value, expiry)
        while len(self._data) > self._max_entries:
            self._data.popitem(last=False)
            self._evictions += 1

    def __contains__(self, key: object) -> bool:
        return self._live(key) is not None  # type: ignore[arg-type]

    def delete(self, key: K) -> bool:
        """Drop `key`; return whether it was present."""
        return self._data.pop(key, None) is not None

    def clear(self) -> None:
        self._data.clear()

    def snapshot(self) -> dict[K, V]:
        """The unexpired entries, as a plain dict.

        A copy, and deliberately not a live view: callers want to count or
        inspect without the iteration order being disturbed by the LRU bookkeeping
        a `get` performs.
        """
        now = self._clock()
        return {
            key: value
            for key, (value, expiry) in self._data.items()
            if expiry is None or now < expiry
        }

    def __len__(self) -> int:
        return len(self._data)

    @property
    def stats(self) -> CacheStats:
        return CacheStats(self._hits, self._misses, self._evictions,
                          self._expirations, len(self._data))


__all__ = ["BoundedCache", "CacheStats"]
