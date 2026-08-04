"""`wreath.entity`'s statements, against a real server.

`tests/test_entity.py` proves the *contract* — one holder, a fence that moves on
handover and not on renewal, an owner-scoped release — against a fake whose
branches mirror each clause of the real statement. What it cannot prove is that
the statements are valid SQL, that `ON CONFLICT ... DO UPDATE ... WHERE` behaves
the way the fake assumes, or that two workers racing for one name genuinely
serialise. Those need PostgreSQL.

The concurrency cases here are the reason this file exists. A claim built as a
read followed by a write passes every single-threaded test ever written and
admits two winners under load; the only way to know the `WHERE` clause is doing
the work is to run it against a server with two callers pushing at once.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from wreath.entity import Ownership
from wreath.postgres import Database, PoolConfig

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.network,
    pytest.mark.skipif(not _DSN, reason="set WREATH_TEST_POSTGRES_DSN for live entity tests"),
]


async def _database(label: str = "entity_live") -> Database:
    database = Database(
        name=label,
        dsn=_DSN or "",
        pools={"write": PoolConfig(min_size=1, max_size=6),
               "read": PoolConfig(min_size=1, max_size=6)},
    )
    await database.start()
    return database


async def _fresh(database: Database, **kw: object) -> Ownership:
    """An `Ownership` over a table of its own, created for this test."""
    table = f"entity_live_{uuid.uuid4().hex[:10]}"
    own = Ownership(database, table=table, **kw)  # type: ignore[arg-type]
    connection = await database.acquire("write")
    try:
        for statement in own._store.declaration.statements():
            await connection.execute(statement)
    finally:
        await database.release("write", connection)
    return own


@pytest.fixture
async def database():
    db = await _database()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def worker():
    """A factory for simulated peer workers, each with its own pool.

    **One `Database` per worker, not one shared.** `PostgresStore` names a
    prepared statement `{prefix}_{name}_{table}` and refuses a duplicate
    registration, so two `Ownership` objects over one table on one `Database`
    collide -- the collision guard doing exactly its job. That is not a
    limitation to work around: two workers really are two processes with two
    pools, and a fixture that shared one would be testing a shape production
    never has.
    """
    made: list[Database] = []

    async def build(table: str, **kw: object) -> Ownership:
        db = await _database(f"entity_peer_{len(made)}")
        made.append(db)
        return Ownership(db, table=table, **kw)  # type: ignore[arg-type]

    try:
        yield build
    finally:
        for db in made:
            await db.stop()


# --- the statements are valid, and mean what the fake assumed -------------------------


async def test_the_declared_ddl_applies(database) -> None:
    own = await _fresh(database)
    assert await own.holder("device:1") is None


async def test_a_claim_returns_a_lease_and_a_second_holder_is_refused(database, worker) -> None:
    first = await _fresh(database)
    second = await worker(first._store.declaration.table)

    lease = await first.hold("device:1")
    assert lease is not None and lease.fence >= 1
    assert await second.hold("device:1") is None
    assert await first.holder("device:1") == first.owner


async def test_a_renewal_keeps_the_fence(database) -> None:
    own = await _fresh(database)
    first = await own.hold("device:1")
    again = await own.hold("device:1")
    assert first is not None and again is not None
    assert first.fence == again.fence


async def test_a_handover_after_expiry_bumps_the_fence(database, worker) -> None:
    # A one-second lease, so the expiry is real rather than simulated.
    first = await _fresh(database, lease=1.0)
    second = await worker(first._store.declaration.table, lease=1.0)

    before = await first.hold("device:1")
    await asyncio.sleep(1.2)
    after = await second.hold("device:1")

    assert before is not None and after is not None
    assert after.fence == before.fence + 1
    assert await second.holder("device:1") == second.owner


async def test_release_is_scoped_to_the_owner(database, worker) -> None:
    # The shutdown race: a worker whose lease lapsed tidies up on the way out
    # and must not delete its successor's row.
    first = await _fresh(database, lease=1.0)
    second = await worker(first._store.declaration.table, lease=1.0)

    await first.hold("device:1")
    await asyncio.sleep(1.2)
    await second.hold("device:1")

    assert await first.release("device:1") is False
    assert await second.holder("device:1") == second.owner


async def test_an_expired_lease_stops_naming_a_holder(database) -> None:
    own = await _fresh(database, lease=1.0)
    await own.hold("device:1")
    assert await own.holder("device:1") == own.owner
    await asyncio.sleep(1.2)
    assert await own.holder("device:1") is None


# --- the batch statements -------------------------------------------------------------


async def test_renew_all_extends_every_held_name_in_one_statement(database) -> None:
    own = await _fresh(database, lease=2.0)
    for index in range(20):
        await own.hold(f"device:{index}")

    await asyncio.sleep(1.0)
    kept = await own.renew_all()
    assert len(kept) == 20

    # Past the original deadline; still held, because the renewal moved it.
    await asyncio.sleep(1.5)
    assert await own.holder("device:0") == own.owner


async def test_renew_all_omits_a_name_another_worker_took(database, worker) -> None:
    # The only way to learn a name was lost: there is no heartbeat.
    mine = await _fresh(database, lease=1.0)
    theirs = await worker(mine._store.declaration.table, lease=30.0)

    await mine.hold("device:1")
    await mine.hold("device:2")
    await asyncio.sleep(1.2)
    await theirs.hold("device:1")

    kept = await mine.renew_all()
    assert "device:1" not in kept


async def test_release_all_drops_only_this_workers_rows(database, worker) -> None:
    mine = await _fresh(database)
    theirs = await worker(mine._store.declaration.table)

    await mine.hold("device:1")
    await mine.hold("device:2")
    await theirs.hold("device:3")

    assert await mine.release_all() == 2
    assert await theirs.holder("device:3") == theirs.owner


async def test_release_many_is_owner_scoped_too(database, worker) -> None:
    mine = await _fresh(database)
    theirs = await worker(mine._store.declaration.table)

    await mine.hold("device:1")
    await theirs.hold("device:2")

    assert await mine.release_many(["device:1", "device:2"]) == 1
    assert await theirs.holder("device:2") == theirs.owner


# --- the property a single-threaded test cannot prove ---------------------------------


async def test_a_contested_name_admits_exactly_one_winner(database, worker) -> None:
    """Twenty workers claim one name at once; one row comes back.

    This is what the single `INSERT ... ON CONFLICT DO UPDATE ... WHERE` buys.
    A read-then-write claim passes every sequential test and admits two winners
    here, which is the failure the whole design exists to prevent.
    """
    seed = await _fresh(database)
    table = seed._store.declaration.table
    workers = [await worker(table) for _ in range(8)]

    leases = await asyncio.gather(*(w.hold("device:contested") for w in workers))
    winners = [lease for lease in leases if lease is not None]

    assert len(winners) == 1
    assert await seed.holder("device:contested") == winners[0].owner


async def test_concurrent_claims_on_distinct_names_all_succeed(database, worker) -> None:
    # The contention above must come from the name, not from the table.
    seed = await _fresh(database)
    table = seed._store.declaration.table
    workers = [await worker(table) for _ in range(8)]

    leases = await asyncio.gather(
        *(w.hold(f"device:{index}") for index, w in enumerate(workers))
    )
    assert all(lease is not None for lease in leases)


async def test_a_contested_handover_bumps_the_fence_once(database, worker) -> None:
    """Many workers race for a lapsed name; the fence moves by exactly one.

    A fence that moved per *attempt* rather than per handover would let a
    losing worker's completion match a fence nobody holds.
    """
    seed = await _fresh(database, lease=1.0)
    table = seed._store.declaration.table
    first = await worker(table, lease=1.0)

    before = await first.hold("device:1")
    assert before is not None
    await asyncio.sleep(1.2)

    contenders = [await worker(table, lease=30.0) for _ in range(6)]
    leases = await asyncio.gather(*(c.hold("device:1") for c in contenders))
    winners = [lease for lease in leases if lease is not None]

    assert len(winners) == 1
    assert winners[0].fence == before.fence + 1
