from typing import Any

import pytest

from wreath._capability_map import CapabilityMap
from wreath.kv import KV


class CountedTable(KV):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.counts = 0

    def count(self, *, now: float | None = None) -> int:
        self.counts += 1
        return super().count(now=now)


def test_evict_writes_do_not_count_the_native_table() -> None:
    mapping = CapabilityMap(max_entries=16, clock=lambda: 0.0)
    table = CountedTable(max_entries=16, clock=lambda: 0.0)
    mapping._table = table
    for index in range(32):
        assert mapping.put(index % 20, index)
    assert mapping.peek(11) == 31
    assert table.counts == 0


@pytest.mark.parametrize("overflow", ["evict", "earliest", "refuse"])
def test_existing_key_updates_do_not_count_capacity(overflow: Any) -> None:
    mapping = CapabilityMap(max_entries=16, overflow=overflow, clock=lambda: 0.0)
    table = CountedTable(max_entries=16, clock=lambda: 0.0)
    mapping._table = table
    mapping.put("key", 0)
    table.counts = 0
    for index in range(32):
        assert mapping.put("key", index)
    assert mapping.peek("key") == 31
    assert table.counts == 0


def test_evict_writes_preserve_native_counters_and_explicit_clock() -> None:
    calls = []

    def clock() -> float:
        calls.append(1)
        return 0.0

    mapping = CapabilityMap(max_entries=2, clock=clock)
    calls.clear()
    mapping.put("a", 1, ttl=1, now=0)
    mapping.put("b", 2, ttl=100, now=0)
    mapping.put("a", 3, ttl=100, now=2)
    mapping.put("c", 4, ttl=100, now=2)
    assert calls == []
    assert mapping.held("b") is None
    assert mapping.held("a") == 3
    assert mapping._table.evictions == 1
    assert mapping._table.expirations == 1
    assert mapping._table.hits == mapping._table.misses == 0


def test_deadline_changes_preserve_existing_sweep_tie_order() -> None:
    mapping = CapabilityMap(max_entries=2, overflow="refuse", clock=lambda: 0.0)
    other = object()
    mapping.put(None, "first", ttl=10)
    mapping.put(other, "second", ttl=10)
    mapping.put(None, "first", ttl=20)
    mapping.put(None, "first", ttl=10)
    assert mapping.sweep(now=10) == ("first", "second")
