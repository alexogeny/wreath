from __future__ import annotations

import pytest

from wreath._replay_adapters import ScriptedRecord

from .conftest import FakeConnection


async def test_a_list_argument_is_refused_as_the_driver_refuses_it() -> None:
    connection = FakeConnection()
    with pytest.raises(TypeError, match="unsupported PostgreSQL value type: list"):
        await connection.fetch("SELECT 1 WHERE id = ANY($1)", [1, 2])


async def test_the_refusal_carries_the_drivers_own_guidance() -> None:
    connection = FakeConnection()
    with pytest.raises(TypeError, match=r"IN \(\$1, \$2, \.\.\.\)"):
        await connection.fetch("SELECT 1 WHERE id = ANY($1)", [1, 2])


async def test_two_commands_in_one_statement_are_refused() -> None:
    connection = FakeConnection()
    with pytest.raises(Exception, match="multiple commands"):
        await connection.execute("SELECT 1; SELECT 2")


async def test_a_semicolon_inside_a_string_is_still_one_command() -> None:
    connection = FakeConnection()
    await connection.execute("SELECT 'a;b'")


async def test_a_cast_is_fine_once_and_fatal_after() -> None:
    connection = FakeConnection()
    await connection.fetch("SELECT $1::regclass", "pg_class")
    with pytest.raises(TypeError, match="no binary encoder"):
        await connection.fetch("SELECT $1::regclass", "pg_class")


async def test_scripted_rows_come_back_with_the_drivers_surface() -> None:
    connection = FakeConnection()
    connection.script("users", [[1, "a@b.c"]])
    rows = await connection.fetch("SELECT id, email FROM users")

    assert isinstance(rows[0], ScriptedRecord)
    assert rows[0][0] == 1  # positional access is what the hydrator uses
    for absent in ("append", "keys", "values", "items", "get"):
        assert not hasattr(rows[0], absent), absent


async def test_a_scripted_mapping_keeps_its_column_names() -> None:
    connection = FakeConnection()
    connection.script("users", [{"id": 1, "email": "a@b.c"}])
    row = await connection.fetchrow("SELECT id, email FROM users")

    assert row["email"] == "a@b.c"
    assert row[0] == 1
    assert not hasattr(row, "values")


def test_duplicate_scripted_columns_keep_the_drivers_first_match_rule() -> None:
    row = ScriptedRecord(("value", "value"), ("first", "second"))

    assert row["value"] == "first"


async def test_a_declared_oid_refuses_a_value_the_driver_would_not_return() -> None:
    connection = FakeConnection()
    sql = "SELECT attname FROM pg_attribute"
    connection.script("pg_attribute", [{"attname": "id"}])
    connection.describe(sql, ("attname",), (19,))  # 19 = name

    with pytest.raises(AssertionError, match="Script what the driver returns"):
        await connection.fetch(sql)


async def test_the_driver_shaped_value_is_accepted() -> None:
    connection = FakeConnection()
    sql = "SELECT attname FROM pg_attribute"
    connection.script("pg_attribute", [{"attname": b"id"}])
    connection.describe(sql, ("attname",), (19,))

    rows = await connection.fetch(sql)
    assert rows[0]["attname"] == b"id"


async def test_an_undeclared_result_is_positional_and_unchecked() -> None:
    connection = FakeConnection()
    connection.script("users", [[1, "a@b.c"]])
    rows = await connection.fetch("SELECT id, email FROM users")
    assert rows[0][0] == 1


async def test_the_deliberate_mismatch_opt_out_has_to_be_written() -> None:
    connection = FakeConnection()
    sql = "SELECT attname FROM pg_attribute"
    connection.script("pg_attribute", [{"attname": "id"}])
    connection.describe(sql, ("attname",), (19,), checked=False)

    rows = await connection.fetch(sql)
    assert rows[0]["attname"] == "id"
