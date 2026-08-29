from __future__ import annotations

import pytest
from _pgfidelity import FakePostgresError, check_statement

from .fakes import FakeConnection, World


def _connection() -> FakeConnection:
    return FakeConnection(World("things", [{"id": 1}]))


class TestTheCastThatSurvivesOneCall:
    """`$1::regclass` in the default progress denominator.

    The cast makes PostgreSQL infer the *parameter* as `regclass`, which has no
    binary encoder. Only the prepared statement carries the inferred type, so
    the first execution survives and every later one raises -- "works once,
    then fails forever", on a default code path.
    """

    @pytest.mark.asyncio
    async def test_the_first_call_still_succeeds(self) -> None:
        # Refusing immediately would be a *different* fiction: it would make an
        # unreachable branch look reachable, and hide that a single-call test
        # cannot catch this either.
        connection = _connection()
        await connection.fetchval("SELECT reltuples FROM x WHERE oid = $1::regclass", "t")

    @pytest.mark.asyncio
    async def test_the_second_call_raises(self) -> None:
        connection = _connection()
        sql = "SELECT reltuples FROM x WHERE oid = $1::regclass"
        await connection.fetchval(sql, "t")
        with pytest.raises(TypeError, match="no binary encoder"):
            await connection.fetchval(sql, "t")

    @pytest.mark.asyncio
    async def test_to_regclass_survives_every_call(self) -> None:
        connection = _connection()
        sql = "SELECT reltuples FROM x WHERE oid = to_regclass($1)"
        for _ in range(3):
            await connection.fetchval(sql, "t")

    @pytest.mark.asyncio
    async def test_a_cast_to_an_encodable_type_is_fine(self) -> None:
        connection = _connection()
        sql = "SELECT reltuples FROM pg_class WHERE oid = $1::jsonb"
        for _ in range(3):
            await connection.fetchval(sql, '{"a": 1}')

    @pytest.mark.asyncio
    async def test_the_same_trap_with_a_friendlier_type_name(self) -> None:
        connection = _connection()
        sql = "SELECT reltuples FROM pg_class WHERE oid = $1::uuid"
        await connection.fetchval(sql, "11111111-1111-1111-1111-111111111111")
        with pytest.raises(TypeError, match="uuid codec requires UUID"):
            await connection.fetchval(sql, "11111111-1111-1111-1111-111111111111")


class TestBindingAList:
    """`= ANY($1)` with a Python list, in three places.

    `_infer_oid` has no `list` case, so the driver raises before PostgreSQL is
    ever reached. Two of the three sites were *safety refusals*, so neither had
    ever fired, and nor had the feature each was guarding.
    """

    @pytest.mark.asyncio
    async def test_a_list_is_refused_on_the_first_call(self) -> None:
        connection = _connection()
        with pytest.raises(TypeError, match="unsupported PostgreSQL value type: list"):
            await connection.fetch("SELECT 1 WHERE x = ANY($1)", [1, 2, 3])

    @pytest.mark.asyncio
    async def test_a_tuple_is_refused_too(self) -> None:
        connection = _connection()
        with pytest.raises(TypeError, match="unsupported PostgreSQL value type: tuple"):
            await connection.fetch("SELECT 1 WHERE x = ANY($1)", (1, 2, 3))

    def test_the_in_spelling_that_replaced_it_binds_cleanly(self) -> None:
        check_statement("SELECT id FROM things WHERE id IN ($1, $2)", (1, 2))


class TestMultipleCommands:
    """`schema_sql()` output handed straight to `execute`.

    The driver uses the extended query protocol exclusively, so a string with
    two commands is refused outright. That is why every caller splits on
    ``";\\n"`` by hand -- a fake that accepted it made the split look optional.
    """

    @pytest.mark.asyncio
    async def test_two_commands_are_refused(self) -> None:
        connection = _connection()
        with pytest.raises(FakePostgresError, match="multiple commands"):
            await connection.execute("CREATE SCHEMA a;\nCREATE TABLE a.b (c int)")

    @pytest.mark.asyncio
    async def test_one_command_with_a_trailing_semicolon_is_fine(self) -> None:
        connection = _connection()
        await connection.execute("SELECT count(*) FROM things;")


class TestTheRowSurface:
    """A `Record` is not a mapping, and the fake no longer pretends otherwise."""

    @pytest.mark.asyncio
    async def test_a_row_subscripts_by_name_and_position(self) -> None:
        row = await _connection().fetchrow("SELECT count(*) FROM things")
        assert row["count"] == row[0] == 1

    @pytest.mark.asyncio
    async def test_values_is_absent_as_it_is_on_the_real_record(self) -> None:
        # The live `EXPLAIN` test was written against `row.values()` and died on
        # `AttributeError`, because the fake it was drafted against was a dict.
        row = await _connection().fetchrow("SELECT count(*) FROM things")
        assert not hasattr(row, "values")
        with pytest.raises(TypeError):
            dict(row)
