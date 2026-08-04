"""A name has one owner, and a question reaches whoever owns it.

The tree already answered "exactly one process owns this right now" six ways --
an advisory lock, a job lease and fence, a store claim, a pass row lock, a
single-use `DELETE ... RETURNING`, and `wreath.streams` borrowing the queue's
fence outright. What none of them could express is the *other* half of a
stateful gateway: a message aimed at whoever holds a name, sent from a worker
that does not know which one that is.

These tests use a fake database that runs the real statements' *semantics* --
not their SQL -- because the interesting properties are concurrency ones and a
container is not available on every machine. The SQL itself is exercised
against real PostgreSQL by the store suite it is built on; what is proved here
is the contract:

* a second holder is refused while a lease is live, and admitted once it lapses;
* the fence moves on a handover and **not** on a renewal;
* `release` is scoped to the owner, so a lapsed holder cannot delete its
  successor's row on the way out;
* `ask` reaches the holder on another worker, carries the answer back, reports
  a handler failure as a failure rather than as a timeout, and refuses a name
  nobody holds without waiting for the deadline.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from wreath._correlation import TooManyPending
from wreath.entity import EntityRegistry, NotHeld, Ownership, Unanswered
from wreath.temporal import seconds


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, by: float) -> None:
        self.now += by


class FakeOwnershipDB:
    """The `hold`/`release`/`holder` statements' semantics over a dict.

    One row per name: `(owner, fence, expires)`. Every branch below mirrors a
    clause of the real statement, and the mapping is stated so a divergence is
    a visible edit rather than a silent drift:

    * `hold` -> the `ON CONFLICT DO UPDATE ... WHERE expired OR same owner`
    * the `CASE` -> fence bumps only when the owner changes
    * `release` -> `DELETE ... WHERE name = $1 AND owner = $2`
    * `holder` -> `SELECT ... WHERE expires > clock_timestamp()`
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.rows: dict[str, dict[str, Any]] = {}

    async def acquire(self, workload: str) -> FakeOwnershipDB:
        return self

    async def execute(self, sql: str, *args: Any) -> str:
        """The ad-hoc `release_many` statement, which is not a prepared one.

        It cannot be: the predicate is `IN ($2, $3, ...)` with one placeholder
        per name, because the driver refuses to bind a sequence.
        """
        assert "DELETE FROM" in sql and " IN (" in sql, sql
        owner, *names = args
        gone = [k for k in names if k in self.rows and self.rows[k]["owner"] == owner]
        for key in gone:
            del self.rows[key]
        return f"DELETE {len(gone)}"

    async def release(self, workload: str, connection: Any) -> None:
        return None


class FakeStatement:
    def __init__(self, db: FakeOwnershipDB, name: str, lease: float) -> None:
        self.db, self.name, self.lease = db, name, lease

    async def fetchrow(self, *args: Any) -> Any:
        now = self.db.clock.now
        if self.name == "hold":
            key, owner = args
            row = self.db.rows.get(key)
            if row is None:
                self.db.rows[key] = {"owner": owner, "fence": 1, "expires": now + self.lease}
                return {"fence": 1}
            live = row["expires"] > now
            if live and row["owner"] != owner:
                return None  # the WHERE clause matched nothing
            if row["owner"] != owner:
                row["fence"] += 1  # a handover
            row["owner"] = owner
            row["expires"] = now + self.lease
            return {"fence": row["fence"]}
        if self.name == "renew_all":
            owner = args[0]
            kept = []
            for key, row in self.db.rows.items():
                if row["owner"] == owner and row["expires"] > now:
                    row["expires"] = now + self.lease
                    kept.append({"name": key})
            return kept
        if self.name == "holder":
            row = self.db.rows.get(args[0])
            if row is None or row["expires"] <= now:
                return None
            return {"owner": row["owner"]}
        raise AssertionError(f"unexpected statement {self.name!r}")

    async def fetch(self, *args: Any) -> Any:
        return await self.fetchrow(*args)

    async def execute(self, *args: Any) -> str:
        if self.name == "release_all":
            owner = args[0]
            gone = [k for k, r in self.db.rows.items() if r["owner"] == owner]
            for key in gone:
                del self.db.rows[key]
            return f"DELETE {len(gone)}"
        if self.name == "release":
            key, owner = args
            row = self.db.rows.get(key)
            if row is not None and row["owner"] == owner:
                del self.db.rows[key]
                return "DELETE 1"
            return "DELETE 0"
        raise AssertionError(f"unexpected statement {self.name!r}")


