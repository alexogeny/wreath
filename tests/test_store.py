from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from wreath._pgname import quote_identifier, validate_identifier
from wreath.store import CLAIMED, Column, Keyed, MemoryStore, PostgresStore, Sql, sql_identifier


class _FakeStatement:
    def __init__(self, name: str, sql: str, workload: str) -> None:
        self.name = name
        self.sql = sql
        self.workload = workload
        self.calls: list[tuple[Any, ...]] = []
        self.result: Any = None

    async def fetchrow(self, *args: Any) -> Any:
        self.calls.append(args)
        return self.result

    async def execute(self, *args: Any) -> str:
        self.calls.append(args)
        return "OK"


class _FakeDatabase:
    def __init__(self) -> None:
        self.statements: dict[str, _FakeStatement] = {}

    def statement(self, name: str, sql: str, *, workload: str) -> _FakeStatement:
        if name in self.statements:
            raise ValueError(f"duplicate PostgreSQL statement: {name}")
        statement = _FakeStatement(name, sql, workload)
        self.statements[name] = statement
        return statement


def _declaration(**kwargs: Any) -> Keyed:
    return Keyed(
        table="things",
        columns=(Column("status", "int"), Column("body", "bytea")),
        prefix="wreath_thing",
        **kwargs,
    )


def test_identifiers_are_checked_rather_than_quoted() -> None:
    assert sql_identifier("wreath_session") == "wreath_session"
    for bad in ("a; DROP TABLE users", "", "1abc", "sch.ema", "a b"):
        with pytest.raises(ValueError, match="plain SQL identifier"):
            sql_identifier(bad)
    with pytest.raises(ValueError, match="key .* is not a plain SQL identifier"):
        sql_identifier("a-b", what="key")


def test_a_non_string_sql_identifier_is_refused_by_the_identifier_contract() -> None:
    value: Any = object()
    with pytest.raises(ValueError, match="plain SQL identifier"):
        sql_identifier(value)


def test_quoted_identifier_validation_refuses_each_invalid_shape() -> None:
    with pytest.raises(ValueError, match="must be 1..63 bytes"):
        validate_identifier(object(), "channel")

    for value in (object(), "", "bad\x00name"):
        with pytest.raises(ValueError, match="unusable SQL identifier"):
            quote_identifier(value)


def test_every_name_in_the_declaration_is_checked_not_only_the_table() -> None:
    for kwargs in (
        {"table": "t; DROP TABLE users"},
        {"table": "t", "key": "k; --"},
        {"table": "t", "stamp": "e; --"},
        {"table": "t", "columns": (Column("d; --", "jsonb"),)},
    ):
        with pytest.raises(ValueError, match="plain SQL identifier"):
            Keyed(**kwargs)  # type: ignore[arg-type]


def test_the_schema_is_offered_not_applied() -> None:
    sql = Keyed(
        table="wreath_session",
        columns=(Column("data", "jsonb", null=False),),
        key="sid",
        index_stamp=True,
    ).schema_sql()

    assert sql.startswith("CREATE TABLE IF NOT EXISTS wreath_session (")
    assert "sid text PRIMARY KEY" in sql
    assert "data jsonb NOT NULL" in sql
    assert "expires timestamptz NOT NULL" in sql
    # An index on the stamp is what makes purging a large table cheap.
    assert "CREATE INDEX IF NOT EXISTS wreath_session_expires_idx" in sql
    # Offered, not applied: nothing here runs unless a migration runs it.
    assert "DROP" not in sql


def test_a_claim_needs_a_deadline_and_a_payload_it_may_reset() -> None:
    with pytest.raises(ValueError, match="deadline"):
        _declaration(claim=True, ttl=60.0, deadline=False)
    with pytest.raises(ValueError, match="nullable"):
        Keyed(table="t", columns=(Column("tokens", "float8", null=False),), claim=True, ttl=60.0)
    with pytest.raises(ValueError, match="ttl"):
        _declaration(claim=True)
    with pytest.raises(ValueError, match="ttl must be positive"):
        _declaration(ttl=0.0)


