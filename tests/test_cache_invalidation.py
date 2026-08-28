"""ORM-driven cache invalidation within and across workers."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from orm.conftest import FakeDatabase, Post, User

from wreath._orm_events import (
    WRITE_CHANNEL,
    has_subscribers,
    publish_write,
    subscribe_writes,
    unsubscribe_writes,
)
from wreath.cache import (
    SnapshotCache,
    invalidate_across_workers,
    refresh_on,
)
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.response_cache import cached


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def registry(database: FakeDatabase) -> Registry:
    return Registry(database, [User, Post])


@pytest.fixture(autouse=True)
def _isolate_subscribers():
    from wreath import _orm_events

    subscribers = _orm_events._subscribers.copy()
    subscriber_keys = {
        key: subscriptions.copy()
        for key, subscriptions in _orm_events._subscriber_keys.items()
    }
    bridges = _orm_events._bridges.copy()
    yield
    _orm_events._subscribers.clear()
    _orm_events._subscribers.update(subscribers)
    _orm_events._subscriber_keys.clear()
    _orm_events._subscriber_keys.update(subscriber_keys)
    _orm_events._bridges.clear()
    _orm_events._bridges.update(bridges)


class Req:
    def __init__(self, path: str = "/report") -> None:
        self.method = "GET"
        self.path = path
        self.query_string = b""


def test_subscribers_receive_written_model_names() -> None:
    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)
    publish_write(frozenset({"User"}))
    assert seen == [frozenset({"User"})]


def test_a_broken_subscriber_cannot_fail_a_committed_write() -> None:
    def explode(written: frozenset[str]) -> None:
        raise RuntimeError("subscriber is broken")

    seen: list[frozenset[str]] = []
    subscribe_writes(explode)
    subscribe_writes(seen.append)

    publish_write(frozenset({"User"}))
    assert seen == [frozenset({"User"})]


def test_unsubscribe_stops_delivery() -> None:
    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)
    unsubscribe_writes(seen.append)
    publish_write(frozenset({"User"}))
    assert seen == []


def test_publishing_nothing_is_a_no_op() -> None:
    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)
    publish_write(frozenset())
    assert seen == []


@pytest.mark.asyncio
async def test_a_write_to_a_watched_model_drops_the_cache() -> None:
    calls = 0

    @cached(invalidate_on=[User])
    async def report(request: Any) -> dict:
        nonlocal calls
        calls += 1
        return {"n": calls}

    assert (await report(Req()))["n"] == 1
    assert (await report(Req()))["n"] == 1

    publish_write(frozenset({"User"}))

    assert (await report(Req()))["n"] == 2


@pytest.mark.asyncio
async def test_a_write_to_an_unwatched_model_leaves_the_cache_alone() -> None:
    calls = 0

    @cached(invalidate_on=[User])
    async def report(request: Any) -> dict:
        nonlocal calls
        calls += 1
        return {"n": calls}

    await report(Req())
    publish_write(frozenset({"Post"}))
    assert (await report(Req()))["n"] == 1


@pytest.mark.asyncio
async def test_a_cache_naming_no_models_ignores_writes_entirely() -> None:
    calls = 0

    @cached()
    async def report(request: Any) -> dict:
        nonlocal calls
        calls += 1
        return {"n": calls}

    await report(Req())
    publish_write(frozenset({"User"}))
    assert (await report(Req()))["n"] == 1


def test_a_cached_handler_takes_its_subscription_with_it() -> None:
    from wreath import _orm_events

    _orm_events._subscribers.clear()
    _orm_events._subscriber_keys.clear()
    _orm_events._bridges.clear()

    def define() -> None:
        @cached(invalidate_on=[User])
        async def report(request: Any) -> dict:
            return {}

        assert len(_orm_events._subscribers) == 1

    define()

    assert not _orm_events._subscribers
    assert not has_subscribers()


def test_a_cached_handler_that_is_still_referenced_keeps_listening() -> None:
    from wreath import _orm_events

    _orm_events._subscribers.clear()
    _orm_events._subscriber_keys.clear()
    _orm_events._bridges.clear()

    @cached(invalidate_on=[User])
    async def report(request: Any) -> dict:
        return {}

    routes = [report]

    assert len(_orm_events._subscribers) == 1
    assert has_subscribers()
    assert routes


@pytest.mark.asyncio
async def test_a_shared_store_is_one_invalidation_domain() -> None:
    from wreath.cache import BoundedCache

    store: BoundedCache = BoundedCache(max_entries=32)
    users_calls = 0

    @cached(store=store, invalidate_on=[User])
    async def users(request: Any) -> dict:
        nonlocal users_calls
        users_calls += 1
        return {"n": users_calls}

    @cached(store=store, invalidate_on=[Post])
    async def posts(request: Any) -> dict:
        return {}

    await users(Req("/users"))
    await posts(Req("/posts"))
    assert (await users(Req("/users")))["n"] == 1

    publish_write(frozenset({"Post"}))

    assert (await users(Req("/users")))["n"] == 2


def test_the_watched_set_is_introspectable() -> None:
    @cached(invalidate_on=[User, Post])
    async def report(request: Any) -> dict:
        return {}

    assert report.invalidated_by == frozenset({"User", "Post"})


@pytest.mark.asyncio
async def test_a_committed_flush_publishes_the_models_it_wrote(
    registry: Registry, database: FakeDatabase
) -> None:
    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    database.connection.script("INSERT", [[7, None]])
    session = Session(registry, "write")
    session.add(User(email="a@b.c", name="Ada"))
    await session.flush()

    assert seen == [frozenset({"User"})]


@pytest.mark.asyncio
async def test_a_rolled_back_transaction_publishes_nothing(
    registry: Registry, database: FakeDatabase
) -> None:
    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    database.connection.script("INSERT", [[7, None]])
    session = Session(registry, "write")
    with pytest.raises(RuntimeError, match="deliberate"):
        async with session.begin():
            session.add(User(email="a@b.c", name="Ada"))
            await session.flush()
            raise RuntimeError("deliberate")

    assert seen == []


@pytest.mark.asyncio
async def test_a_rolled_back_savepoint_publishes_nothing(
    registry: Registry, database: FakeDatabase
) -> None:
    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    database.connection.script("INSERT", [[7, None]])
    session = Session(registry, "write")
    async with session.begin():
        with pytest.raises(RuntimeError, match="deliberate"):
            async with session.begin():
                session.add(User(email="a@b.c", name="Ada"))
                await session.flush()
                raise RuntimeError("deliberate")

    assert seen == []


@pytest.mark.asyncio
async def test_a_rolled_back_savepoint_keeps_the_enclosing_writes(
    registry: Registry, database: FakeDatabase
) -> None:
    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    database.connection.script("INSERT", [[7, None]])
    session = Session(registry, "write")
    async with session.begin():
        session.add(User(email="a@b.c", name="Ada"))
        await session.flush()
        with pytest.raises(RuntimeError, match="deliberate"):
            async with session.begin():
                session.add(Post(title="draft", author_id=7))
                await session.flush()
                raise RuntimeError("deliberate")

    assert seen == [frozenset({"User"})]


@pytest.mark.asyncio
async def test_writes_inside_a_transaction_publish_once_on_commit(
    registry: Registry, database: FakeDatabase
) -> None:
    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    database.connection.script("INSERT", [[7, None]])
    session = Session(registry, "write")
    async with session.begin():
        session.add(User(email="a@b.c", name="Ada"))
        await session.flush()
        session.add(User(email="b@b.c", name="Bea"))
        await session.flush()

    assert seen == [frozenset({"User"})]


@pytest.mark.asyncio
async def test_a_flush_with_nothing_pending_publishes_nothing(
    registry: Registry, database: FakeDatabase
) -> None:
    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    await Session(registry, "write").flush()
    assert seen == []


@pytest.mark.asyncio
async def test_collection_is_skipped_when_nobody_is_listening(
    registry: Registry, database: FakeDatabase
) -> None:
    from wreath import _orm_events

    _orm_events._subscribers.clear()
    _orm_events._subscriber_keys.clear()
    _orm_events._bridges.clear()
    database.connection.script("INSERT", [[7, None]])
    session = Session(registry, "write")
    session.add(User(email="a@b.c", name="Ada"))
    await session.flush()
    assert session._written == frozenset()


class FakeBus:
    """Record publications and deliver them to connected peers."""

    def __init__(self, *, fail: bool = False) -> None:
        self.handlers: list[tuple[str, Any]] = []
        self.published: list[tuple[str, Any]] = []
        self.peers: list[FakeBus] = []
        self.fail = fail

    def subscribe(self, channel: str, **kwargs: Any):
        def register(handler):
            self.handlers.append((channel, handler))
            return handler

        return register

    async def publish(self, channel: str, payload: Any, **kwargs: Any) -> None:
        if self.fail:
            raise ConnectionError("the bus is down")
        self.published.append((channel, payload))
        for bus in (self, *self.peers):
            for subscribed, handler in bus.handlers:
                if subscribed == channel:
                    await handler(_BusMessage(channel, payload))
class _BusMessage:
    def __init__(self, channel: str, payload: Any) -> None:
        self.channel = channel
        self.payload = payload


def _elsewhere(bus: FakeBus) -> FakeBus:
    """Connect a peer without registering a second process-global bridge."""
    peer = FakeBus()
    peer.peers = [bus]
    return peer


async def _commit_elsewhere(peer: FakeBus, *models: str) -> None:
    await peer.publish(WRITE_CHANNEL, {"models": sorted(models), "origin": "worker-a"})


@pytest.mark.asyncio
async def test_a_local_write_is_carried_to_the_bus() -> None:
    bus = FakeBus()
    invalidate_across_workers(bus)

    publish_write(frozenset({"User", "Post"}))
    await asyncio.sleep(0)

    (channel, payload), = bus.published
    assert channel == WRITE_CHANNEL
    assert payload["models"] == ["Post", "User"]
    assert payload["origin"]


@pytest.mark.asyncio
async def test_a_write_on_one_worker_clears_the_cache_on_another() -> None:
    worker_b = FakeBus()
    invalidate_across_workers(worker_b)
    worker_a = _elsewhere(worker_b)

    calls = 0

    @cached(invalidate_on=[User])
    async def report(request: Any) -> dict:
        nonlocal calls
        calls += 1
        return {"n": calls}

    assert (await report(Req()))["n"] == 1
    assert (await report(Req()))["n"] == 1

    await _commit_elsewhere(worker_a, "User")

    assert (await report(Req()))["n"] == 2


@pytest.mark.asyncio
async def test_another_workers_write_to_an_unwatched_model_changes_nothing() -> None:
    worker_b = FakeBus()
    invalidate_across_workers(worker_b)
    worker_a = _elsewhere(worker_b)

    calls = 0

    @cached(invalidate_on=[User])
    async def report(request: Any) -> dict:
        nonlocal calls
        calls += 1
        return {"n": calls}

    await report(Req())
    await _commit_elsewhere(worker_a, "Post")
    assert (await report(Req()))["n"] == 1


@pytest.mark.asyncio
async def test_a_worker_ignores_the_echo_of_its_own_broadcast() -> None:
    bus = FakeBus()
    invalidate_across_workers(bus)

    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    publish_write(frozenset({"User"}))
    await asyncio.sleep(0)

    assert seen == [frozenset({"User"})]


@pytest.mark.asyncio
async def test_a_received_write_is_never_rebroadcast() -> None:
    worker_b = FakeBus()
    invalidate_across_workers(worker_b)
    worker_a = _elsewhere(worker_b)

    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    await _commit_elsewhere(worker_a, "User")
    for _ in range(5):
        await asyncio.sleep(0)

    assert seen == [frozenset({"User"})]
    assert worker_b.published == []


@pytest.mark.asyncio
async def test_a_bus_that_is_down_cannot_fail_a_committed_write() -> None:
    bus = FakeBus(fail=True)
    invalidate_across_workers(bus)

    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    publish_write(frozenset({"User"}))
    await asyncio.sleep(0)

    assert seen == [frozenset({"User"})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    ["not-a-dict", {}, {"models": "User"}, {"models": []}, {"models": [1, 2]}],
)
async def test_a_malformed_bus_payload_is_ignored(payload: Any) -> None:
    bus = FakeBus()
    invalidate_across_workers(bus)
    peer = _elsewhere(bus)

    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    await peer.publish(WRITE_CHANNEL, payload)
    assert seen == []


@pytest.mark.asyncio
async def test_closing_a_bridge_stops_carrying_writes() -> None:
    bus = FakeBus()
    bridge = invalidate_across_workers(bus)
    bridge.close()

    publish_write(frozenset({"User"}))
    await asyncio.sleep(0)

    assert bus.published == []


def test_a_worker_with_only_a_bridge_still_collects_names() -> None:
    from wreath import _orm_events

    _orm_events._subscribers.clear()
    _orm_events._subscriber_keys.clear()
    _orm_events._bridges.clear()
    assert not has_subscribers()

    invalidate_across_workers(FakeBus())
    assert has_subscribers()


@pytest.mark.asyncio
async def test_a_write_outside_the_event_loop_is_not_carried() -> None:
    bus = FakeBus()
    invalidate_across_workers(bus)

    await asyncio.to_thread(publish_write, frozenset({"User"}))
    assert bus.published == []


@pytest.mark.asyncio
async def test_a_write_refreshes_a_snapshot_cache() -> None:
    loads = 0

    def load_llamas() -> dict[int, str]:
        nonlocal loads
        loads += 1
        return {1: f"Bea v{loads}"}

    cache: SnapshotCache = SnapshotCache()
    await cache.refresh(load_llamas)
    assert cache.get(1) == "Bea v1"

    refresh_on(cache, [User], load=load_llamas)
    publish_write(frozenset({"User"}))
    await asyncio.sleep(0)

    assert cache.get(1) == "Bea v2"
    assert cache.generation == 2


@pytest.mark.asyncio
async def test_an_unwatched_model_leaves_the_snapshot_alone() -> None:
    cache: SnapshotCache = SnapshotCache()
    await cache.refresh(lambda: {1: "Bea"})
    refresh_on(cache, [User], load=lambda: {1: "reloaded"})

    publish_write(frozenset({"Post"}))
    await asyncio.sleep(0)

    assert cache.get(1) == "Bea"


@pytest.mark.asyncio
async def test_a_snapshot_refresh_reaches_every_worker() -> None:
    worker_b = FakeBus()
    invalidate_across_workers(worker_b)
    worker_a = _elsewhere(worker_b)

    cache: SnapshotCache = SnapshotCache()
    await cache.refresh(lambda: {1: "Bea"})
    refresh_on(cache, [User], load=lambda: {1: "renamed"})

    await _commit_elsewhere(worker_a, "User")
    await asyncio.sleep(0)

    assert cache.get(1) == "renamed"


@pytest.mark.asyncio
async def test_a_failing_loader_keeps_the_previous_generation() -> None:
    def explode() -> dict:
        raise RuntimeError("the database is down")

    cache: SnapshotCache = SnapshotCache()
    await cache.refresh(lambda: {1: "Bea"})
    refresh_on(cache, [User], load=explode)

    publish_write(frozenset({"User"}))
    await asyncio.sleep(0)

    assert cache.get(1) == "Bea"
    assert cache.generation == 1


@pytest.mark.asyncio
async def test_a_failing_loader_is_countable_not_silent() -> None:
    def explode() -> dict:
        raise RuntimeError("the database is down")

    cache: SnapshotCache = SnapshotCache()
    await cache.refresh(lambda: {1: "Bea"})
    watch = refresh_on(cache, [User], load=explode)
    assert watch.refresh_errors == 0
    assert watch.last_error is None

    publish_write(frozenset({"User"}))
    await asyncio.sleep(0)
    publish_write(frozenset({"User"}))
    await asyncio.sleep(0)

    assert watch.refresh_errors == 2
    assert isinstance(watch.last_error, RuntimeError)
    assert str(watch.last_error) == "the database is down"
    assert cache.get(1) == "Bea"


@pytest.mark.asyncio
async def test_a_successful_reload_counts_nothing() -> None:
    cache: SnapshotCache = SnapshotCache()
    await cache.refresh(lambda: {1: "Bea"})
    watch = refresh_on(cache, [User], load=lambda: {1: "renamed"})

    publish_write(frozenset({"User"}))
    await asyncio.sleep(0)

    assert cache.get(1) == "renamed"
    assert watch.refresh_errors == 0
    assert watch.last_error is None


@pytest.mark.asyncio
async def test_refresh_on_returns_a_handle_that_can_be_stopped() -> None:
    cache: SnapshotCache = SnapshotCache()
    await cache.refresh(lambda: {1: "Bea"})
    stop = refresh_on(cache, [User], load=lambda: {1: "renamed"})
    stop()

    publish_write(frozenset({"User"}))
    await asyncio.sleep(0)
    assert cache.get(1) == "Bea"
