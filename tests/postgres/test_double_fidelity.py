"""The doubles and the server, asked the same questions.

Every rule in ``tests/_pgfidelity.py`` exists because a fake accepted something
PostgreSQL refuses, and code built on that acceptance shipped broken. The
obvious fix -- make the fakes stricter -- replaces one fiction with a more
confident one unless the strictness is *measured*. So this file asks the same
question twice, once of a real connection and once of the helper the doubles
use, and fails if they disagree.

That makes a divergence a red test today rather than an incident later. It is
also the only file that can notice the helper drifting when the driver changes.

Skipped unless ``WREATH_TEST_POSTGRES_DSN`` points at a throwaway database.
"""

from __future__ import annotations

import datetime
import decimal
import os
import uuid
from typing import Any

import pytest

# A plain import, not a relative one: `tests/` is on `sys.path` (its conftest
# puts it there) but is not a package, while `tests/postgres/` is -- so `..`
# escapes the top level. The module name is unique across the tree precisely so
# this is unambiguous; `import conftest` is the cautionary tale.
from _pgfidelity import (  # noqa: E402
    FakeRecord,
    PreparedStatements,
    check_bindable,
    check_single_statement,
    record,
)

from wreath.postgres import Database, PoolConfig

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
        reason="set WREATH_TEST_POSTGRES_DSN to run live double-fidelity checks",
    ),
    pytest.mark.asyncio,
]

_OK = "ok"


@pytest.fixture
async def connection() -> Any:
    database = Database(
        "fidelity",
        os.environ["WREATH_TEST_POSTGRES_DSN"],
        pools={"write": PoolConfig(min_size=1, max_size=2)},
    )
    await database.start()
    held = await database.acquire("write")
    try:
        yield held
    finally:
        await database.release("write", held)
        await database.stop()


def _classify(error: BaseException | None) -> str:
    """Reduce an outcome to something both sides can be compared on."""
    if error is None:
        return _OK
    if isinstance(error, TypeError):
        return "TypeError"
    if "multiple commands" in str(error):
        return "multiple-commands"
    return type(error).__name__


async def _server(connection: Any, sql: str, args: tuple[Any, ...], runs: int) -> list[str]:
    outcomes = []
    for _ in range(runs):
        try:
            await connection.fetchval(sql, *args)
            outcomes.append(_OK)
        except BaseException as error:  # noqa: BLE001 - classification is the point
            outcomes.append(_classify(error))
    return outcomes


def _double(sql: str, args: tuple[Any, ...], runs: int) -> list[str]:
    prepared = PreparedStatements()
    outcomes = []
    for _ in range(runs):
        try:
            check_single_statement(sql)
            check_bindable(args)
            prepared.check(sql, args)
            outcomes.append(_OK)
        except BaseException as error:  # noqa: BLE001 - classification is the point
            outcomes.append(_classify(error))
    return outcomes


#: Each case is a claim about the server that a double must also make. Two runs
#: everywhere, because the whole regclass defect lived in the gap between them.
CASES: list[tuple[str, str, tuple[Any, ...]]] = [
    ("plain int", "SELECT $1::int", (1,)),
    ("plain text", "SELECT $1::text", ("x",)),
    ("bytea", "SELECT $1::bytea", (b"x",)),
    ("jsonb", "SELECT $1::jsonb", ('{"a":1}',)),
    # The defect, exactly: fine once, fatal thereafter.
    ("regclass cast", "SELECT $1::regclass", ("pg_class",)),
    ("oid cast", "SELECT $1::oid", (1,)),
    ("name cast", "SELECT $1::name", ("x",)),
    # The same trap wearing a friendlier type name.
    ("uuid cast, str", "SELECT $1::uuid", ("11111111-1111-1111-1111-111111111111",)),
    ("uuid cast, UUID", "SELECT $1::uuid", (uuid.UUID(int=1),)),
    ("numeric cast, str", "SELECT $1::numeric", ("1.5",)),
    ("numeric cast, Decimal", "SELECT $1::numeric", (decimal.Decimal("1.5"),)),
    (
        "timestamptz cast, datetime",
        "SELECT $1::timestamptz",
        (datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),),
    ),
    # No encoder for a list at all, so PostgreSQL is never even reached.
    ("ANY with a list", "SELECT 1 WHERE 1 = ANY($1)", ([1, 2],)),
    ("ANY with a tuple", "SELECT 1 WHERE 1 = ANY($1)", ((1, 2),)),
    ("multi-statement", "SELECT 1; SELECT 2", ()),
]


@pytest.mark.parametrize(("label", "sql", "args"), CASES, ids=[c[0] for c in CASES])
async def test_the_double_agrees_with_the_server(
    connection: Any, label: str, sql: str, args: tuple[Any, ...]
) -> None:
    """Run it twice against both. Disagreement on either run is the failure."""
    assert await _server(connection, sql, args, 2) == _double(sql, args, 2), label


