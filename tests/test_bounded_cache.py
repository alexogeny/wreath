from __future__ import annotations

import pytest

from wreath.cache import BoundedCache


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_get_set_and_miss() -> None:
    cache: BoundedCache[str, int] = BoundedCache(max_entries=4)
    assert cache.get("a") is None
    cache.set("a", 1)
    assert cache.get("a") == 1
    assert "a" in cache and "b" not in cache


def test_lru_eviction_past_capacity() -> None:
    cache: BoundedCache[str, int] = BoundedCache(max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")  # touch a, so b is now the LRU
    cache.set("c", 3)  # evicts b
    assert "a" in cache and "c" in cache
    assert "b" not in cache
    assert cache.stats.evictions == 1


def test_ttl_expiry_is_lazy() -> None:
    clock = _Clock()
    cache: BoundedCache[str, int] = BoundedCache(max_entries=8, ttl=10, clock=clock)
    cache.set("a", 1)
    clock.now = 9.9
    assert cache.get("a") == 1
    clock.now = 10.0
    assert cache.get("a") is None  # expired exactly at ttl
    assert cache.stats.expirations == 1


def test_updating_a_key_refreshes_recency_and_value() -> None:
    cache: BoundedCache[str, int] = BoundedCache(max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("a", 10)  # update a -> a is now most-recent
    cache.set("c", 3)  # evicts b, not a
    assert cache.get("a") == 10
    assert "b" not in cache


def test_delete_clear_and_stats() -> None:
    cache: BoundedCache[str, int] = BoundedCache(max_entries=4)
    cache.set("a", 1)
    assert cache.delete("a") is True
    assert cache.delete("a") is False
    cache.set("b", 2)
    cache.get("b")
    cache.get("missing")
    assert cache.stats.hits == 1 and cache.stats.misses == 1
    assert cache.stats.hit_rate == 0.5
    cache.clear()
    assert len(cache) == 0


def test_validates_arguments() -> None:
    with pytest.raises(ValueError):
        BoundedCache(max_entries=0)
    with pytest.raises(ValueError):
        BoundedCache(ttl=0)
