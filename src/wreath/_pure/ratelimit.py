"""Pure-Python token-bucket twin.

Mirrors the observable behavior of `wreath._native.ratelimit.TokenBucket`: lazy
refill, a hard `max_entries` ceiling, reclaiming buckets that have refilled to
capacity, and evicting the fullest bucket when every one is still limited.

The native type's `slots` attribute is not mirrored: it reports the size of
the C hash table, which has no counterpart here. `tracked` is the portable
observable.
"""

from __future__ import annotations


class TokenBucket:
    """Bounded in-process token-bucket table."""

    __slots__ = ("_buckets", "_max_entries", "capacity", "rate")

    def __init__(self, capacity: float, rate: float, max_entries: int = 10000) -> None:
        if not capacity > 0.0:
            raise ValueError("capacity must be positive")
        if not rate > 0.0:
            raise ValueError("rate must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.capacity = float(capacity)
        self.rate = float(rate)
        self._max_entries = max_entries
        # key -> [tokens, last touched]
        self._buckets: dict[str, list[float]] = {}

    @property
    def tracked(self) -> int:
        return len(self._buckets)

    def _refill(self, entry: list[float], now: float) -> float:
        tokens, updated = entry
        elapsed = now - updated
        # A clock that moved backwards must not mint tokens.
        if elapsed <= 0.0:
            return tokens
        tokens = min(self.capacity, tokens + elapsed * self.rate)
        entry[0] = tokens
        entry[1] = now
        return tokens

    def _ensure_room(self, now: float) -> None:
        if len(self._buckets) < self._max_entries:
            return
        # A bucket refilled to capacity is indistinguishable from an absent one.
        for key in tuple(self._buckets):
            if self._refill(self._buckets[key], now) >= self.capacity:
                del self._buckets[key]
        if len(self._buckets) < self._max_entries:
            return
        fullest = max(self._buckets, key=lambda key: self._buckets[key][0])
        del self._buckets[fullest]

    def acquire(self, key: str, now: float, cost: float = 1.0) -> float:
        """Consume `cost` tokens. Returns 0.0 when allowed, else retry-after."""
        if not isinstance(key, str):
            raise TypeError("key must be a string")
        if cost <= 0.0:
            raise ValueError("cost must be positive")
        if cost > self.capacity:
            raise ValueError("cost exceeds the bucket capacity")
        entry = self._buckets.get(key)
        if entry is None:
            self._ensure_room(now)
            self._buckets[key] = [self.capacity - cost, now]
            return 0.0
        tokens = self._refill(entry, now)
        if tokens >= cost:
            entry[0] = tokens - cost
            return 0.0
        return (cost - tokens) / self.rate

    def clear(self) -> None:
        self._buckets.clear()


__all__ = ["TokenBucket"]
