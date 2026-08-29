from __future__ import annotations

import asyncio

import pytest

from wreath._snapshot import SnapshotCache as PureSnapshotCache
from wreath.cache import SnapshotCache

BACKENDS = [PureSnapshotCache]
if SnapshotCache is not PureSnapshotCache:
    BACKENDS.append(SnapshotCache)


@pytest.mark.parametrize("cache_type", BACKENDS)
def test_replace_publishes_new_generation(cache_type) -> None:
    cache = cache_type()
    assert cache.generation == 0
    generation = cache.replace({1: "a", 2: "b"})
    assert generation == 1
    assert cache.generation == 1
    assert cache.get(1) == "a"
    assert cache.get(99) is None
    assert cache.get(99, "fallback") == "fallback"


@pytest.mark.parametrize("cache_type", BACKENDS)
def test_get_many_preserves_order_and_duplicates(cache_type) -> None:
    cache = cache_type()
    cache.replace({1: "a", 2: "b"})
    assert cache.get_many([1, 2, 1, 99]) == ["a", "b", "a", None]
    assert cache.get_many([99, 99], "x") == ["x", "x"]


@pytest.mark.parametrize("cache_type", BACKENDS)
def test_require_raises_on_miss(cache_type) -> None:
    cache = cache_type()
    cache.replace({1: "a"})
    assert cache.require(1) == "a"
    with pytest.raises(KeyError):
        cache.require(2)


@pytest.mark.parametrize("cache_type", BACKENDS)
def test_capacity_violation_preserves_previous(cache_type) -> None:
    cache = cache_type(max_entries=2)
    cache.replace({1: "a", 2: "b"})
    with pytest.raises(ValueError):
        cache.replace({1: "a", 2: "b", 3: "c"})
    # The failed publish left the previous generation intact.
    assert cache.generation == 1
    assert cache.get(1) == "a"
    assert cache.get(3) is None


@pytest.mark.parametrize("cache_type", BACKENDS)
def test_byte_capacity_violation_preserves_previous(cache_type) -> None:
    cache = cache_type(max_bytes=1)
    with pytest.raises(ValueError, match="max_bytes"):
        cache.replace({1: "a"})
    assert cache.generation == 0


@pytest.mark.parametrize("cache_type", BACKENDS)
def test_membership_and_iteration(cache_type) -> None:
    cache = cache_type()
    cache.replace({1: "a", 2: "b"})
    assert 1 in cache
    assert 3 not in cache
    assert len(cache) == 2
    assert dict(cache) == {1: "a", 2: "b"}


@pytest.mark.parametrize("cache_type", BACKENDS)
async def test_single_flight_refresh(cache_type) -> None:
    cache = cache_type()
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {5: "e"}

    await asyncio.gather(*[cache.refresh(loader) for _ in range(5)])
    assert calls == 1
    assert cache.get(5) == "e"
    assert cache.generation == 1


@pytest.mark.parametrize("cache_type", BACKENDS)
async def test_refresh_failure_preserves_previous(cache_type) -> None:
    cache = cache_type()
    cache.replace({1: "a"})

    async def boom():
        raise RuntimeError("load failed")

    with pytest.raises(RuntimeError):
        await cache.refresh(boom)
    assert cache.get(1) == "a"
    assert cache.generation == 1


@pytest.mark.parametrize("cache_type", BACKENDS)
async def test_readers_see_one_complete_generation(cache_type) -> None:
    cache = cache_type()
    cache.replace(dict.fromkeys(range(100), "old"))

    async def reader():
        # Whatever generation we observe, every key in it is consistent.
        seen = {cache.get(k) for k in range(100)}
        return seen

    async def writer():
        cache.replace(dict.fromkeys(range(100), "new"))

    results = await asyncio.gather(reader(), writer(), reader())
    for seen in (results[0], results[2]):
        assert seen in ({"old"}, {"new"})