class FakeStore:
    """Stands in for `PostgresStore`, handing out the fake statements above.

    Counts what was asked for, because "the message was dropped before any
    ownership lookup" is a property some tests need to assert and the slotted
    `Ownership` cannot be monkeypatched to observe it.
    """

    def __init__(self, db: FakeOwnershipDB, lease: float) -> None:
        self.db, self.lease = db, lease
        self.asked: list[str] = []

    def statement(self, name: str) -> FakeStatement:
        self.asked.append(name)
        return FakeStatement(self.db, name, self.lease)


def _ownership(db: FakeOwnershipDB, *, owner: str, lease: float = 30.0) -> Ownership:
    own = Ownership(db, owner=owner, lease=lease)
    # Swap the store, not the statements: `PostgresStore` is slotted, and the
    # real SQL it builds is covered against a live database by the store suite.
    # Every other code path here -- key composition, fence reading, owner
    # scoping on release -- stays real.
    own._store = FakeStore(db, lease)  # type: ignore[assignment]
    return own


# --- ownership -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_name_admits_one_holder() -> None:
    clock = FakeClock()
    db = FakeOwnershipDB(clock)
    first, second = _ownership(db, owner="a"), _ownership(db, owner="b")

    assert await first.hold("device:1") is not None
    assert await second.hold("device:1") is None


@pytest.mark.asyncio
async def test_a_renewal_does_not_move_the_fence() -> None:
    # The jobs rule: a fence that moved on renewal would invalidate the holder's
    # own in-flight work, which is the opposite of what a fence is for.
    clock = FakeClock()
    own = _ownership(FakeOwnershipDB(clock), owner="a")
    first = await own.hold("device:1")
    clock.advance(5)
    again = await own.hold("device:1")
    assert first is not None and again is not None
    assert first.fence == again.fence


@pytest.mark.asyncio
async def test_a_handover_moves_the_fence() -> None:
    clock = FakeClock()
    db = FakeOwnershipDB(clock)
    first, second = _ownership(db, owner="a"), _ownership(db, owner="b")

    before = await first.hold("device:1")
    clock.advance(31)  # the lease lapses
    after = await second.hold("device:1")

    assert before is not None and after is not None
    assert after.fence == before.fence + 1
    assert after.owner == "b"


@pytest.mark.asyncio
async def test_a_lapsed_holder_cannot_release_its_successors_row() -> None:
    # The shutdown race a naive DELETE loses: a worker whose lease expired
    # tidies up on the way out and removes the new holder's ownership.
    clock = FakeClock()
    db = FakeOwnershipDB(clock)
    first, second = _ownership(db, owner="a"), _ownership(db, owner="b")

    await first.hold("device:1")
    clock.advance(31)
    await second.hold("device:1")

    assert await first.release("device:1") is False
    assert await second.holder("device:1") == "b"


@pytest.mark.asyncio
async def test_released_is_immediately_free() -> None:
    clock = FakeClock()
    db = FakeOwnershipDB(clock)
    first, second = _ownership(db, owner="a"), _ownership(db, owner="b")

    await first.hold("device:1")
    assert await first.release("device:1") is True
    assert await second.hold("device:1") is not None


@pytest.mark.asyncio
async def test_holder_ignores_a_lapsed_row() -> None:
    clock = FakeClock()
    own = _ownership(FakeOwnershipDB(clock), owner="a")
    await own.hold("device:1")
    assert await own.holder("device:1") == "a"
    clock.advance(31)
    assert await own.holder("device:1") is None


def test_a_lease_must_be_positive() -> None:
    # Matched on the *whole* message, not on "positive". `Keyed` raises
    # `ValueError("ttl must be positive")` a few lines later for the same input,
    # so a looser match passes with this guard deleted -- which `wreath mutant`
    # duly reported as a survivor when this test was first written.
    with pytest.raises(ValueError, match="^a lease must be positive$"):
        Ownership(FakeOwnershipDB(FakeClock()), lease=0)


def test_a_negative_lease_is_refused_by_the_same_guard() -> None:
    with pytest.raises(ValueError, match="^a lease must be positive$"):
        Ownership(FakeOwnershipDB(FakeClock()), lease=-1)


