"""Bounded, expiring, optionally single-use in-process capabilities.

Security domains keep their refusal policy in the caller. This primitive owns
the reusable mechanism: deadline-aware storage, atomic event-loop-local claim,
conditional consume, and update without extending the original window.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable
from time import monotonic
from typing import Any, Literal

from .kv import KV

Overflow = Literal["evict", "earliest", "refuse"]


class CapabilityMap:
    """A policy-parameterized capability table over the canonical native KV."""

    __slots__ = (
        "_clock",
        "_deadlines",
        "_expire_at_deadline",
        "_heap",
        "_keys",
        "_last_now",
        "_sequence",
        "_table",
        "_ttl",
        "overflow",
    )

    def __init__(
        self,
        *,
        max_entries: int,
        ttl: float | None = None,
        clock: Callable[[], float] = monotonic,
        overflow: Overflow = "evict",
        expire_at_deadline: bool = True,
    ) -> None:
        self._table = KV(max_entries=max_entries, ttl=ttl, clock=clock)
        self._clock = clock
        self._last_now = clock()
        self._ttl = ttl
        self._deadlines: dict[Any, float] = {}
        self._heap: list[tuple[float, int, Any]] = []
        self._keys: set[Any] = set()
        self._sequence = 0
        self.overflow = overflow
        self._expire_at_deadline = expire_at_deadline

    @property
    def max_entries(self) -> int:
        return self._table.max_entries

    def __len__(self) -> int:
        # Capability stores sweep when they are used.  Counting at the last
        # observed instant preserves that contract for callers that supply a
        # synthetic `now` (and avoids asking the native table's real clock).
        if self.overflow != "refuse":
            return self._table.count(now=self._last_now)
        self._purge_expired(self._last_now)
        return len(self._keys)

    def __contains__(self, key: Any) -> bool:
        return self.peek(key, now=self._last_now) is not None

    def _now(self, now: float | None) -> float:
        current = self._clock() if now is None else now
        self._last_now = current
        return current

    def peek(self, key: Any, *, now: float | None = None) -> Any:
        current = self._now(now)
        if self.overflow == "refuse":
            self._purge_expired(current)
        return self._table.peek(key, None, current)

    def held(self, key: Any) -> Any:
        return self._table.held(key)

    def put(
        self,
        key: Any,
        value: Any,
        *,
        ttl: float | None = None,
        now: float | None = None,
        keep_deadline: bool = False,
    ) -> bool:
        current = self._now(now)
        if self.overflow == "refuse":
            self._purge_expired(current)
            new = key not in self._keys
        else:
            new = self._table.peek(key, None, current) is None
        return self._put(key, value, ttl, current, keep_deadline, new)

    def _put(
        self,
        key: Any,
        value: Any,
        ttl: float | None,
        current: float,
        keep_deadline: bool,
        new: bool,
    ) -> bool:
        size = (
            len(self._keys)
            if self.overflow == "refuse"
            else self._table.count(now=current)
        )
        if new and size >= self._table.max_entries:
            if self.overflow == "refuse":
                return False
            if self.overflow == "earliest":
                self._evict_earliest(current)
        self._table.set(key, value, ttl, current, keep_deadline)
        if new and self.overflow == "refuse":
            self._keys.add(key)
        lifetime = self._ttl if ttl is None else ttl
        track_deadline = self.overflow == "refuse" or (new and self.overflow == "earliest")
        if track_deadline and lifetime is not None and (new or not keep_deadline):
            deadline = current + lifetime
            self._sequence += 1
            self._deadlines[key] = deadline
            heapq.heappush(self._heap, (deadline, self._sequence, key))
        elif self.overflow == "refuse" and not keep_deadline:
            self._deadlines.pop(key, None)
        return True

    def claim(
        self,
        key: Any,
        value: Any = True,
        *,
        ttl: float | None = None,
        now: float | None = None,
    ) -> bool:
        current = self._now(now)
        if self.overflow == "refuse":
            self._purge_expired(current)
            present = key in self._keys
        else:
            present = self._table.peek(key, None, current) is not None
        if present:
            return False
        return self._put(key, value, ttl, current, False, True)

    def complete(self, key: Any, value: Any, *, now: float | None = None) -> bool:
        current = self._now(now)
        if self._table.peek(key, None, current) is None:
            return False
        self._table.set(key, value, None, current, True)
        return True

    def consume(
        self,
        key: Any,
        *,
        predicate: Callable[[Any], bool] | None = None,
        now: float | None = None,
    ) -> Any:
        value = self.peek(key, now=now)
        if value is None or (predicate is not None and not predicate(value)):
            return None
        self._table.delete(key)
        self._deadlines.pop(key, None)
        self._keys.discard(key)
        return value

    def discard(self, key: Any) -> None:
        self._table.delete(key)
        self._deadlines.pop(key, None)
        self._keys.discard(key)

    def sweep(self, *, now: float | None = None) -> tuple[Any, ...]:
        return self._purge_expired(self._now(now))

    @property
    def next_deadline(self) -> float:
        while self._heap:
            deadline, _sequence, key = self._heap[0]
            if self._deadlines.get(key) == deadline:
                return deadline
            heapq.heappop(self._heap)
        return float("inf")

    def _purge_expired(self, now: float) -> tuple[Any, ...]:
        expired: list[Any] = []
        while self._heap:
            deadline, _sequence, key = self._heap[0]
            past = now >= deadline if self._expire_at_deadline else now > deadline
            if not past:
                break
            heapq.heappop(self._heap)
            if self._deadlines.get(key) != deadline:
                continue
            self._deadlines.pop(key, None)
            value = self._table.held(key)
            self._table.delete(key)
            self._keys.discard(key)
            if value is not None:
                expired.append(value)
        return tuple(expired)

    def _evict_earliest(self, now: float) -> None:
        while self._heap:
            deadline, _sequence, key = heapq.heappop(self._heap)
            if self._deadlines.get(key) != deadline:
                continue
            self._deadlines.pop(key, None)
            if self._table.peek(key, None, now) is not None:
                self._table.delete(key)
                self._keys.discard(key)
                return


__all__: tuple[str, ...] = ()
