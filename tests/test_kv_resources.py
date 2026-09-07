import math
import weakref

import pytest

from wreath.kv import KV


def test_repeated_empty_clear_preserves_capacity_for_refill():
    table = KV(max_entries=8192)
    for index in range(8192):
        table.set(index, index, now=0)
    slots = table.slots
    assert slots > 0
    assert table.clear() == 8192
    assert table.slots == slots
    assert table.clear() == 0
    assert table.count(now=0) == 0
    assert table.peek("missing", None, 0) is None
    assert table.purge(now=0) == 0
    table.set("new", "value", now=0)
    assert table.slots == slots
    assert table.peek("new", None, 0) == "value"


def test_clear_releases_values_and_evicted_records_preserving_counters():
    class Value:
        pass

    table = KV(max_entries=1, track_evictions=True)
    first, second = Value(), Value()
    refs = weakref.ref(first), weakref.ref(second)
    table.set("first", first, cost=10, now=0)
    table.set("second", second, cost=20, now=0)
    assert table.get("second", now=0) is second
    assert table.get("missing", now=0) is None
    counters = table.hits, table.misses, table.evictions, table.expirations
    del first, second
    assert table.clear() == 1
    assert all(ref() is None for ref in refs)
    assert table.take_evicted() == []
    assert table.bytes == 0
    assert (table.hits, table.misses, table.evictions, table.expirations) == counters
    table.set("refill", 1, cost=3, now=0)
    assert table.bytes == 3
    assert table.count(now=0) == 1


def test_empty_clear_removes_tombstones_and_eviction_records():
    table = KV(max_entries=64, track_evictions=True)
    for index in range(65):
        table.set(index, index, now=0)
    for index in range(1, 65):
        assert table.delete(index)
    assert table.count(now=0) == 0
    assert table.clear() == 0
    assert table.take_evicted() == []
    assert table.clear() == 0
    for index in range(64):
        table.set(index, index, now=0)
    assert table.count(now=0) == 64
    assert all(table.peek(index, None, 0) == index for index in range(64))


def test_count_remains_read_only_at_expiry_and_when_time_moves_backwards():
    table = KV(max_entries=8)
    table.set("short", 1, ttl=2, now=0)
    table.set("long", 2, ttl=10, now=0)
    table.set("forever", 3, now=0)
    counters = table.hits, table.misses, table.evictions, table.expirations
    assert [table.count(now=now) for now in (0, 2, 10, 1, -1)] == [3, 2, 1, 3, 3]
    assert table.held("short") == 1
    assert table.held("long") == 2
    assert (table.hits, table.misses, table.evictions, table.expirations) == counters
    assert table.count(now=math.nan) == 0
    assert table.count(now=math.inf) == 0
    assert table.count(now=-math.inf) == 3


@pytest.mark.parametrize("operation", ["set", "claim", "touch"])
def test_nan_deadlines_do_not_count_as_live_after_later_writes_and_rebuilds(operation):
    table = KV(max_entries=128, ttl=10)
    if operation == "set":
        table.set("nan", "value", now=math.nan)
    elif operation == "claim":
        assert table.claim("nan", "value", now=math.nan)
    else:
        table.set("nan", "value", now=0)
        assert table.touch("nan", now=math.nan)
    assert table.count(now=0) == 0
    for index in range(64):
        table.set(index, index, now=0)
    assert table.count(now=0) == 64
    assert table.held("nan") == "value"
    assert table.expirations == 0


def test_count_uses_injected_clock_and_preserves_clock_errors():
    now = [0]
    table = KV(max_entries=4, ttl=2, clock=lambda: now[0])
    table.set("one", 1)
    assert len(table) == table.count() == 1
    now[0] = 2
    assert len(table) == table.count() == 0
    now[0] = 0
    assert len(table) == table.count() == 1
    with pytest.raises(TypeError):
        table.count(now=object())
    now[0] = object()
    with pytest.raises(TypeError):
        table.count()


def test_count_survives_early_deadline_removal_and_extension():
    table = KV(max_entries=4, ttl=100)
    table.set("short", 1, ttl=1, now=0)
    table.set("long", 2, now=0)
    table.delete("short")
    assert table.count(now=2) == 1
    table.set("short", 1, ttl=1, now=0)
    assert table.touch("short", ttl=100, now=0)
    assert table.count(now=2) == 2