def test_a_lease_accepts_a_duration() -> None:
    own = Ownership(FakeOwnershipDB(FakeClock()), lease=seconds(45))
    assert own._lease == 45.0


def test_an_explicit_owner_is_used_rather_than_a_generated_one() -> None:
    # The `owner or uuid4().hex` fallback: two workers given the same identity
    # must be the same holder, which is what makes a rolling restart able to
    # resume rather than contend.
    assert Ownership(FakeOwnershipDB(FakeClock()), owner="node-a").owner == "node-a"


def test_a_generated_owner_is_unique_per_process() -> None:
    first = Ownership(FakeOwnershipDB(FakeClock()))
    second = Ownership(FakeOwnershipDB(FakeClock()))
    assert first.owner and second.owner and first.owner != second.owner


@pytest.mark.asyncio
async def test_release_reports_false_when_the_driver_reports_no_status() -> None:
    # `isinstance(status, str)` guards the `.endswith` beside it. A driver that
    # returns None -- a test double, a backend that reports nothing, which
    # `PostgresStore.purge_count` already allows for -- would otherwise raise
    # AttributeError from inside a shutdown path.
    class SilentStatement(FakeStatement):
        async def execute(self, *args: Any) -> Any:
            return None

    class SilentStore(FakeStore):
        def statement(self, name: str) -> FakeStatement:
            self.asked.append(name)
            return SilentStatement(self.db, name, self.lease)

    own = Ownership(FakeOwnershipDB(FakeClock()), owner="a")
    own._store = SilentStore(FakeOwnershipDB(FakeClock()), 30.0)  # type: ignore[assignment]
    assert await own.release("device:1") is False


@pytest.mark.asyncio
async def test_releasing_a_name_nobody_holds_reports_false() -> None:
    # `DELETE 0` is not an error, but it is not a release either -- the caller
    # asked whether it gave something up, and it did not.
    own = _ownership(FakeOwnershipDB(FakeClock()), owner="a")
    assert await own.release("device:absent") is False


# --- ask and answer ------------------------------------------------------------------


class FakeBus:
    """Ephemeral fan-out: every subscriber on the bus sees every message.

    That is `pg_notify`'s actual behaviour, and it is what makes the filtering
    in `_receive` load-bearing rather than decorative -- a worker sees answers
    to questions it never asked.
    """

    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def subscribe(self, channel: str, **kw: Any) -> Any:
        def register(handler: Any) -> Any:
            self.handlers.append(handler)
            return handler

        return register

    async def publish(self, channel: str, payload: Any, **kw: Any) -> None:
        # Delivered on their own tasks, as a real fan-out would be, so a
        # handler that awaits cannot deadlock the publisher.
        for handler in list(self.handlers):
            asyncio.get_running_loop().create_task(handler(payload))


def _registry(db: FakeOwnershipDB, bus: FakeBus, owner: str) -> EntityRegistry:
    registry = EntityRegistry(db, bus)
    registry._ownership = _ownership(db, owner=owner)
    return registry


@pytest.mark.asyncio
async def test_a_question_reaches_the_holder_on_another_worker() -> None:
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    holder = _registry(db, bus, "a")
    asker = _registry(db, bus, "b")

    seen: list[tuple[str, Any]] = []

    @holder.answers("device")
    async def answer(name: str, payload: Any) -> Any:
        seen.append((name, payload))
        return {"soc": 82}

    await holder.hold("device", "abc")
    reply = await asker.ask("device", "abc", {"op": "read"}, timeout=seconds(2))

    assert reply == {"soc": 82}
    assert seen == [("abc", {"op": "read"})]


@pytest.mark.asyncio
async def test_a_name_nobody_holds_refuses_without_waiting() -> None:
    # Checked against the ownership table before publishing: an ephemeral
    # publish to nobody is a silent no-op, and waiting the full deadline for
    # that is a bad diagnosis of a simple fact.
    db, bus = FakeOwnershipDB(FakeClock()), FakeBus()
    asker = _registry(db, bus, "b")
    with pytest.raises(NotHeld):
        await asker.ask("device", "missing", {}, timeout=seconds(30))
    assert asker.unrouted == 1


