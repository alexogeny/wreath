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


async def test_the_declaration_names_its_table_and_payload() -> None:
    declaration = challenge_declaration(table=_table())
    statements = declaration.statements()
    assert any("CREATE TABLE IF NOT EXISTS" in s for s in statements)
    assert any("user_id" in s for s in statements)
    assert any("kind" in s for s in statements)


async def test_the_declaration_offers_a_schema_component() -> None:
    component = challenge_declaration(table=_table()).schema_claim("second_factor_challenges")
    assert component.name == "second_factor_challenges"
    assert any(step.version == 1 for step in component.steps)


async def test_memory_consumes_a_challenge_exactly_once() -> None:
    store = MemoryChallengeStore()
    await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60)
    assert await store.consume("h1", user_id="u1", kind="webauthn-register") == {"c": "x"}
    # The second attempt is the replay this whole design exists to refuse.
    assert await store.consume("h1", user_id="u1", kind="webauthn-register") is None


async def test_memory_capacity_refuses_without_evicting_a_live_challenge() -> None:
    store = MemoryChallengeStore(max_entries=1)
    await store.put(
        "victim",
        user_id="u1",
        kind="webauthn-register",
        payload={"c": "victim"},
        ttl=60,
    )

    with pytest.raises(OverflowError, match="capacity"):
        await store.put(
            "attacker",
            user_id="u2",
            kind="webauthn-register",
            payload={"c": "attacker"},
            ttl=60,
        )

    assert await store.consume("victim", user_id="u1", kind="webauthn-register") == {"c": "victim"}


async def test_memory_refuses_another_user_without_consuming() -> None:
    store = MemoryChallengeStore()
    await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60)
    assert await store.consume("h1", user_id="u2", kind="webauthn-register") is None
    assert await store.consume("h1", user_id="u1", kind="webauthn-register") == {"c": "x"}


async def test_memory_refuses_another_kind_without_consuming() -> None:
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


async def test_memory_peek_reads_without_consuming() -> None:
    store = MemoryChallengeStore()
    await store.put("h1", user_id="u1", kind="totp-enrolment", payload={"s": "x"}, ttl=60)
    row = await store.peek("h1")
    assert row is not None
    assert (row.user_id, row.kind, row.payload) == ("u1", "totp-enrolment", {"s": "x"})
    # Twice, because a read that spends on the second call is not a read.
    assert (await store.peek("h1")) is not None
    assert await store.consume("h1", user_id="u1", kind="totp-enrolment") == {"s": "x"}


async def test_memory_peek_answers_none_for_absent_and_expired() -> None:
    ticks = [0.0]
    store = MemoryChallengeStore(clock=lambda: ticks[0])
    assert await store.peek("nope") is None
    await store.put("h1", user_id="u1", kind="k", payload={}, ttl=60)
    ticks[0] = 61.0
    assert await store.peek("h1") is None


async def test_memory_peek_does_not_apply_the_binding() -> None:
    store = MemoryChallengeStore()
    await store.put("h1", user_id="u1", kind="k", payload={"c": "x"}, ttl=60)
    row = await store.peek("h1")
    assert row is not None and row.user_id == "u1"
    assert await store.consume("h1", user_id="u2", kind="k") is None


async def test_memory_peek_mutation_does_not_reach_the_stored_row() -> None:
    store = MemoryChallengeStore()
    await store.put("h1", user_id="u1", kind="k", payload={"c": "x"}, ttl=60)
    row = await store.peek("h1")
    assert row is not None
    row.payload["c"] = "tampered"
    assert await store.consume("h1", user_id="u1", kind="k") == {"c": "x"}


async def test_memory_discards_a_challenge() -> None:
    store = MemoryChallengeStore()
    await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60)
    await store.discard("h1")
    assert await store.consume("h1", user_id="u1", kind="webauthn-register") is None


async def test_memory_consumption_is_atomic_across_concurrent_tasks() -> None:
    store = MemoryChallengeStore()
    await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60)
    results = await asyncio.gather(
        *(store.consume("h1", user_id="u1", kind="webauthn-register") for _ in range(16))
    )
    assert sum(1 for r in results if r is not None) == 1


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
        await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60)
        assert await store.consume("h1", user_id="u1", kind="webauthn-register") == {"c": "x"}
        assert await store.consume("h1", user_id="u1", kind="webauthn-register") is None
    finally:
        await _drop(db, table)


@requires_db
async def test_postgres_refuses_another_user_without_consuming() -> None:
    table = _table("user")
    db, store = await _store(table)
    try:
        await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60)
        assert await store.consume("h1", user_id="u2", kind="webauthn-register") is None
        # Still there for whoever began it -- the attempt cost them nothing.
        assert await store.consume("h1", user_id="u1", kind="webauthn-register") == {"c": "x"}
    finally:
        await _drop(db, table)


@requires_db
async def test_postgres_peek_reads_without_consuming() -> None:
    table = _table("peek")
    db, store = await _store(table)
    try:
        await store.put("h1", user_id="u1", kind="totp-enrolment", payload={"s": "x"}, ttl=60)
        row = await store.peek("h1")
        assert row is not None
        assert (row.user_id, row.kind, row.payload) == ("u1", "totp-enrolment", {"s": "x"})
        # Still spendable afterwards: the read cost the ceremony nothing.
        assert await store.consume("h1", user_id="u1", kind="totp-enrolment") == {"s": "x"}
        assert await store.peek("h1") is None
    finally:
        await _drop(db, table)


@requires_db
async def test_postgres_peek_refuses_an_expired_row_like_consume_does() -> None:
    table = _table("peekexp")
    db, store = await _store(table)
    try:
        await store.put("h1", user_id="u1", kind="k", payload={"c": "x"}, ttl=0.001)
        await asyncio.sleep(0.05)
        assert await store.consume("h1", user_id="u1", kind="k") is None
        assert await store.peek("h1") is None
    finally:
        await _drop(db, table)


@requires_db
async def test_postgres_refuses_an_expired_challenge_and_purges_it() -> None:
    table = _table("exp")
    db, store = await _store(table)
    try:
        await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=0.001)
        await asyncio.sleep(0.05)
        assert await store.consume("h1", user_id="u1", kind="webauthn-register") is None
        assert await store.purge_count() == 1
    finally:
        await _drop(db, table)


@requires_db
async def test_two_simultaneous_completions_leave_exactly_one_winner() -> None:
    table = _table("race")
    db, store = await _store(table)
    try:
        await store.put("h1", user_id="u1", kind="webauthn-register", payload={"c": "x"}, ttl=60)
        results = await asyncio.gather(
            *(store.consume("h1", user_id="u1", kind="webauthn-register") for _ in range(16))
        )
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        assert winners[0] == {"c": "x"}
    finally:
        await _drop(db, table)
