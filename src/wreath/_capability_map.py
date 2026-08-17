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
        "_heap",
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
    ) -> None:
        self._table = KV(max_entries=max_entries, ttl=ttl, clock=clock)
        self._clock = clock
        self._last_now = clock()
        self._ttl = ttl
        self._deadlines: dict[Any, float] = {}
        self._heap: list[tuple[float, int, Any]] = []
        self._sequence = 0
        self.overflow = overflow

    @property
    def max_entries(self) -> int:
        return self._table.max_entries

    def __len__(self) -> int:
        # Capability stores sweep when they are used.  Counting at the last
        # observed instant preserves that contract for callers that supply a
        # synthetic `now` (and avoids asking the native table's real clock).
        return self._table.count(now=self._last_now)

    def _now(self, now: float | None) -> float:
        current = self._clock() if now is None else now
        self._last_now = current
        return current

    def peek(self, key: Any, *, now: float | None = None) -> Any:
        return self._table.peek(key, None, self._now(now))

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
        new = self.peek(key, now=current) is None
        if new and self._table.count(now=current) >= self._table.max_entries:
            if self.overflow == "refuse":
                return False
            if self.overflow == "earliest":
                self._evict_earliest(current)
        self._table.set(key, value, ttl, current, keep_deadline)
        lifetime = self._ttl if ttl is None else ttl
        if new and self.overflow == "earliest" and lifetime is not None:
            deadline = current + lifetime
            self._sequence += 1
            self._deadlines[key] = deadline
            heapq.heappush(self._heap, (deadline, self._sequence, key))
        return True

    def claim(
        self,
        key: Any,
        value: Any = True,
        *,
        ttl: float | None = None,
        now: float | None = None,
    ) -> bool:
        if self.peek(key, now=now) is not None:
            return False
        return self.put(key, value, ttl=ttl, now=now)

    def complete(self, key: Any, value: Any, *, now: float | None = None) -> bool:
        current = self._now(now)
        if self.peek(key, now=current) is None:
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
        return value

    def discard(self, key: Any) -> None:
        self._table.delete(key)
        self._deadlines.pop(key, None)

    def _evict_earliest(self, now: float) -> None:
        while self._heap:
            deadline, _sequence, key = heapq.heappop(self._heap)
            if self._deadlines.get(key) != deadline:
                continue
            self._deadlines.pop(key, None)
            if self._table.peek(key, None, now) is not None:
                self._table.delete(key)
                return


__all__: tuple[str, ...] = ()