@pytest.mark.asyncio
async def test_a_handler_failure_is_reported_rather_than_timed_out() -> None:
    db, bus = FakeOwnershipDB(FakeClock()), FakeBus()
    holder, asker = _registry(db, bus, "a"), _registry(db, bus, "b")

    @holder.answers("device")
    async def answer(name: str, payload: Any) -> Any:
        raise RuntimeError("the socket is closed")

    await holder.hold("device", "abc")
    with pytest.raises(NotHeld, match="the socket is closed"):
        await asker.ask("device", "abc", {}, timeout=seconds(2))


@pytest.mark.asyncio
async def test_a_holder_that_never_answers_times_out() -> None:
    db, bus = FakeOwnershipDB(FakeClock()), FakeBus()
    holder, asker = _registry(db, bus, "a"), _registry(db, bus, "b")

    @holder.answers("device")
    async def answer(name: str, payload: Any) -> Any:
        await asyncio.sleep(10)
        return None

    await holder.hold("device", "abc")
    with pytest.raises(Unanswered):
        await asker.ask("device", "abc", {}, timeout=0.05)


@pytest.mark.asyncio
async def test_a_worker_that_lost_the_name_does_not_answer_for_it() -> None:
    # The ownership table is the authority, not the question. Between the
    # asker's check and the message arriving, the name can move.
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    stale, current, asker = (
        _registry(db, bus, "a"),
        _registry(db, bus, "b"),
        _registry(db, bus, "c"),
    )
    answered: list[str] = []

    for registry, tag in ((stale, "stale"), (current, "current")):

        @registry.answers("device")
        async def answer(name: str, payload: Any, tag: str = tag) -> Any:
            answered.append(tag)
            return tag

    await stale.hold("device", "abc")
    clock.advance(31)
    await current.hold("device", "abc")

    assert await asker.ask("device", "abc", {}, timeout=seconds(2)) == "current"
    assert answered == ["current"]


@pytest.mark.asyncio
async def test_too_many_questions_in_flight_refuses() -> None:
    # The pending map is memory a remote caller would otherwise control.
    db, bus = FakeOwnershipDB(FakeClock()), FakeBus()
    holder = _registry(db, bus, "a")
    asker = EntityRegistry(db, bus, max_pending=1)
    asker._ownership = _ownership(db, owner="b")

    @holder.answers("device")
    async def answer(name: str, payload: Any) -> Any:
        await asyncio.sleep(5)
        return None

    await holder.hold("device", "abc")
    slow = asyncio.create_task(asker.ask("device", "abc", {}, timeout=seconds(3)))
    await asyncio.sleep(0)  # let it register in `_pending`

    with pytest.raises(TooManyPending):
        await asker.ask("device", "abc", {}, timeout=seconds(3))
    assert asker.refusals == 1

    slow.cancel()
    with pytest.raises(asyncio.CancelledError):
        await slow


@pytest.mark.asyncio
async def test_an_answer_to_somebody_elses_question_is_ignored() -> None:
    # One channel carries the whole registry, so every worker sees every reply.
    # Settling on a correlation this registry never issued would hand one
    # caller another caller's answer.
    db, bus = FakeOwnershipDB(FakeClock()), FakeBus()
    asker = _registry(db, bus, "b")
    await asker._receive({"correlation": "not-ours", "reply": {"soc": 1}})
    assert asker.outstanding == 0


