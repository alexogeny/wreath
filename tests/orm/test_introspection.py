"""Startup schema validation against the catalog."""

from __future__ import annotations

from typing import Any

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.errors import SchemaMismatchError
from wreath.orm.introspection import validate_registry
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text

from .conftest import FakeDatabase

pytestmark = pytest.mark.asyncio


class Account(Model, table="accounts"):
    id: Mapped[int] = column(Int64, primary_key=True)
    email: Mapped[str] = column(Text, unique=True)
    note: Mapped[str] = column(Text, nullable=True)


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