@pytest.mark.parametrize("ttl", [float("nan"), float("inf")])
def test_a_store_ttl_must_be_finite_at_declaration(ttl: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        _declaration(ttl=ttl)


def test_a_store_ttl_refuses_a_boolean() -> None:
    with pytest.raises(ValueError, match="number of seconds"):
        _declaration(ttl=True)


def test_a_store_snapshots_its_column_declaration() -> None:
    columns: Any = [Column("body", "text")]
    declaration = Keyed(table="things", columns=columns)

    columns[0] = Column("injected", "text); DROP TABLE users; --")

    assert declaration.columns == (Column("body", "text"),)
    assert "DROP TABLE" not in declaration.schema_sql()


async def test_the_store_touches_no_database_until_a_statement_runs() -> None:
    store = PostgresStore(object(), _declaration(ttl=60.0, claim=True))
    assert store.table == "things"
    assert "CREATE TABLE" in store.schema_sql()
    assert "ON CONFLICT" in store.sql("claim")  # the SQL is ready...
    with pytest.raises(AttributeError):
        await store.claim("k")  # ... the statement is not


async def test_a_statement_is_prepared_once_and_named_for_its_store() -> None:
    database = _FakeDatabase()
    store = PostgresStore(database, _declaration(ttl=60.0, claim=True))

    await store.claim("k")
    await store.claim("k")

    assert list(database.statements) == ["wreath_thing_claim_things"]
    statement = database.statements["wreath_thing_claim_things"]
    assert statement.workload == "write"
    assert statement.calls == [("k",), ("k",)]


async def test_reads_can_be_sent_to_a_replica_but_only_on_request() -> None:
    def workload(**kwargs: Any) -> str:
        database = _FakeDatabase()
        store = PostgresStore(database, _declaration(ttl=60.0), **kwargs)
        return store.workload("read")

    # A claimed row must be read back from the primary that just accepted the
    # claim, so "write" is the default even for a SELECT.
    assert workload() == "write"
    assert workload(read_workload="read") == "read"


def test_a_name_must_be_defined_before_it_can_be_used() -> None:
    store = PostgresStore(object(), _declaration())
    with pytest.raises(ValueError, match="no SQL named"):
        store.sql("acquire")
    store.define("acquire", "SELECT 1")
    assert store.sql("acquire") == "SELECT 1"
    assert store.workload("acquire") == "write"
    with pytest.raises(ValueError, match="already defined"):
        store.define("acquire", "SELECT 2")


async def test_a_returned_row_is_the_claim() -> None:
    database = _FakeDatabase()
    store = PostgresStore(database, _declaration(ttl=60.0, claim=True))

    assert await store.claim("k") is False  # no row: someone else holds it
    database.statements["wreath_thing_claim_things"].result = ("k",)
    assert await store.claim("k") is True

    sql = store.sql("claim")
    assert sql.count(";") == 0  # a second statement would be a second chance
    assert "ON CONFLICT (key) DO UPDATE" in sql
    assert "RETURNING" in sql
    # Reclaimed only when expired, and Postgres owns the clock: workers on
    # disagreeing wall clocks must not disagree about expiry.
    assert "WHERE s.expires < clock_timestamp()" in sql
    assert "now()" not in sql
    # The claim hands back a row with no payload, so a reader can tell "mine,
    # nothing written yet" from "finished".
    body = sql.split("DO UPDATE")[1]
    assert "status = NULL" in body and "body = NULL" in body


async def test_the_claim_is_refused_when_the_declaration_did_not_ask_for_one() -> None:
    store = PostgresStore(_FakeDatabase(), _declaration())
    with pytest.raises(ValueError, match="no SQL named 'claim'"):
        await store.claim("k")


async def test_reading_by_key_can_require_the_row_to_still_be_live() -> None:
    database = _FakeDatabase()
    store = PostgresStore(database, _declaration(ttl=60.0))

    await store.read("k")
    await store.read("k", live=True)

    plain = store.sql("read")
    live = store.sql("read_live")
    assert plain == "SELECT status, body FROM things AS s WHERE s.key = $1"
    # The live predicate is the exact complement of the expired one, so purge
    # can never drop a row a read would still honour.
    assert live == plain + " AND s.expires >= clock_timestamp()"


async def test_deleting_a_key_is_one_statement() -> None:
    database = _FakeDatabase()
    store = PostgresStore(database, _declaration(ttl=60.0))
    assert await store.delete("k") == "OK"
    assert database.statements["wreath_thing_delete_things"].sql == (
        "DELETE FROM things WHERE key = $1"
    )


async def test_purge_drops_what_the_store_no_longer_honours() -> None:
    database = _FakeDatabase()
    store = PostgresStore(database, _declaration(ttl=60.0))
    await store.purge()
    assert store.sql("purge") == "DELETE FROM things AS s WHERE s.expires < clock_timestamp()"
    assert database.statements["wreath_thing_purge_things"].calls == [()]


async def test_purge_can_instead_drop_rows_untouched_for_a_while() -> None:
    database = _FakeDatabase()
    store = PostgresStore(database, _declaration(stamp="updated", deadline=False))
    await store.purge(3600.0)
    assert "updated < clock_timestamp() - make_interval(secs => $1::float8)" in (
        store.sql("purge_idle")
    )
    assert database.statements["wreath_thing_purge_idle_things"].calls == [(3600.0,)]


@pytest.mark.parametrize("idle_seconds", [0.0, -1.0, float("nan"), float("inf"), True])
async def test_purge_idle_window_must_be_a_finite_positive_number(idle_seconds: Any) -> None:
    store = PostgresStore(_FakeDatabase(), _declaration(stamp="updated", deadline=False))
    with pytest.raises(ValueError, match="idle_seconds must be a finite positive number"):
        await store.purge(idle_seconds)


async def test_a_last_touched_stamp_has_no_deadline_to_purge_by() -> None:
    store = PostgresStore(_FakeDatabase(), _declaration(stamp="updated", deadline=False))
    with pytest.raises(ValueError, match="idle_seconds"):
        await store.purge()


def test_the_upsert_builder_keeps_the_shape_every_caller_needs() -> None:
    store = PostgresStore(object(), _declaration(ttl=60.0))
    sql = store.upsert(
        values={"key": "$1", "status": "$2", "expires": store.window("$3")},
        update={"status": Sql("excluded.status")},
        returning="s.status",
    )
    assert sql == (
        "INSERT INTO things AS s (key, status, expires)\n"
        "VALUES ($1, $2, clock_timestamp() + make_interval(secs => $3::float8))\n"
        "ON CONFLICT (key) DO UPDATE SET\n"
        "    status = excluded.status\n"
        "RETURNING s.status"
    )
    assert sql.count(";") == 0
    # A column name is interpolated, so it is checked like any other identifier.
    with pytest.raises(ValueError, match="plain SQL identifier"):
        store.upsert(values={"key; --": "$1"}, update={"status": Sql("excluded.status")})


def test_a_fixed_ttl_renders_as_a_literal_window() -> None:
    store = PostgresStore(object(), _declaration(ttl=86400.0))
    assert store.window() == ("clock_timestamp() + make_interval(secs => 86400.0::float8)")
    with pytest.raises(ValueError, match="ttl"):
        PostgresStore(object(), _declaration()).window()


def test_claiming_in_memory_is_synchronous_and_that_is_the_point() -> None:
    store = MemoryStore(ttl=60.0)
    assert store.claim("k") is True
    assert store.claim("k") is False
    # Claimed but nothing stored yet -- the in-memory twin of a NULL payload.
    assert store.read("k") is CLAIMED
    store.set("k", "payload")
    assert store.read("k") == "payload"
    assert store.claim("k") is False  # a finished key is not reclaimed


def test_a_memory_claim_is_released_by_deleting_it() -> None:
    store = MemoryStore(ttl=60.0)
    store.claim("k")
    store.delete("k")
    assert store.read("k") is None
    assert store.claim("k") is True


def test_a_memory_write_keeps_the_deadline_the_key_was_claimed_with() -> None:
    clock = [1000.0]
    store = MemoryStore(ttl=10.0, clock=lambda: clock[0])

    assert store.claim("k") is True
    clock[0] += 6.0  # a slow handler runs...
    store.set("k", "payload")  # ... and writes its answer
    assert store.read("k") == "payload"

    clock[0] += 4.0  # 10s after the claim, 4s after the write
    assert store.read("k") is None
    assert store.claim("k") is True  # so the key is reclaimable, not extended


def test_a_memory_write_opens_a_fresh_window_when_the_key_had_expired() -> None:
    clock = [1000.0]
    store = MemoryStore(ttl=10.0, clock=lambda: clock[0])

    store.set("k", "first")
    clock[0] += 11.0
    assert store.read("k") is None

    store.set("k", "second")  # the old deadline is gone, not inherited
    clock[0] += 9.0
    assert store.read("k") == "second"


def test_a_memory_store_without_a_ttl_expires_nothing() -> None:
    clock = [1000.0]
    store = MemoryStore(clock=lambda: clock[0])
    store.claim("k")
    store.set("k", "payload")
    clock[0] += 1_000_000.0
    assert store.read("k") == "payload"


@pytest.mark.parametrize("ttl", [True, float("inf")])
def test_a_memory_store_ttl_must_be_a_finite_number(ttl: Any) -> None:
    with pytest.raises(ValueError, match="ttl must be a finite positive number"):
        MemoryStore(ttl=ttl)


def test_a_memory_claim_expires_on_the_ttl() -> None:
    clock = [1000.0]
    store = MemoryStore(ttl=10.0, clock=lambda: clock[0])
    store.claim("k")
    store.set("k", "payload")
    clock[0] += 11.0
    assert store.read("k") is None
    assert store.claim("k") is True  # expiry reclaims, the same as Postgres


def test_the_memory_half_is_bounded_because_a_worker_is_not() -> None:
    store = MemoryStore(ttl=60.0, max_entries=8)
    for index in range(100):
        store.claim(f"k{index}")
    assert len(store) <= 8
    store.clear()
    assert len(store) == 0


class _SlowDatabase(_FakeDatabase):
    """`statement()` slow enough for two callers to overlap in it.

    Not artificial: the real one builds a `Statement`, touches `_configs`, and
    registers in a dict, and the GIL is released across all of that.
    """

    def statement(self, name: str, sql: str, *, workload: str) -> _FakeStatement:
        time.sleep(0.02)
        return super().statement(name, sql, workload=workload)


def test_two_threads_reaching_a_statement_first_do_not_both_prepare_it() -> None:
    database = _SlowDatabase()
    store = PostgresStore(database, _declaration(ttl=3600.0))

    barrier = threading.Barrier(2)
    returned: list[Any] = []
    failed: list[BaseException] = []

    def first_use() -> None:
        barrier.wait()
        try:
            returned.append(store.statement("purge"))
        except BaseException as exc:  # noqa: BLE001
            failed.append(exc)

    threads = [threading.Thread(target=first_use) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failed == []
    assert len(database.statements) == 1
    # And both callers hold the same statement, not one each.
    assert returned[0] is returned[1]


def test_the_settled_path_does_not_take_the_lock() -> None:
    store = PostgresStore(_FakeDatabase(), _declaration(ttl=3600.0))
    first = store.statement("purge")

    store._prepare_lock = None  # any acquire from here would raise
    assert store.statement("purge") is first


class TestRowsAffected:
    """One parser for a PostgreSQL command tag, where there were four.

    Two of the four took the *second* whitespace field, which is right for
    `DELETE 5` and wrong for `INSERT 0 5` -- where the first number is a legacy
    OID and the count is the last field. Those two returned `None` for every
    insert, which at the call site is indistinguishable from "this backend does
    not report a count".
    """

    @pytest.mark.parametrize(
        ("tag", "expected"),
        [
            ("DELETE 5", 5),
            ("UPDATE 3", 3),
            ("SELECT 12", 12),
            ("INSERT 0 5", 5),  # the one the old split got wrong
            ("INSERT 0 0", 0),
            ("DELETE 0", 0),
        ],
    )
    def test_the_count_is_the_last_field(self, tag: str, expected: int) -> None:
        from wreath.store import rows_affected

        assert rows_affected(tag) == expected

    @pytest.mark.parametrize("given", [None, 5, b"DELETE 5", "", "DELETE", "DELETE x"])
    def test_an_unreadable_tag_is_none_rather_than_zero(self, given: object) -> None:
        # "no rows matched" and "this backend does not say" are different facts,
        # and a caller recording the first when it meant the second is reporting
        # a clean sweep that never happened.
        from wreath.store import rows_affected

        assert rows_affected(given) is None


# Table and column names are interpolated into statement text -- they cannot be
# bound -- so `sql_identifier` and `_expression` are the whole of the policy.
# `wreath mutant` reported every one of these controls as removable with no test
# objecting, which for an injection guard is the finding that matters most.


class TestIdentifierGuard:
    @pytest.mark.parametrize(
        "given",
        ["a b", "a-b", "a;b", 'a"b', "a'b", "", "1abc", "a.b", "drop table x", "a\nb"],
    )
    def test_anything_that_is_not_a_bare_identifier_is_refused(self, given: str) -> None:
        from wreath.store import sql_identifier

        with pytest.raises(ValueError, match="plain SQL identifier"):
            sql_identifier(given)

    @pytest.mark.parametrize("given", ["sessions", "wreath_entity", "_x", "a1"])
    def test_a_bare_identifier_is_returned_unchanged(self, given: str) -> None:
        from wreath.store import sql_identifier

        assert sql_identifier(given) == given

    @pytest.mark.parametrize("given", ["A_1", "a" * 64])
    def test_an_unquoted_name_cannot_fold_or_truncate(self, given: str) -> None:
        from wreath.store import sql_identifier

        with pytest.raises(ValueError, match="identifier|63-byte"):
            sql_identifier(given)

    @pytest.mark.parametrize("word", ["select", "table", "user", "order", "default"])
    def test_a_reserved_word_is_refused(self, word: str) -> None:
        # It would reach the generated DDL unquoted and fail there, a long way
        # from the declaration that caused it.
        from wreath.store import sql_identifier

        with pytest.raises(ValueError, match="reserved SQL word"):
            sql_identifier(word)

    @pytest.mark.parametrize("word", ["SELECT", "Table", "UsEr"])
    def test_the_reserved_check_is_case_insensitive(self, word: str) -> None:
        from wreath.store import sql_identifier

        with pytest.raises(ValueError, match="reserved SQL word"):
            sql_identifier(word)

    def test_the_refusal_names_what_was_being_declared(self) -> None:
        from wreath.store import sql_identifier

        with pytest.raises(ValueError, match="^column .* is not"):
            sql_identifier("a b", what="column")


class TestExpressionGuard:
    @pytest.mark.parametrize("given", ["$1", "$12", "$1::text", "$2::float8", " $1 "])
    def test_a_bind_placeholder_is_accepted(self, given: str) -> None:
        from wreath.store import _expression

        assert _expression(given, what="a value") == given

    @pytest.mark.parametrize(
        "given",
        ["now()", "1", "'x'", "$1; DROP TABLE t", "$", "$a", "x$1", "$1::text; --"],
    )
    def test_anything_else_is_refused(self, given: str) -> None:
        # Refused rather than quoted: the caller who genuinely means a fragment
        # says so with `Sql(...)`, and that is the whole audit surface.
        from wreath.store import _expression

        with pytest.raises(TypeError, match="bind placeholder"):
            _expression(given, what="a value")

    def test_an_explicitly_marked_fragment_is_accepted(self) -> None:
        from wreath.store import Sql, _expression

        assert _expression(Sql("clock_timestamp()"), what="a value") == "clock_timestamp()"

    @pytest.mark.parametrize("given", [None, 1, 1.5, b"$1", ["$1"]])
    def test_a_non_string_is_refused(self, given: object) -> None:
        from wreath.store import _expression

        with pytest.raises(TypeError, match="bind placeholder"):
            _expression(given, what="a value")


class TestPreparedStatementNames:
    def test_an_over_long_statement_name_is_refused(self) -> None:
        from wreath.store import Keyed, PostgresStore

        # At *construction*, because the store defines its own statements there
        # -- so an unusable prefix is a declaration-time error rather than a
        # surprise at the first query.
        with pytest.raises(ValueError, match="PostgreSQL truncates"):
            PostgresStore(None, Keyed(table="t" * 40, prefix="p" * 40))

    def test_a_name_within_the_limit_is_accepted(self) -> None:
        from wreath.store import Keyed, PostgresStore

        store = PostgresStore(None, Keyed(table="sessions"))
        store.define("read_one", "SELECT 1")
        assert store.sql("read_one") == "SELECT 1"


class TestDeclarationRendering:
    def test_a_not_null_column_renders_the_constraint(self) -> None:
        from wreath.store import Column, Keyed

        ddl = " ".join(Keyed(table="t", columns=(Column("a", "text", null=False),)).statements())
        assert "a text NOT NULL" in ddl

    def test_a_nullable_column_does_not(self) -> None:
        from wreath.store import Column, Keyed

        ddl = " ".join(Keyed(table="t", columns=(Column("a", "text", null=True),)).statements())
        assert "a text NOT NULL" not in ddl
        assert "a text" in ddl

    def test_the_stamp_index_is_declared_only_when_asked(self) -> None:
        from wreath.store import Keyed

        with_index = " ".join(Keyed(table="t", index_stamp=True).statements())
        without = " ".join(Keyed(table="t", index_stamp=False).statements())
        assert "CREATE INDEX" in with_index
        assert "CREATE INDEX" not in without


class TestStatementShape:
    """Which statements a declaration produces, and what they carry."""

    def test_a_store_with_no_payload_columns_defines_no_read(self) -> None:
        # There is nothing to select: the key is the whole row, and `SELECT
        # FROM` is not a statement.
        from wreath.store import Keyed, PostgresStore

        store = PostgresStore(None, Keyed(table="t"))
        with pytest.raises(ValueError, match="no SQL named"):
            store.sql("read")

    def test_a_store_with_payload_columns_defines_both_reads(self) -> None:
        from wreath.store import Column, Keyed, PostgresStore

        store = PostgresStore(None, Keyed(table="t", columns=(Column("a", "text"),)))
        assert "SELECT a FROM" in store.sql("read")
        assert store.sql("read_live") != store.sql("read")

    def test_read_live_adds_the_deadline_predicate(self) -> None:
        from wreath.store import Column, Keyed, PostgresStore

        store = PostgresStore(None, Keyed(table="t", columns=(Column("a", "text"),)))
        assert "clock_timestamp()" in store.sql("read_live")

    def test_a_last_touched_store_defines_no_deadline_purge(self) -> None:
        # Its stamp is arithmetic for whoever reads it, not a deadline, so
        # there is no expired-row predicate to purge on.
        from wreath.store import Keyed, PostgresStore

        store = PostgresStore(None, Keyed(table="t", deadline=False))
        with pytest.raises(ValueError, match="no SQL named"):
            store.sql("purge")

    def test_a_deadline_store_defines_one(self) -> None:
        from wreath.store import Keyed, PostgresStore

        store = PostgresStore(None, Keyed(table="t", deadline=True))
        assert "DELETE FROM" in store.sql("purge")

    def test_upsert_omits_the_returning_clause_unless_asked(self) -> None:
        from wreath.store import Keyed, PostgresStore

        store = PostgresStore(None, Keyed(table="t"))
        without = store.upsert(values={"key": "$1"}, update={"key": "$1"})
        with_it = store.upsert(values={"key": "$1"}, update={"key": "$1"}, returning="key")
        assert "RETURNING" not in without
        assert with_it.endswith("RETURNING key")


@pytest.mark.asyncio
async def test_read_live_is_chosen_only_when_asked() -> None:
    from wreath.store import Column, Keyed, PostgresStore

    class Recording(PostgresStore):
        # A subclass gets a __dict__, which the slotted base deliberately does not.
        def statement(self, name: str):  # type: ignore[override]
            self.asked = name

            class _Stub:
                async def fetchrow(self, *_args):
                    return None

            return _Stub()

    store = Recording(None, Keyed(table="t", columns=(Column("a", "text"),)))
    await store.read("k")
    assert store.asked == "read"
    await store.read("k", live=True)
    assert store.asked == "read_live"
