"""ORM-driven response-cache invalidation.

A TTL is a guess. Wreath owns the ORM and the response cache, so it can do the
exact thing instead: when a model is written and the transaction commits, the
caches that named that model clear. These tests pin the two properties that
make it safe -- it fires on commit, and it does *not* fire on rollback.

The second half pins the cross-worker half: the same announcement, carried over
the message bus, so a write on worker A clears worker B. The properties that
matter there are that it does not echo, does not storm, and cannot fail a write.
"""

from __future__ import annotations

import asyncio
import gc
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
    """Subscribers are process-global; keep tests from leaking into each other."""
    from wreath import _orm_events

    subscribers = list(_orm_events._subscribers)
    bridges = list(_orm_events._bridges)
    yield
    _orm_events._subscribers[:] = subscribers
    _orm_events._bridges[:] = bridges


class Req:
    def __init__(self, path: str = "/report") -> None:
        self.method = "GET"
        self.path = path
        self.query_string = b""


# --- the event seam ----------------------------------------------------------


def test_subscribers_receive_written_model_names() -> None:
    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)
    publish_write(frozenset({"User"}))
    assert seen == [frozenset({"User"})]


def test_a_broken_subscriber_cannot_fail_a_committed_write() -> None:
    """The data is already durable; a bad cache listener must not raise."""
    def explode(written: frozenset[str]) -> None:
        raise RuntimeError("subscriber is broken")

    seen: list[frozenset[str]] = []
    subscribe_writes(explode)
    subscribe_writes(seen.append)

    publish_write(frozenset({"User"}))       # must not raise
    assert seen == [frozenset({"User"})]     # and the others still ran


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


# --- the cache decorator -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_write_to_a_watched_model_drops_the_cache() -> None:
    calls = 0

    @cached(invalidate_on=[User])
    async def report(request: Any) -> dict:
        nonlocal calls
        calls += 1
        return {"n": calls}

    assert (await report(Req()))["n"] == 1
    assert (await report(Req()))["n"] == 1        # served from cache

    publish_write(frozenset({"User"}))

    assert (await report(Req()))["n"] == 2        # recomputed


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
    assert (await report(Req()))["n"] == 1        # still cached


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
    """`invalidate_on` subscribes at *decoration* time and has no later moment
    to unsubscribe at, so the subscription is owned by the handler.

    A closure left in the process-global list would outlive every handler that
    ever existed, and -- worse than the memory -- `has_subscribers()` would stay
    true for the rest of the process, so the session's "collect nothing when
    nobody is listening" fast path would be dead in any app that caches at all.
    """
    from wreath import _orm_events

    _orm_events._subscribers.clear()
    _orm_events._bridges.clear()

    def define() -> None:
        @cached(invalidate_on=[User])
        async def report(request: Any) -> dict:
            return {}

        assert len(_orm_events._subscribers) == 1     # listening while it lives

    define()
    gc.collect()

    assert _orm_events._subscribers == []             # and gone with it
    assert not has_subscribers()


def test_a_cached_handler_that_is_still_referenced_keeps_listening() -> None:
    """The ordinary case: a handler registered on an app lives for the process."""
    from wreath import _orm_events

    _orm_events._subscribers.clear()
    _orm_events._bridges.clear()

    @cached(invalidate_on=[User])
    async def report(request: Any) -> dict:
        return {}

    routes = [report]                 # what an app's router holds
    gc.collect()

    assert len(_orm_events._subscribers) == 1
    assert has_subscribers()
    assert routes                     # the reference is the point


@pytest.mark.asyncio
async def test_a_shared_store_is_one_invalidation_domain() -> None:
    """Documented coupling, pinned so it cannot change silently.

    `_on_write` clears the whole store, so a write to *any* watched model of
    *any* handler sharing that store drops everything in it -- including
    entries for endpoints that named different models. Sharing a store buys one
    budget and one invalidation surface; the second half of that is a cost.
    """
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
    assert (await users(Req("/users")))["n"] == 1      # cached

    publish_write(frozenset({"Post"}))                # nothing users named

    assert (await users(Req("/users")))["n"] == 2     # cleared anyway


def test_the_watched_set_is_introspectable() -> None:
    @cached(invalidate_on=[User, Post])
    async def report(request: Any) -> dict:
        return {}

    assert report.invalidated_by == frozenset({"User", "Post"})


# --- end to end through a session --------------------------------------------


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
    """The write never happened, so nothing cached is stale.

    Invalidating from inside a transaction that then rolls back would evict
    correct data for a write that was undone.
    """
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
    """The same rule one level deeper, where it used to be broken.

    An inner ``begin()`` is a SAVEPOINT. Rolling it back used to leave its model
    names on the session, so the outer commit announced a write that had been
    undone. Over-invalidating is wasteful rather than wrong, but the invariant
    is "a rolled-back write invalidates nothing", at every depth.
    """
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
    """The other half: a savepoint discards only what happened inside it.

    Clearing the whole pending set on a savepoint rollback would be the
    opposite defect -- a committed write that never invalidates anything, which
    serves stale data rather than merely too little cache.
    """
    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    database.connection.script("INSERT", [[7, None]])
    session = Session(registry, "write")
    async with session.begin():
        session.add(User(email="a@b.c", name="Ada"))
        await session.flush()                      # kept: outside the savepoint
        with pytest.raises(RuntimeError, match="deliberate"):
            async with session.begin():
                session.add(Post(title="draft", author_id=7))
                await session.flush()              # discarded with the savepoint
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

    # One announcement for the transaction, not one per flush.
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
    """An app with no cache subscribers pays one bool read, not a set build."""
    from wreath import _orm_events

    _orm_events._subscribers.clear()
    _orm_events._bridges.clear()
    database.connection.script("INSERT", [[7, None]])
    session = Session(registry, "write")
    session.add(User(email="a@b.c", name="Ada"))
    await session.flush()          # must not raise, and collects nothing
    assert session._written == frozenset()


