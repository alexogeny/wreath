"""Real PostgreSQL proof that a partial index round-trips through the catalog.

The risk this file exists for is not a crash. It is that ``detect`` reports drift
on an index it created moments earlier, on every run, forever -- because
PostgreSQL stores a predicate as a node tree and ``pg_get_expr`` deparses it back
to a canonical text that need not match what was written. So every test here
applies the migration and then asks ``detect`` **twice**: the second answer must
be empty.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from wreath.migrations import (
    _build_native_artifact,
    apply_single_artifact,
    detect_single,
    generate_single_plan,
)
from wreath.orm import Mapped, Model, column
from wreath.orm._index_predicate import RESERVED_WORDS, render_predicate
from wreath.orm.registry import Registry
from wreath.orm.table import all_of, eq, index, is_not_null, one_of
from wreath.orm.types import Bool, Int64, Text
from wreath.postgres import connect

pytestmark = [pytest.mark.asyncio, pytest.mark.network]
_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")


async def connection() -> Any:
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for real partial-index tests")
    return await connect(_DSN)


class _Database:
    name = "partial-index-test"


async def _apply(registry: Registry, db: Any) -> None:
    generation = await generate_single_plan(registry, db)
    artifact = _build_native_artifact(
        migration_id=uuid.uuid4().bytes,
        parent_checksum=bytes(32),
        source_fingerprint=generation.actual_fingerprint,
        target_fingerprint=generation.desired_fingerprint,
        operation_tape=generation.diff.tape,
        named_plan=generation.plan.tape,
        sql_tape=generation.sql.tape,
    )
    await apply_single_artifact(registry, db, artifact.data)


async def _predicates_in(db: Any, schema: str) -> list[str]:
    rows = await db.fetch(
        "SELECT pg_get_expr(i.indpred, i.indrelid) AS pred "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1::text AND i.indpred IS NOT NULL",
        schema,
    )
    return sorted(str(row[0]) for row in rows)


async def _roundtrip(model_factory: Any) -> tuple[list[str], Any, Any]:
    """Apply a model, then detect twice. The second detect must be clean."""
    schema = f"wreath_partial_{uuid.uuid4().hex[:12]}"
    model = model_factory(schema)
    registry = Registry(_Database(), [model], validate_schema="off")
    db = await connection()
    try:
        await db.execute(f'CREATE SCHEMA "{schema}"')
        await _apply(registry, db)
        first = await detect_single(registry, db)
        second = await detect_single(registry, db)
        predicates = await _predicates_in(db, schema)
        return predicates, first, second
    finally:
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()


async def test_an_equality_predicate_round_trips() -> None:
    def build(schema: str) -> Any:
        class Job(Model, table="jobs", schema=schema):
            id: Mapped[int] = column(Int64, primary_key=True)
            queue: Mapped[str] = column(Text)
            state: Mapped[str] = column(Text)
            _claim = index("queue", where=eq("state", "ready"))

        return Job

    predicates, first, second = await _roundtrip(build)
    assert predicates == ["(state = 'ready'::text)"]
    assert first.current, "the schema should be current right after applying it"
    assert second.current, "a second detect must not rediscover the same index"


async def test_a_unique_partial_index_round_trips() -> None:
    """The shape the exactly-once guarantee rests on."""

    def build(schema: str) -> Any:
        class Message(Model, table="messages", schema=schema):
            id: Mapped[int] = column(Int64, primary_key=True)
            channel: Mapped[str] = column(Text)
            dedup_key: Mapped[str] = column(Text, nullable=True)
            _dedup = index(
                "channel", "dedup_key", unique=True, where=is_not_null("dedup_key")
            )

        return Message

    predicates, first, second = await _roundtrip(build)
    assert predicates == ["(dedup_key IS NOT NULL)"]
    assert first.current and second.current


async def test_an_in_predicate_round_trips_as_any_array() -> None:
    def build(schema: str) -> Any:
        class Delivery(Model, table="deliveries", schema=schema):
            id: Mapped[int] = column(Int64, primary_key=True)
            next_attempt_at: Mapped[int] = column(Int64)
            state: Mapped[str] = column(Text)
            _ready = index(
                "next_attempt_at", where=one_of("state", ["pending", "retry_wait"])
            )

        return Delivery

    predicates, first, second = await _roundtrip(build)
    # PostgreSQL deparses IN as = ANY (ARRAY[...]); if wreath emitted IN here the
    # second detect would see a different string and want to rebuild the index.
    assert predicates == ["(state = ANY (ARRAY['pending'::text, 'retry_wait'::text]))"]
    assert first.current and second.current


async def test_a_conjunction_and_an_integer_round_trip() -> None:
    def build(schema: str) -> Any:
        class Task(Model, table="tasks", schema=schema):
            id: Mapped[int] = column(Int64, primary_key=True)
            state: Mapped[str] = column(Text)
            tries: Mapped[int] = column(Int64)
            _stuck = index("id", where=all_of(eq("state", "ready"), eq("tries", 0)))

        return Task

    predicates, first, second = await _roundtrip(build)
    assert predicates == ["((state = 'ready'::text) AND (tries = 0))"]
    assert first.current and second.current


async def test_a_boolean_and_a_reserved_word_column_round_trip() -> None:
    def build(schema: str) -> Any:
        class Row(Model, table="rows", schema=schema):
            id: Mapped[int] = column(Int64, primary_key=True)
            group: Mapped[str] = column(Text)
            archived: Mapped[bool] = column(Bool)
            _live = index("id", where=all_of(eq("archived", False), eq("group", "a")))

        return Row

    predicates, first, second = await _roundtrip(build)
    # "group" is reserved, so PostgreSQL quotes it and so must wreath.
    assert predicates == ["((archived = false) AND (\"group\" = 'a'::text))"]
    assert first.current and second.current


async def test_two_predicates_over_one_column_set_are_two_indexes() -> None:
    """The digest in the object name is what keeps these apart."""

    def build(schema: str) -> Any:
        class Split(Model, table="splits", schema=schema):
            id: Mapped[int] = column(Int64, primary_key=True)
            state: Mapped[str] = column(Text)
            _ready = index("id", where=eq("state", "ready"))
            _done = index("id", where=eq("state", "done"))

        return Split

    predicates, first, second = await _roundtrip(build)
    assert predicates == ["(state = 'done'::text)", "(state = 'ready'::text)"]
    assert first.current and second.current


async def test_the_reserved_word_list_matches_the_server() -> None:
    """A version bump that adds a keyword must not silently change quoting."""
    db = await connection()
    try:
        rows = await db.fetch(
            "SELECT word FROM pg_get_keywords() WHERE catcode IN ('R', 'T')"
        )
    finally:
        await db.close()
    assert {str(row[0]) for row in rows} == set(RESERVED_WORDS)


async def test_the_renderer_agrees_with_pg_get_expr_verbatim() -> None:
    """Render locally, create the index from that text, read it back unchanged.

    This is the property the whole design rests on, stated without the migration
    machinery in the way: what wreath writes is already what PostgreSQL says.
    """
    schema = f"wreath_render_{uuid.uuid4().hex[:12]}"
    db = await connection()

    class Sample(Model, table="samples", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        state: Mapped[str] = column(Text)
        note: Mapped[str] = column(Text, nullable=True)
        tries: Mapped[int] = column(Int64)
        archived: Mapped[bool] = column(Bool)

    registry = Registry(_Database(), [Sample], validate_schema="off")
    spec = registry.spec_for(Sample)
    columns = {item.database_name: item for item in spec.columns}
    cases = [
        eq("state", "ready"),
        eq("state", "it's"),
        eq("tries", 3),
        eq("archived", True),
        is_not_null("note"),
        one_of("state", ["a", "b", "c"]),
        all_of(eq("state", "ready"), is_not_null("note")),
    ]
    try:
        await db.execute(f'CREATE SCHEMA "{schema}"')
        await db.execute(
            f'CREATE TABLE "{schema}".samples '
            "(id bigint primary key, state text, note text, tries bigint, archived boolean)"
        )
        for ordinal, predicate in enumerate(cases):
            rendered = render_predicate(predicate, columns, "Sample")
            await db.execute(
                f'CREATE INDEX idx_{ordinal} ON "{schema}".samples (id) '
                f"WHERE {rendered}"
            )
            stored = await db.fetchval(
                "SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_index i "
                "JOIN pg_class ic ON ic.oid = i.indexrelid "
                "JOIN pg_class c ON c.oid = i.indrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = $1::text AND ic.relname = $2::text",
                schema,
                f"idx_{ordinal}",
            )
            assert stored == rendered, f"{predicate!r} rendered {rendered} stored {stored}"
    finally:
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()
