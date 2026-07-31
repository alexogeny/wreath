"""Single-use second-factor challenges on `wreath.store`.

A ceremony challenge is consumed exactly once, by the user who began it. The
property that matters is *atomic* consumption: a read followed by a delete lets
two concurrent completions both conclude they were first, which is precisely
what a single-use challenge exists to prevent. So the tests that carry this
file are the concurrency ones, and they need a real PostgreSQL -- a sequential
simulation proves nothing about a race.

The user binding is part of the consuming statement rather than a check after
it. Checking after consuming would let anyone holding a handle burn the
rightful user's ceremony, which is a denial of service on someone else's login.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from wreath._secondfactor import (
    MemoryChallengeStore,
    PostgresChallengeStore,
    challenge_declaration,
)

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
requires_db = pytest.mark.skipif(
    not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)"
)


def _table(suffix: str = "chal") -> str:
    """One table per xdist worker.

    Plain assignment from the worker name, never `setdefault`: the controller
    imports conftest during collection and spawns workers with its own
    environment, so a default would give every worker the controller's name.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "solo")
    return f"w2fa_{worker}_{suffix}"


# --- the declaration, no database --------------------------------------------


async def test_the_declaration_names_its_table_and_payload() -> None:
    declaration = challenge_declaration(table=_table())
    statements = declaration.statements()
    assert any("CREATE TABLE IF NOT EXISTS" in s for s in statements)
    assert any("user_id" in s for s in statements)
    assert any("kind" in s for s in statements)


async def test_the_declaration_offers_a_schema_component() -> None:
    """Wreath owns its own furniture: this table never enters a user's artifact."""
    component = challenge_declaration(table=_table()).component(
        name="second_factor_challenges"
    )
    assert component.name == "second_factor_challenges"
    assert any(step.version == 1 for step in component.steps)


# --- the in-memory twin -------------------------------------------------------


async def test_memory_consumes_a_challenge_exactly_once() -> None:
    store = MemoryChallengeStore()
    await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60)
    assert await store.consume("h1", user_id="u1", kind="webauthn-register") == {"c": "x"}
    # The second attempt is the replay this whole design exists to refuse.
    assert await store.consume("h1", user_id="u1", kind="webauthn-register") is None


async def test_memory_refuses_another_user_without_consuming() -> None:
    """The rightful user's ceremony survives someone else's attempt on it."""
    store = MemoryChallengeStore()
    await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60)
    assert await store.consume("h1", user_id="u2", kind="webauthn-register") is None
    assert await store.consume("h1", user_id="u1", kind="webauthn-register") == {"c": "x"}


async def test_memory_refuses_another_kind_without_consuming() -> None:
    """A registration challenge must not answer an assertion."""
    store = MemoryChallengeStore()
    await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60)
    assert await store.consume("h1", user_id="u1", kind="webauthn-assert") is None
    assert await store.consume("h1", user_id="u1", kind="webauthn-register") == {"c": "x"}


async def test_memory_refuses_an_expired_challenge() -> None:
    ticks = [0.0]
    store = MemoryChallengeStore(clock=lambda: ticks[0])
    await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60)
    ticks[0] = 61.0
    assert await store.consume("h1", user_id="u1", kind="webauthn-register") is None


async def test_memory_discards_a_challenge() -> None:
    store = MemoryChallengeStore()
    await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60)
    await store.discard("h1")
    assert await store.consume("h1", user_id="u1", kind="webauthn-register") is None


async def test_memory_consumption_is_atomic_across_concurrent_tasks() -> None:
    """Synchronous between the read and the delete, so no task interleaves.

    This is the in-process counterpart of the single DELETE the PostgreSQL twin
    uses; it is weaker evidence than the database race below, because a single
    event loop cannot interleave without an await, but it is the property the
    memory store is claiming and so it is worth pinning.
    """
    store = MemoryChallengeStore()
    await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60)
    results = await asyncio.gather(
        *(store.consume("h1", user_id="u1", kind="webauthn-register") for _ in range(16))
    )
    assert sum(1 for r in results if r is not None) == 1


# --- against a live database --------------------------------------------------


async def _database():
    from wreath.postgres import Database

    db = Database("main", _DSN)
    await db.start()
    return db


async def _store(table: str) -> tuple:
    db = await _database()
    connection = await db.acquire("write")
    try:
        await connection.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        await db.release("write", connection)
    store = PostgresChallengeStore(db, table=table)
    connection = await db.acquire("write")
    try:
        for statement in store.declaration.statements():
            await connection.execute(statement)
    finally:
        await db.release("write", connection)
    return db, store


async def _drop(db, table: str) -> None:
    connection = await db.acquire("write")
    try:
        await connection.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        await db.release("write", connection)
    await db.stop()


@requires_db
async def test_postgres_consumes_a_challenge_exactly_once() -> None:
    table = _table("once")
    db, store = await _store(table)
    try:
        await store.put(
            "h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60
        )
        assert await store.consume("h1", user_id="u1", kind="webauthn-register") == {"c": "x"}
        assert await store.consume("h1", user_id="u1", kind="webauthn-register") is None
    finally:
        await _drop(db, table)


@requires_db
async def test_postgres_refuses_another_user_without_consuming() -> None:
    table = _table("user")
    db, store = await _store(table)
    try:
        await store.put(
            "h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60
        )
        assert await store.consume("h1", user_id="u2", kind="webauthn-register") is None
        # Still there for whoever began it -- the attempt cost them nothing.
        assert await store.consume("h1", user_id="u1", kind="webauthn-register") == {"c": "x"}
    finally:
        await _drop(db, table)


@requires_db
async def test_postgres_refuses_an_expired_challenge_and_purges_it() -> None:
    table = _table("exp")
    db, store = await _store(table)
    try:
        await store.put(
            "h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=0.001
        )
        await asyncio.sleep(0.05)
        assert await store.consume("h1", user_id="u1", kind="webauthn-register") is None
        assert await store.purge_count() == 1
    finally:
        await _drop(db, table)


@requires_db
async def test_two_simultaneous_completions_leave_exactly_one_winner() -> None:
    """The test this plan exists for.

    Sixteen tasks race for one challenge against a real PostgreSQL, each on its
    own pooled connection. Exactly one DELETE can return a row; the other
    fifteen are refused as replays. A read-then-delete implementation fails
    this, which is why it is written against the database rather than simulated.
    """
    table = _table("race")
    db, store = await _store(table)
    try:
        await store.put(
            "h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60
        )
        results = await asyncio.gather(
            *(store.consume("h1", user_id="u1", kind="webauthn-register") for _ in range(16))
        )
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        assert winners[0] == {"c": "x"}
    finally:
        await _drop(db, table)
