"""The ORM fake refuses what the driver refuses, and hands back its shape.

`FakeConnection` is the most-used double in the tree -- 86 scripted responses
across eight files -- and for a long time it accepted anything and returned
whatever container it was handed. That is the exact shape
the never-more-capable rule for doubles in `AGENTS.md` records: thirteen
introspection tests drove a fake modelling a driver with catalog codecs, all
green, while `validate_schema="error"` had never once completed lifespan
startup against a real PostgreSQL.

These tests pin the fake to the driver rather than to itself. Each one names a
value or a shape a real connection rejects, and asserts the fake rejects it
too -- so a test built on the fake accepting it cannot be written.
"""

from __future__ import annotations

import pytest

from wreath._replay_adapters import ScriptedRecord

from .conftest import FakeConnection


async def test_a_list_argument_is_refused_as_the_driver_refuses_it() -> None:
    """`= ANY($1)` never reaches PostgreSQL: inference has no element type.

    Two safety refusals in the tree had *never once fired* because the fakes
    accepted a list here, so the branch was untestable, so it went untested.
    """
    connection = FakeConnection()
    with pytest.raises(TypeError, match="unsupported PostgreSQL value type: list"):
        await connection.fetch("SELECT 1 WHERE id = ANY($1)", [1, 2])


async def test_the_refusal_carries_the_drivers_own_guidance() -> None:
    """Not merely the same exception -- the same sentence.

    The fake delegates to `_infer_oid`, so the message a test sees is the one a
    user sees, including the part that names the fix.
    """
    connection = FakeConnection()
    with pytest.raises(TypeError, match=r"IN \(\$1, \$2, \.\.\.\)"):
        await connection.fetch("SELECT 1 WHERE id = ANY($1)", [1, 2])


async def test_two_commands_in_one_statement_are_refused() -> None:
    """The extended query protocol takes one command per statement."""
    connection = FakeConnection()
    with pytest.raises(Exception, match="multiple commands"):
        await connection.execute("SELECT 1; SELECT 2")


async def test_a_semicolon_inside_a_string_is_still_one_command() -> None:
    """The over-refusal guard. Without it the rule would reject valid SQL."""
    connection = FakeConnection()
    await connection.execute("SELECT 'a;b'")


async def test_a_cast_is_fine_once_and_fatal_after() -> None:
    """The defect that shipped, reproducible on the fake at last.

    A cast on a placeholder declares the *parameter* type, and only the
    prepared statement carries it -- so the first execution is coerced and the
    second binds by an OID with no encoder. A fake with no notion of preparing
    cannot model that, which is why `$1::regclass` reached a default code path.
    """
    connection = FakeConnection()
    await connection.fetch("SELECT $1::regclass", "pg_class")
    with pytest.raises(TypeError, match="no binary encoder"):
        await connection.fetch("SELECT $1::regclass", "pg_class")


async def test_scripted_rows_come_back_with_the_drivers_surface() -> None:
    """`fetch` yields `Record`s, so the fake must not yield a `list`.

    A bare list accepts `.append`, `.index` and slicing that a real row does
    not, and a test leaning on any of them passes here and fails in production.
    """
    connection = FakeConnection()
    connection.script("users", [[1, "a@b.c"]])
    rows = await connection.fetch("SELECT id, email FROM users")

    assert isinstance(rows[0], ScriptedRecord)
    assert rows[0][0] == 1  # positional access is what the hydrator uses
    for absent in ("append", "keys", "values", "items", "get"):
        assert not hasattr(rows[0], absent), absent


async def test_a_scripted_mapping_keeps_its_column_names() -> None:
    """A dict is a convenient way to write a row; it is not a row shape.

    Wrapping keeps the names usable by subscript -- which a real `Record`
    supports -- without keeping `.values()`, which it does not.
    """
    connection = FakeConnection()
    connection.script("users", [{"id": 1, "email": "a@b.c"}])
    row = await connection.fetchrow("SELECT id, email FROM users")

    assert row["email"] == "a@b.c"
    assert row[0] == 1
    assert not hasattr(row, "values")


def test_duplicate_scripted_columns_keep_the_drivers_first_match_rule() -> None:
    """The O(1) name index preserves the old tuple.index first-column result."""
    row = ScriptedRecord(("value", "value"), ("first", "second"))

    assert row["value"] == "first"


async def test_a_declared_oid_refuses_a_value_the_driver_would_not_return() -> None:
    """The guard that would have caught the introspection defect on day one.

    `pg_attribute.attname` is `name`, an OID the driver has no codec for, so a
    real connection hands back `b"id"` and never `"id"`. Thirteen tests once
    scripted the `str`, and `validate_schema="error"` -- the framework default
    -- had never once completed lifespan startup against a real PostgreSQL.

    Declaring the result OIDs is what makes the fake able to know that, and
    from that moment it enforces it: the expected value comes from the driver's
    own `_decode_value`, so there is no second table to keep in step.
    """
    connection = FakeConnection()
    sql = "SELECT attname FROM pg_attribute"
    connection.script("pg_attribute", [{"attname": "id"}])
    connection.describe(sql, ("attname",), (19,))  # 19 = name

    with pytest.raises(AssertionError, match="Script what the driver returns"):
        await connection.fetch(sql)


async def test_the_driver_shaped_value_is_accepted() -> None:
    """The other half: the guard must admit what a real connection returns.

    Without this, the refusal above would also pass against a fake that refused
    everything, which proves nothing about fidelity.
    """
    connection = FakeConnection()
    sql = "SELECT attname FROM pg_attribute"
    connection.script("pg_attribute", [{"attname": b"id"}])
    connection.describe(sql, ("attname",), (19,))

    rows = await connection.fetch(sql)
    assert rows[0]["attname"] == b"id"


async def test_an_undeclared_result_is_positional_and_unchecked() -> None:
    """No `describe()` means the fake was never told the types.

    Enforcing an OID it does not have would be inventing one. The row stays
    positional-only, which is honest, and the 169 scripted responses that never
    declare a plan keep working.
    """
    connection = FakeConnection()
    connection.script("users", [[1, "a@b.c"]])
    rows = await connection.fetch("SELECT id, email FROM users")
    assert rows[0][0] == 1


async def test_the_deliberate_mismatch_opt_out_has_to_be_written() -> None:
    """`checked=False` is a decision; the default is enforcement.

    A handful of tests describe a plan that disagrees with the model on
    purpose. That is legitimate, and it has to be said out loud -- a silent
    exemption is the hole this guard exists to close.
    """
    connection = FakeConnection()
    sql = "SELECT attname FROM pg_attribute"
    connection.script("pg_attribute", [{"attname": "id"}])
    connection.describe(sql, ("attname",), (19,), checked=False)

    rows = await connection.fetch(sql)
    assert rows[0]["attname"] == "id"
