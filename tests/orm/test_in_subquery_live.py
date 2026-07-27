"""`IN (SELECT ...)` against a real PostgreSQL, executed twice.

A subquery that *compiles* is not a subquery that *runs*, and the two failures
this file exists to catch only appear against a server:

* **Placeholder ordering.** The outer statement numbers parameters positionally
  and the subquery's WHERE is emitted in the middle of it. If the bind program
  walked the tree in a different order than the renderer emitted it, the values
  would be transposed -- and transposed values of the same type produce a
  perfectly valid query returning the wrong rows. No fake can catch that,
  because a fake does not care which value lands in which slot.
* **The second execution.** PostgreSQL infers parameter types when a statement
  is *prepared*, and wreath's plan cache means the first call prepares and every
  later call reuses. A shape that works once and fails forever after has reached
  a default code path in this repository before, so every query here runs twice
  and the second result is asserted, not just the first.

Skipped unless ``WREATH_TEST_POSTGRES_DSN`` points at a throwaway database.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from wreath.orm import Mapped, Model, Registry, column
from wreath.orm.session import Session
from wreath.orm.types import Int64, Text
from wreath.postgres import Database, PoolConfig

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
        reason="set WREATH_TEST_POSTGRES_DSN to run IN (SELECT ...) against a server",
    ),
    pytest.mark.asyncio,
]

#: One schema per xdist worker. Workers sharing a schema race on
#: `CREATE SCHEMA IF NOT EXISTS`, and PostgreSQL reports the race as a duplicate
#: key on `pg_namespace_nspname_index` -- a catalog error that reads like
#: anything except a test-isolation bug.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_in_subquery_{_WORKER}"


class Species(Model, table="species", schema=_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    protection: Mapped[str] = column(Text)


class Sighting(Model, table="sightings", schema=_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    species_id: Mapped[int] = column(Int64, references=Species.id)
    note: Mapped[str] = column(Text)


_DDL = (
    f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE',
    f'CREATE SCHEMA "{_SCHEMA}"',
    f'CREATE TABLE "{_SCHEMA}"."species" '
    "(id bigint PRIMARY KEY, protection text NOT NULL)",
    f'CREATE TABLE "{_SCHEMA}"."sightings" '
    "(id bigint PRIMARY KEY, species_id bigint NOT NULL, note text NOT NULL)",
    f"""INSERT INTO "{_SCHEMA}"."species" (id, protection) VALUES
        (1, 'open'), (2, 'open'), (3, 'sensitive'), (4, 'restricted')""",
    f"""INSERT INTO "{_SCHEMA}"."sightings" (id, species_id, note) VALUES
        (10, 1, 'a'), (11, 2, 'b'), (12, 3, 'c'), (13, 4, 'd'), (14, 1, 'e')""",
)


@pytest.fixture
async def registry() -> Any:
    database = Database(
        "in_subquery",
        os.environ["WREATH_TEST_POSTGRES_DSN"],
        pools={"write": PoolConfig(min_size=1, max_size=3)},
    )
    await database.start()
    connection = await database.acquire("write")
    try:
        for statement in _DDL:
            await connection.execute(statement)
    finally:
        await database.release("write", connection)
    built = Registry(database, [Species, Sighting], validate_schema="off")
    try:
        yield built
    finally:
        connection = await database.acquire("write")
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        finally:
            await database.release("write", connection)
        await database.stop()


async def _ids(registry: Any, query: Any) -> list[int]:
    session = Session(registry, "write")
    try:
        return sorted(row.id for row in await session.fetch(query))
    finally:
        await session.close()


async def test_in_subquery_returns_the_right_rows_twice(registry: Any) -> None:
    """The whole point: one statement, correct rows, and correct again prepared."""
    query = Sighting.select().where(
        Sighting.species_id.in_(
            Species.select(Species.id).where(Species.protection == "open")
        )
    )
    assert await _ids(registry, query) == [10, 11, 14]
    # Second execution: the plan is cached and the statement is now prepared.
    assert await _ids(registry, query) == [10, 11, 14]


async def test_not_in_subquery_returns_the_complement_twice(registry: Any) -> None:
    query = Sighting.select().where(
        Sighting.species_id.not_in(
            Species.select(Species.id).where(Species.protection == "open")
        )
    )
    assert await _ids(registry, query) == [12, 13]
    assert await _ids(registry, query) == [12, 13]


async def test_the_outer_and_inner_values_do_not_transpose(registry: Any) -> None:
    """Two bound values of the same type, one outside the subquery and one in.

    If the bind program walked the tree in a different order than the renderer
    emitted placeholders, these two would swap -- and because both are `text`,
    PostgreSQL would run the transposed query happily and return the wrong rows
    rather than raising. Asserting the *rows* is what catches it; asserting that
    the query merely executed would not.
    """
    query = Sighting.select().where(
        Sighting.note == "c",
        Sighting.species_id.in_(
            Species.select(Species.id).where(Species.protection == "sensitive")
        ),
    )
    assert await _ids(registry, query) == [12]
    assert await _ids(registry, query) == [12]

    # And the transposition would have matched nothing, so the assertion above
    # can actually fail: prove the swapped pairing is not also [12].
    swapped = Sighting.select().where(
        Sighting.note == "sensitive",
        Sighting.species_id.in_(Species.select(Species.id).where(Species.protection == "c")),
    )
    assert await _ids(registry, swapped) == []


async def test_two_subquery_shapes_do_not_share_a_cached_plan(registry: Any) -> None:
    """The plan cache keys on shape. Same table, different filter, different rows.

    Run in one session and in this order deliberately: if the second query hit
    the first one's cached plan, it would return the first one's rows and the
    assertion would fail with real data rather than an error.
    """
    open_only = Sighting.select().where(
        Sighting.species_id.in_(
            Species.select(Species.id).where(Species.protection == "open")
        )
    )
    restricted_only = Sighting.select().where(
        Sighting.species_id.in_(
            Species.select(Species.id).where(Species.protection == "restricted")
        )
    )
    assert await _ids(registry, open_only) == [10, 11, 14]
    assert await _ids(registry, restricted_only) == [13]
    assert await _ids(registry, open_only) == [10, 11, 14]


async def test_count_matches_the_page_it_describes(registry: Any) -> None:
    """`total` is only true if COUNT(*) filters through the subquery too."""
    from wreath.orm.compiler import compile_count

    query = Sighting.select().where(
        Sighting.species_id.in_(
            Species.select(Species.id).where(Species.protection == "open")
        )
    )
    sql, values, oids = compile_count(registry, query)
    session = Session(registry, "write")
    try:
        connection = await session._acquire()
        total = await connection.fetchval(sql, *values)
    finally:
        await session.close()
    assert total == len(await _ids(registry, query)) == 3
