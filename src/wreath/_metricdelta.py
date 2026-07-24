"""Shared counter-delta bookkeeping for the push-metric NFR bridges.

StatsD and CloudWatch-EMF both report counters as the *increment since the last
flush* (the collector aggregates over the period). Both need identical
"remember the last value, return the non-negative delta, fall back to the raw
value on a counter reset" semantics -- so it lives here once, audited in one
place, rather than copied per bridge.
"""
from __future__ import annotations


class DeltaTracker:
    """Per-key counter-delta tracker: ``delta(key, value)`` returns the increase
    since the previous call for ``key`` (0 on first sight), or the raw ``value``
    if the counter went backwards (a reset), never a negative number."""

    __slots__ = ("_prev",)

    def __init__(self) -> None:
        self._prev: dict[tuple, float] = {}

    def delta(self, key: tuple, value: float) -> float:
        prev = self._prev.get(key, 0)
        self._prev[key] = value
        d = value - prev
        return d if d >= 0 else value  # counter reset -> send the current value
