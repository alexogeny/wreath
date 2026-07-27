"""Startup schema validation against the catalog.

Most of this file drives a scripted fake, which is right for the comparison
logic and wrong for the catalog read itself: the fake answers with `str` and
`int`, and the real driver does not. `pg_catalog` is made of `name`, `oid`,
`"char"`, `int2[]` and `int2vector`, none of which the driver has a codec for,
so every one of those tests passed against rows no PostgreSQL would send. The
live tests at the end of this file are the ones that read a real catalog.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.errors import SchemaMismatchError
from wreath.orm.introspection import _validate_constraints, validate_registry
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text
from wreath.postgres import Database, connect

from .conftest import FakeDatabase

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

#: Deliberately not `network`. The default marker expression excludes that mark,
#: and this is the only cover for the default `validate_schema="error"` path, so
#: hiding it behind two gates instead of one is how it went unrun before.
live = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN to validate against a real pg_catalog",
)


class Account(Model, table="accounts"):
    id: Mapped[int] = column(Int64, primary_key=True)
    email: Mapped[str] = column(Text, unique=True)
    note: Mapped[str] = column(Text, nullable=True)


class Parent(Model, table="parents"):
    id: Mapped[int] = column(Int64, primary_key=True)
    alternate_id: Mapped[int] = column(Int64, unique=True)


class Child(Model, table="children"):
    id: Mapped[int] = column(Int64, primary_key=True)
    parent_id: Mapped[int] = column(Int64, references=Parent.id)


async def test_foreign_key_validation_checks_the_referenced_column() -> None:
    database = FakeDatabase()
    registry = Registry(database, [Parent, Child], validate_schema="off")
    spec = registry.spec_for(Child)
    database.connection.script(
        "pg_constraint",
        [
            ["p", "{1}", None, "", ""],
            # The local column is correct, but this FK targets Parent.alternate_id
            # (position 2) rather than the declared Parent.id (position 1).
            ["f", "{2}", "{2}", "public", "parents"],
        ],
    )

    issues = await _validate_constraints(
        database.connection, spec, {1: "id", 2: "parent_id"}
    )

    assert [issue.issue_code for issue in issues] == ["missing_foreign_key"]


def catalog_row(
    name: str, position: int, oid: int, not_null: bool, default: str = ""
) -> list[Any]:
    """One pg_attribute row in the shape _COLUMNS_SQL selects."""
    return ["public", "accounts", name, position, oid, not_null, 0, default]


def healthy(database: FakeDatabase) -> None:
    database.connection.script(
        "pg_attribute",
        [
            catalog_row("id", 1, Int64.oid, True),
            catalog_row("email", 2, Text.oid, True),
            catalog_row("note", 3, Text.oid, False),
        ],
    )
    database.connection.script("pg_constraint", [["p", "{1}", None, "", ""]])
    database.connection.script("pg_index", [["1"], ["2"]])


def build(mode: str) -> tuple[Registry, FakeDatabase]:
    database = FakeDatabase()
    registry = Registry(database, [Account], validate_schema=mode)
    return registry, database


async def test_a_matching_schema_produces_no_issues() -> None:
    registry, database = build("error")
    healthy(database)
    assert not await validate_registry(registry)


async def test_off_performs_no_catalog_query() -> None:
    registry, database = build("off")
    assert not await validate_registry(registry)
    assert database.connection.calls == []
    assert database.acquired == 0


async def test_a_missing_table_is_reported() -> None:
    registry, _ = build("error")
    with pytest.raises(SchemaMismatchError, match="missing_table"):
        await validate_registry(registry)


async def test_a_missing_column_is_reported() -> None:
    registry, database = build("error")
    database.connection.script(
        "pg_attribute",
        [catalog_row("id", 1, Int64.oid, True), catalog_row("email", 2, Text.oid, True)],
    )
    database.connection.script("pg_constraint", [["p", "{1}", None, "", ""]])
    database.connection.script("pg_index", [["1"], ["2"]])
    with pytest.raises(SchemaMismatchError, match="missing_column"):
        await validate_registry(registry)


async def test_a_type_mismatch_is_reported_by_oid() -> None:
    registry, database = build("error")
    database.connection.script(
        "pg_attribute",
        [
            catalog_row("id", 1, Int64.oid, True),
            catalog_row("email", 2, Int64.oid, True),
            catalog_row("note", 3, Text.oid, False),
        ],
    )
    database.connection.script("pg_constraint", [["p", "{1}", None, "", ""]])
    database.connection.script("pg_index", [["1"], ["2"]])
    with pytest.raises(SchemaMismatchError, match="type_mismatch"):
        await validate_registry(registry)


async def test_a_nullability_mismatch_is_reported() -> None:
    registry, database = build("error")
    database.connection.script(
        "pg_attribute",
        [
            catalog_row("id", 1, Int64.oid, True),
            catalog_row("email", 2, Text.oid, False),
            catalog_row("note", 3, Text.oid, False),
        ],
    )
    database.connection.script("pg_constraint", [["p", "{1}", None, "", ""]])
    database.connection.script("pg_index", [["1"], ["2"]])
    with pytest.raises(SchemaMismatchError, match="nullability_mismatch"):
        await validate_registry(registry)


async def test_a_primary_key_mismatch_is_reported() -> None:
    registry, database = build("error")
    healthy(database)
    database.connection.responses = [
        (fragment, rows)
        for fragment, rows in database.connection.responses
        if fragment != "pg_constraint"
    ]
    database.connection.script("pg_constraint", [["p", "{2}", None, "", ""]])
    with pytest.raises(SchemaMismatchError, match="primary_key_mismatch"):
        await validate_registry(registry)


async def test_a_missing_unique_constraint_is_reported() -> None:
    registry, database = build("error")
    database.connection.script(
        "pg_attribute",
        [
            catalog_row("id", 1, Int64.oid, True),
            catalog_row("email", 2, Text.oid, True),
            catalog_row("note", 3, Text.oid, False),
        ],
    )
    database.connection.script("pg_constraint", [["p", "{1}", None, "", ""]])
    database.connection.script("pg_index", [["1"]])
    with pytest.raises(SchemaMismatchError, match="missing_unique"):
        await validate_registry(registry)


async def test_warn_emits_one_warning_and_does_not_raise() -> None:
    registry, _ = build("warn")
    with pytest.warns(RuntimeWarning, match="missing_table"):
        diff = await validate_registry(registry)
    assert diff


async def test_the_diff_is_sorted_deterministically() -> None:
    registry, database = build("warn")
    database.connection.script(
        "pg_attribute",
        [
            catalog_row("note", 3, Int64.oid, True),
            catalog_row("email", 2, Int64.oid, False),
            catalog_row("id", 1, Text.oid, True),
        ],
    )
    database.connection.script("pg_constraint", [["p", "{1}", None, "", ""]])
    database.connection.script("pg_index", [["1"], ["2"]])
    with pytest.warns(RuntimeWarning):
        diff = await validate_registry(registry)
    keys = [(item.schema, item.table, item.column, item.issue_code) for item in diff.issues]
    assert keys == sorted(keys)


async def test_validation_returns_its_connection() -> None:
    registry, database = build("warn")
    with pytest.warns(RuntimeWarning):
        await validate_registry(registry)
    assert database.acquired == 1
    assert database.released == 1


async def test_a_declared_server_default_is_compared_normalized() -> None:
    class Stamped(Model, table="stamped"):
        id: Mapped[int] = column(Int64, primary_key=True)
        state: Mapped[str] = column(Text, server_default="'new'::text")

    database = FakeDatabase()
    registry = Registry(database, [Stamped], validate_schema="error")
    database.connection.script(
        "pg_attribute",
        [
            ["public", "stamped", "id", 1, Int64.oid, True, 0, ""],
            ["public", "stamped", "state", 2, Text.oid, True, 0, "('new'::text)"],
        ],
    )
    database.connection.script("pg_constraint", [["p", "{1}", None, "", ""]])
    database.connection.script("pg_index", [["1"]])
    # Parentheses and case differences are PostgreSQL's rendering, not a drift.
    assert not await validate_registry(registry)


async def test_validation_never_writes_ddl() -> None:
    registry, database = build("warn")
    healthy(database)
    await validate_registry(registry)
    for sql, _ in database.connection.calls:
        assert sql.strip().upper().startswith("SELECT")


# --------------------------------------------------------------------------
# Against a real catalog.
#
# Everything above answers "does the comparison draw the right conclusion from
# these rows". These answer "are those the rows PostgreSQL sends", which is the
# question the fake cannot be asked -- and the one that was wrong. Until this
# ran, `validate_schema` had never completed against a database: the read
# raised inside the reader task and the caller waited forever.
# --------------------------------------------------------------------------

_DDL = """
CREATE TABLE {schema}.parents (
    id bigint PRIMARY KEY,
    alternate_id bigint NOT NULL UNIQUE
);
CREATE TABLE {schema}.children (
    id bigint PRIMARY KEY,
    parent_id bigint NOT NULL REFERENCES {schema}.parents (id)
);
"""


@pytest.fixture
async def live_schema() -> AsyncIterator[tuple[str, Any]]:
    """A throwaway schema holding the two tables the live models map."""
    assert _DSN is not None
    schema = f"introspect_{uuid.uuid4().hex[:12]}"
    connection = await connect(_DSN)
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        for statement in _DDL.format(schema=f'"{schema}"').split(";"):
            if statement.strip():
                await connection.execute(statement)
    finally:
        await connection.close()

    database = Database("live", _DSN)
    await database.start()
    try:
        yield schema, database
    finally:
        await database.stop()
        cleanup = await connect(_DSN)
        try:
            await cleanup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await cleanup.close()


def _live_models(schema: str) -> list[type]:
    """Two models, so every catalog statement runs cold *and* then cached.

    `validate_registry` reuses one connection for every spec, so the first model
    sends each statement for the first time (text results) and the second reuses
    the prepared plan (binary results). An uncast catalog column decodes
    differently in the two, so a single model would only ever prove half of it.
    """

    class LiveParent(Model, table="parents", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        alternate_id: Mapped[int] = column(Int64, unique=True)

    class LiveChild(Model, table="children", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        parent_id: Mapped[int] = column(Int64, references=LiveParent.id)

    return [LiveParent, LiveChild]


@live
async def test_the_default_validate_schema_completes_against_a_real_catalog(
    live_schema: tuple[str, Any],
) -> None:
    """The shipped default, end to end, against PostgreSQL.

    `validate_schema` is not passed: `"error"` is what a `Registry` and
    `app.orm()` use when nobody says otherwise, so the default is what has to
    work. It did not. The catalog read raised in the reader task, which is not
    the caller's task, and lifespan startup hung there forever -- so a wreath
    app with default settings never finished starting against a real database.

    `asyncio.wait_for` is the assertion for that half. A plain `await` would
    have hung the suite rather than failed this test.
    """
    schema, database = live_schema
    registry = Registry(database, _live_models(schema))

    assert registry.validate_schema == "error", "the default under test moved"

    diff = await asyncio.wait_for(validate_registry(registry), timeout=15.0)

    assert not diff, f"a matching schema reported {diff.report()}"


@live
async def test_a_real_catalog_mismatch_is_reported_not_silently_accepted(
    live_schema: tuple[str, Any],
) -> None:
    """The live pass above must not be passing vacuously.

    Every catalog value the comparison keys on comes back through a codec: the
    column name (`name`), the type OID (`oid`), the primary-key and unique
    column lists (`int2[]` and `int2vector`), the foreign-key target
    (`int2[]`). Decoded wrongly, they do not report a mismatch -- they report
    *everything* as a mismatch, which is what an uncast read did. So this pins
    both directions: the two issues below and no others.
    """
    schema, database = live_schema

    class Wrong(Model, table="parents", schema=schema):
        id: Mapped[str] = column(Text, primary_key=True)  # bigint in the database
        alternate_id: Mapped[int] = column(Int64)  # unique in the database
        absent: Mapped[int] = column(Int64, nullable=True)  # not in the database

    registry = Registry(database, [Wrong])

    with pytest.raises(SchemaMismatchError) as caught:
        await asyncio.wait_for(validate_registry(registry), timeout=15.0)

    reported = {(issue.column, issue.issue_code) for issue in caught.value.diff}
    assert reported == {
        ("id", "type_mismatch"),
        ("absent", "missing_column"),
    }, reported


@live
async def test_the_catalog_read_survives_a_composite_key_and_a_foreign_key(
    live_schema: tuple[str, Any],
) -> None:
    """A multi-column `conkey`/`indkey` is where the array decode actually bites.

    Single-column vectors render as `{1}` and `1`, which several wrong readings
    survive by accident. Two columns give `{1,2}` and `1 2`, and a composite
    foreign key gives a `confkey` that has to line up positionally with it.
    """
    schema, database = live_schema
    connection = await connect(_DSN)
    try:
        await connection.execute(
            f'CREATE TABLE "{schema}".pairs ('
            "  left_id bigint NOT NULL,"
            "  right_id bigint NOT NULL,"
            "  PRIMARY KEY (left_id, right_id))"
        )
    finally:
        await connection.close()

    class Pair(Model, table="pairs", schema=schema):
        left_id: Mapped[int] = column(Int64, primary_key=True)
        right_id: Mapped[int] = column(Int64, primary_key=True)

    registry = Registry(database, [*_live_models(schema), Pair])

    diff = await asyncio.wait_for(validate_registry(registry), timeout=15.0)

    assert not diff, f"a matching composite key reported {diff.report()}"