@pytest.mark.asyncio
async def test_a_second_reply_to_one_question_is_ignored() -> None:
    # At-most-once fan-out does not promise at-most-one *sender*: a superseded
    # holder can answer late. Settling a done future would raise InvalidState.
    db, bus = FakeOwnershipDB(FakeClock()), FakeBus()
    asker = _registry(db, bus, "b")
    async with asker._pending.slot(identifier="c1") as (_key, waiter):
        waiter.set_result({"first": True})
        await asker._receive({"correlation": "c1", "reply": {"second": True}})
        assert waiter.result() == {"first": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "not a mapping",
        {"reply": {"soc": 1}},                        # no correlation
        {"correlation": 7, "reply": {}},              # correlation not a string
        {"correlation": "c", "ask": 1, "kind": "device", "name": "abc"},
        {"correlation": "c", "ask": "device:abc", "kind": 1, "name": "abc"},
        # Unhashable: without the `kind` check this reaches `_answers.get(...)`
        # and raises TypeError inside a bus handler, taking the subscriber down.
        {"correlation": "c", "ask": "device:abc", "kind": ["device"], "name": "abc"},
        {"correlation": "c", "ask": "device:abc", "kind": "device", "name": 1},
    ],
)
async def test_a_malformed_message_is_dropped_rather_than_raising(message: Any) -> None:
    # Every worker sees every message, so a payload from a newer build -- or
    # from something else publishing on the channel -- must not take a
    # subscriber down.
    #
    # The `holder` count is what makes this test able to tell. Each field check
    # is one clause of an `or`, so dropping any single one still refuses via the
    # others and no handler runs either way -- the assertion has to be that the
    # message was dropped *before* the ownership lookup, not merely that nothing
    # was answered. That is also a real property: a flood of malformed messages
    # must not become a flood of database round trips.
    db, bus = FakeOwnershipDB(FakeClock()), FakeBus()
    registry = _registry(db, bus, "a")
    store: FakeStore = registry._ownership._store  # type: ignore[assignment]

    @registry.answers("device")
    async def answer(name: str, payload: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("a malformed message reached the handler")

    await registry._receive(message)
    assert store.asked == []


@pytest.mark.asyncio
async def test_a_question_for_an_unregistered_kind_is_ignored() -> None:
    # A registry serving devices must not answer a question about sessions
    # merely because it happens to hold the name.
    db, bus = FakeOwnershipDB(FakeClock()), FakeBus()
    registry = _registry(db, bus, "a")
    await registry.hold("session", "abc")
    published: list[Any] = []
    registry._bus = type("B", (), {"publish": lambda _s, _c, p, **k: published.append(p)})()
    await registry._receive(
        {"correlation": "c", "ask": "session:abc", "kind": "session", "name": "abc"}
    )
    assert published == []


def test_one_answer_per_kind() -> None:
    db, bus = FakeOwnershipDB(FakeClock()), FakeBus()
    registry = _registry(db, bus, "a")

    @registry.answers("device")
    async def first(name: str, payload: Any) -> Any:
        return None

    with pytest.raises(ValueError, match="already has an answer"):

        @registry.answers("device")
        async def second(name: str, payload: Any) -> Any:
            return None


# --- registration ---------------------------------------------------------------------


def test_a_registered_registry_contributes_its_table_to_the_schema() -> None:
    """An `EntityRegistry` built by hand is a table nothing creates.

    `Wreath.schema_components` collects a claim by *asking* the registries it
    walks, and a user-held object is in none of them -- so `wreath_entity` was
    emitted by `wreath schema sql` and applied by nothing, which is the exact
    defect that mechanism exists to prevent. `app.entities()` registers it, and
    `EntityRegistry.schema_owners` is what carries the claim from the `Ownership`
    that actually owns the table.
    """
    from wreath import Wreath

    app = Wreath()
    app.postgres("main", dsn="postgresql://u:p@localhost/db")
    app.messaging("events", database="main")
    app.entities(database="main", bus="events")
    assert "entity" in {component.name for component in app.schema_components()}


def test_entities_refuses_an_unknown_database_or_bus() -> None:
    from wreath import Wreath

    app = Wreath()
    app.postgres("main", dsn="postgresql://u:p@localhost/db")
    with pytest.raises(KeyError, match="unknown message bus"):
        app.entities(database="main", bus="absent")
    app.messaging("events", database="main")
    with pytest.raises(KeyError, match="unknown database"):
        app.entities(database="absent", bus="events")


def test_a_duplicate_registry_name_is_refused() -> None:
    from wreath import Wreath

    app = Wreath()
    app.postgres("main", dsn="postgresql://u:p@localhost/db")
    app.messaging("events", database="main")
    app.entities(database="main", bus="events")
    with pytest.raises(ValueError, match="duplicate entity registry"):
        app.entities(database="main", bus="events")


# --- the renewal service --------------------------------------------------------------
#
# A lease without a keep-alive is a lease every caller writes a loop for, once
# per name, at one round trip each. These pin that the loop is the registry's,
# that it costs a bounded number of statements however much a worker holds, and
# that the two lifecycle edges -- a name lost under load, and a clean shutdown --
# behave differently from each other on purpose.


def _counting_registry(db: FakeOwnershipDB, bus: FakeBus, owner: str, **kw: Any):
    registry = EntityRegistry(db, bus, **kw)
    registry._ownership = _ownership(db, owner=owner)
    return registry


@pytest.mark.asyncio
async def test_a_held_name_is_renewed_by_the_tick() -> None:
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    registry = _counting_registry(db, bus, "a")

    await registry.hold("device", "abc")
    clock.advance(20)  # inside the 30s lease
    await registry._tick()
    clock.advance(20)  # would have lapsed without the renewal

    assert await registry._ownership.holder("device:abc") == "a"


@pytest.mark.asyncio
async def test_renewal_costs_one_statement_however_many_names_are_held() -> None:
    # The property that makes this scale: a statement per name per tick is a
    # round trip per entity per tick, which is the cost that stops it.
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    registry = _counting_registry(db, bus, "a")
    for index in range(50):
        await registry.hold("device", str(index))

    store: FakeStore = registry._ownership._store  # type: ignore[assignment]
    store.asked.clear()
    await registry._tick()

    assert store.asked == ["renew_all"]


@pytest.mark.asyncio
async def test_a_name_taken_while_held_is_noticed_and_counted() -> None:
    # There is no heartbeat, so the renewal result is the only way to learn it.
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    mine = _counting_registry(db, bus, "a")
    theirs = _ownership(db, owner="b")
    seen: list[str] = []
    mine._on_lost = seen.append

    await mine.hold("device", "abc")
    clock.advance(31)                       # the lease lapses
    await theirs.hold("device:abc")         # another worker takes it
    await mine._tick()

    assert mine.lost == 1
    assert seen == ["device:abc"]
    assert mine.held == frozenset()


@pytest.mark.asyncio
async def test_a_lost_name_is_dropped_locally_so_release_cannot_steal_it() -> None:
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    mine = _counting_registry(db, bus, "a")
    theirs = _ownership(db, owner="b")

    await mine.hold("device", "abc")
    clock.advance(31)
    await theirs.hold("device:abc")
    await mine._tick()
    await mine.release("device", "abc")

    assert await theirs.holder("device:abc") == "b"


@pytest.mark.asyncio
async def test_holding_releases_on_exit() -> None:
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    registry = _counting_registry(db, bus, "a")

    async with registry.holding("device", "abc") as lease:
        assert lease is not None
        assert registry.held == frozenset({"device:abc"})
    assert registry.held == frozenset()
    assert await registry._ownership.holder("device:abc") is None


@pytest.mark.asyncio
async def test_holding_yields_none_when_another_worker_has_the_name() -> None:
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    first, second = _counting_registry(db, bus, "a"), _counting_registry(db, bus, "b")

    await first.hold("device", "abc")
    async with second.holding("device", "abc") as lease:
        assert lease is None
    # And the failed attempt must not have released the real holder's row.
    assert await first._ownership.holder("device:abc") == "a"


@pytest.mark.asyncio
async def test_holding_releases_even_when_the_body_raises() -> None:
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    registry = _counting_registry(db, bus, "a")

    with pytest.raises(RuntimeError):
        async with registry.holding("device", "abc"):
            raise RuntimeError("the socket went away")
    assert await registry._ownership.holder("device:abc") is None


@pytest.mark.asyncio
async def test_a_grace_period_keeps_the_name_for_a_reconnect() -> None:
    # A brief drop must not hand the name to another worker and then take it
    # back: that is two handovers and two fence bumps for something that never
    # moved. Re-entering inside the window renews instead.
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    registry = _counting_registry(db, bus, "a")

    async with registry.holding("device", "abc", grace=seconds(5)) as first:
        assert first is not None
    assert await registry._ownership.holder("device:abc") == "a"

    async with registry.holding("device", "abc", grace=seconds(5)) as again:
        assert again is not None
        assert again.fence == first.fence  # renewed, never handed over


@pytest.mark.asyncio
async def test_a_grace_period_expires_on_a_later_tick() -> None:
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    registry = _counting_registry(db, bus, "a")

    async with registry.holding("device", "abc", grace=0.001):
        pass
    await asyncio.sleep(0.01)  # past the grace, once, rather than polling for it
    await registry._tick()

    assert registry.held == frozenset()
    assert await registry._ownership.holder("device:abc") is None


@pytest.mark.asyncio
async def test_draining_releases_everything_in_one_statement() -> None:
    # A rolling deploy that waited out the leases would park every name for a
    # full lease. A clean shutdown knows better and says so.
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    registry = _counting_registry(db, bus, "a")
    for index in range(20):
        await registry.hold("device", str(index))

    store: FakeStore = registry._ownership._store  # type: ignore[assignment]
    store.asked.clear()
    await registry.drain(deadline=0.0)

    assert store.asked == ["release_all"]
    assert registry.held == frozenset()
    assert db.rows == {}


@pytest.mark.asyncio
async def test_a_tick_holding_nothing_asks_the_database_nothing() -> None:
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    registry = _counting_registry(db, bus, "a")
    store: FakeStore = registry._ownership._store  # type: ignore[assignment]
    store.asked.clear()

    await registry._tick()

    assert store.asked == []
    assert registry.renewals == 1


def test_the_renewal_interval_is_a_third_of_the_lease() -> None:
    # Two consecutive ticks can be lost to a slow database before a name is.
    registry = EntityRegistry(
        FakeOwnershipDB(FakeClock()), FakeBus(), lease=seconds(30)
    )
    assert registry._renew_every == pytest.approx(10.0)


# --- controls `wreath mutant` found nothing watching ----------------------------------


@pytest.mark.asyncio
async def test_a_failed_hold_releases_nothing() -> None:
    # `holding`'s finally is guarded on having actually got the lease. Without
    # that guard it calls release for a name it never held -- harmless only
    # because release is owner-scoped, which is the wrong reason for it to be
    # safe. The database should not be asked at all.
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    first, second = _counting_registry(db, bus, "a"), _counting_registry(db, bus, "b")

    await first.hold("device", "abc")
    store: FakeStore = second._ownership._store  # type: ignore[assignment]
    store.asked.clear()
    async with second.holding("device", "abc") as lease:
        assert lease is None
    assert store.asked == ["hold"]


@pytest.mark.asyncio
async def test_releasing_nothing_asks_the_database_nothing() -> None:
    own = _ownership(FakeOwnershipDB(FakeClock()), owner="a")
    store: FakeStore = own._store  # type: ignore[assignment]
    store.asked.clear()
    assert await own.release_many([]) == 0
    assert store.asked == []


@pytest.mark.asyncio
async def test_a_grace_does_not_resurrect_a_name_already_lost() -> None:
    # `_let_go` defers a release only for a name still held. Without that check
    # a name taken by another worker is put back into the renewal set, and this
    # worker then renews a row it does not own.
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    mine = _counting_registry(db, bus, "a")
    theirs = _ownership(db, owner="b")

    await mine.hold("device", "abc")
    clock.advance(31)
    await theirs.hold("device:abc")
    await mine._tick()                       # notices the loss, drops it locally
    await mine._let_go("device:abc", seconds(5))

    assert mine.held == frozenset()


@pytest.mark.asyncio
async def test_draining_a_registry_that_never_started_does_not_raise() -> None:
    # `_woken` is created by `start`. A registry drained without one -- a
    # startup that failed before this service came up -- must still release.
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    registry = _counting_registry(db, bus, "a")
    await registry.hold("device", "abc")

    await registry.drain(deadline=0.0)

    assert db.rows == {}


@pytest.mark.asyncio
async def test_a_grace_that_has_not_elapsed_survives_the_tick() -> None:
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    registry = _counting_registry(db, bus, "a")

    async with registry.holding("device", "abc", grace=seconds(60)):
        pass
    await registry._tick()

    assert registry.held == frozenset({"device:abc"})
    assert await registry._ownership.holder("device:abc") == "a"


@pytest.mark.asyncio
async def test_a_tick_with_no_expired_grace_issues_no_release() -> None:
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    registry = _counting_registry(db, bus, "a")
    await registry.hold("device", "abc")

    store: FakeStore = registry._ownership._store  # type: ignore[assignment]
    store.asked.clear()
    await registry._tick()

    assert store.asked == ["renew_all"]


@pytest.mark.asyncio
async def test_a_successful_renewal_reports_no_loss() -> None:
    # The renewal diff decides what was lost. Without its filter every held
    # name is reported lost on every tick, which is an alarm that never stops.
    clock = FakeClock()
    db, bus = FakeOwnershipDB(clock), FakeBus()
    registry = _counting_registry(db, bus, "a")
    for index in range(3):
        await registry.hold("device", str(index))

    await registry._tick()

    assert registry.lost == 0
    assert len(registry.held) == 3