async def test_a_cast_is_fine_once_and_fatal_after(connection: Any) -> None:
    """The shape of the defect itself, stated as a property rather than a case.

    Worth its own test because the parametrised run would still pass if both
    sides became uniformly strict -- and uniform strictness would be wrong. The
    first call really does succeed, and a double that refused immediately would
    make an unreachable branch look reachable.
    """
    outcomes = await _server(connection, "SELECT $1::regclass", ("pg_class",), 3)
    assert outcomes == [_OK, "TypeError", "TypeError"]
    assert _double("SELECT $1::regclass", ("pg_class",), 3) == outcomes


async def test_to_regclass_is_the_spelling_that_survives(connection: Any) -> None:
    """The fix the progress denominator now uses, pinned so it cannot regress."""
    sql = "SELECT reltuples FROM pg_class WHERE oid = to_regclass($1)"
    assert await _server(connection, sql, ("pg_class",), 3) == [_OK, _OK, _OK]


async def test_reltuples_is_minus_one_before_analyze(connection: Any) -> None:
    """Why the denominator must check rather than trust.

    ``Estimated()`` reads ``reltuples`` and treats -1 as *no denominator*. That
    is not defensive coding: a freshly created table really does answer -1, and
    a percentage over it would be a negative total.
    """
    await connection.execute("DROP TABLE IF EXISTS fidelity_fresh")
    await connection.execute("CREATE TABLE fidelity_fresh (a int)")
    try:
        await connection.execute(
            "INSERT INTO fidelity_fresh SELECT g FROM generate_series(1, 100) g"
        )
        sql = "SELECT reltuples FROM pg_class WHERE oid = to_regclass($1)"
        assert await connection.fetchval(sql, "fidelity_fresh") == -1.0
        await connection.execute("ANALYZE fidelity_fresh")
        assert await connection.fetchval(sql, "fidelity_fresh") == 100.0
    finally:
        await connection.execute("DROP TABLE IF EXISTS fidelity_fresh")


async def test_the_record_surface_matches(connection: Any) -> None:
    """``FakeRecord`` has exactly the surface ``Record`` has -- no more.

    A double returning ``dict`` is friendlier and wrong: it makes
    ``row.values()`` work in tests and fail in production, which is precisely
    how the ``EXPLAIN`` test came to be written against an API that does not
    exist.
    """
    real = await connection.fetchrow("SELECT 1 AS a, 'x' AS b")
    fake = record({"a": 1, "b": "x"})

    assert len(real) == len(fake) == 2
    assert (real[0], real["a"]) == (fake[0], fake["a"]) == (1, 1)
    assert (real[1], real["b"]) == (fake[1], fake["b"]) == ("x", "x")
    # The sequence protocol falls back to __getitem__, so this works on both.
    assert list(real) == list(fake) == [1, "x"]

    for absent in ("keys", "items", "get", "values"):
        assert not hasattr(real, absent), absent
        assert not hasattr(fake, absent), absent

    for row in (real, fake):
        with pytest.raises(KeyError):
            row["nope"]
        with pytest.raises(IndexError):
            row[99]
        with pytest.raises(TypeError):
            dict(row)
        # `in` compares against values, not column names, on both.
        assert ("a" in row) is False
        assert ("x" in row) is True


async def test_a_qualified_column_is_a_predicate_the_server_accepts(
    connection: Any,
) -> None:
    """The shape the passes fake could not parse, so the branch went untested."""
    await connection.execute("DROP TABLE IF EXISTS fidelity_q")
    await connection.execute("CREATE TABLE fidelity_q (a int)")
    try:
        await connection.execute("INSERT INTO fidelity_q VALUES (5)")
        rows = await connection.fetch(
            'SELECT a FROM fidelity_q WHERE "fidelity_q"."a" = $1', 5
        )
        assert [row[0] for row in rows] == [5]
    finally:
        await connection.execute("DROP TABLE IF EXISTS fidelity_q")


def test_the_helper_refuses_without_a_server() -> None:
    """The helper's rules hold with no database, which is where fakes run.

    Not gated on the DSN in spirit -- it is the ungated half of every claim
    above -- but it lives here so the two are read together.
    """
    with pytest.raises(TypeError, match="unsupported PostgreSQL value type: list"):
        check_bindable(([1, 2],))
    with pytest.raises(Exception, match="multiple commands"):
        check_single_statement("SELECT 1; SELECT 2")
    prepared = PreparedStatements()
    prepared.check("SELECT $1::regclass", ("pg_class",))
    with pytest.raises(TypeError, match="no binary encoder"):
        prepared.check("SELECT $1::regclass", ("pg_class",))


def test_a_semicolon_inside_a_string_is_not_a_second_command() -> None:
    """Splitting naively would refuse a perfectly good statement."""
    check_single_statement("SELECT 'a;b'")
    check_single_statement("SELECT 1;")
    with pytest.raises(Exception, match="multiple commands"):
        check_single_statement("SELECT 'a;b'; SELECT 2")


def test_the_fake_record_rejects_a_bad_index_type() -> None:
    with pytest.raises(TypeError):
        FakeRecord(("a",), (1,))[1.5]