# --- across workers ----------------------------------------------------------


class FakeBus:
    """Records subscriptions and published payloads; can wire N workers.

    ``peers`` models the one property that matters: an ephemeral ``NOTIFY``
    reaches every listener on the channel, including the process that sent it.
    """

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
    """A second worker's handle on the same channel.

    Only one bridge can exist per process -- the subscriber registry is
    process-global, as workers are -- so the *other* worker is modelled as a bus
    that publishes onto the channel this one listens to. That is exactly what it
    looks like from here.
    """
    peer = FakeBus()
    peer.peers = [bus]
    return peer


async def _commit_elsewhere(peer: FakeBus, *models: str) -> None:
    """Another worker committed a write to ``models``."""
    await peer.publish(WRITE_CHANNEL, {"models": sorted(models), "origin": "worker-a"})


@pytest.mark.asyncio
async def test_a_local_write_is_carried_to_the_bus() -> None:
    bus = FakeBus()
    invalidate_across_workers(bus)

    publish_write(frozenset({"User", "Post"}))
    await asyncio.sleep(0)          # remote delivery is a task, not inline

    (channel, payload), = bus.published
    assert channel == WRITE_CHANNEL
    assert payload["models"] == ["Post", "User"]     # sorted: a stable wire form
    assert payload["origin"]


@pytest.mark.asyncio
async def test_a_write_on_one_worker_clears_the_cache_on_another() -> None:
    """The whole point: worker A writes, worker B stops serving stale data."""
    worker_b = FakeBus()
    invalidate_across_workers(worker_b)
    worker_a = _elsewhere(worker_b)

    calls = 0

    @cached(invalidate_on=[User])       # the cache lives here, on worker B
    async def report(request: Any) -> dict:
        nonlocal calls
        calls += 1
        return {"n": calls}

    assert (await report(Req()))["n"] == 1
    assert (await report(Req()))["n"] == 1        # served from cache

    await _commit_elsewhere(worker_a, "User")

    assert (await report(Req()))["n"] == 2        # recomputed, without a TTL


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
    assert (await report(Req()))["n"] == 1        # still cached


@pytest.mark.asyncio
async def test_a_worker_ignores_the_echo_of_its_own_broadcast() -> None:
    """NOTIFY comes back to the sender; applying it twice is wasted work."""
    bus = FakeBus()
    invalidate_across_workers(bus)

    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    publish_write(frozenset({"User"}))
    await asyncio.sleep(0)

    assert seen == [frozenset({"User"})]          # once, not twice


@pytest.mark.asyncio
async def test_a_received_write_is_never_rebroadcast() -> None:
    """A bridge that relays what it receives is how a fan-out storm starts."""
    worker_b = FakeBus()
    invalidate_across_workers(worker_b)
    worker_a = _elsewhere(worker_b)

    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    await _commit_elsewhere(worker_a, "User")
    for _ in range(5):
        await asyncio.sleep(0)

    assert seen == [frozenset({"User"})]          # applied here
    assert worker_b.published == []               # and not sent back out


@pytest.mark.asyncio
async def test_a_bus_that_is_down_cannot_fail_a_committed_write() -> None:
    """The row is durable. A broken NOTIFY must not surface as an error."""
    bus = FakeBus(fail=True)
    invalidate_across_workers(bus)

    seen: list[frozenset[str]] = []
    subscribe_writes(seen.append)

    publish_write(frozenset({"User"}))            # must not raise
    await asyncio.sleep(0)

    assert seen == [frozenset({"User"})]          # local invalidation still ran


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
    """A worker whose own caches are empty must still tell the others."""
    from wreath import _orm_events

    _orm_events._subscribers.clear()
    _orm_events._bridges.clear()
    assert not has_subscribers()

    invalidate_across_workers(FakeBus())
    assert has_subscribers()


@pytest.mark.asyncio
async def test_a_write_outside_the_event_loop_is_not_carried() -> None:
    """Defensive: the ORM only publishes from async code, but never raise."""
    bus = FakeBus()
    invalidate_across_workers(bus)

    await asyncio.to_thread(publish_write, frozenset({"User"}))
    assert bus.published == []


# --- snapshot caches ride the same event --------------------------------------


@pytest.mark.asyncio
async def test_a_write_refreshes_a_snapshot_cache() -> None:
    """The other half of W1: reference data reloads instead of being dropped."""
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
    """A write on worker A reloads worker B's reference data, over one bus."""
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
    """A reload that cannot run must not leave readers with nothing."""
    def explode() -> dict:
        raise RuntimeError("the database is down")

    cache: SnapshotCache = SnapshotCache()
    await cache.refresh(lambda: {1: "Bea"})
    refresh_on(cache, [User], load=explode)

    publish_write(frozenset({"User"}))     # must not raise
    await asyncio.sleep(0)

    assert cache.get(1) == "Bea"
    assert cache.generation == 1


@pytest.mark.asyncio
async def test_a_failing_loader_is_countable_not_silent() -> None:
    """The other half of the test above: staying up must not mean staying quiet.

    A snapshot cache degrades upwards -- every read still succeeds against the
    last good generation, so a permanently broken loader moves no other signal.
    Without a count there is nothing anywhere that says the data is stale.
    """
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
    # And the reason the count is the only signal: the reads never stopped.
    assert cache.get(1) == "Bea"


@pytest.mark.asyncio
async def test_a_successful_reload_counts_nothing() -> None:
    """Guard against 'fixed it by counting everything'."""
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
