"""The keyed-store primitive: one declaration, two backends, one claim.

These pin the *storage discipline* the three callers used to re-derive: a plain
identifier for the table, a schema that is offered and never applied, statements
prepared lazily because the store outlives none of the startup order, and a
claim that is one statement whose returned row *is* the claim.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath.store import CLAIMED, Column, Keyed, MemoryStore, PostgresStore, sql_identifier

# --- fakes -------------------------------------------------------------------


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


# --- the declaration ---------------------------------------------------------


def test_identifiers_are_checked_rather_than_quoted() -> None:
    """Table names reach SQL by interpolation, so they must be plain."""
    assert sql_identifier("wreath_session") == "wreath_session"
    for bad in ("a; DROP TABLE users", "", "1abc", "sch.ema", "a b"):
        with pytest.raises(ValueError, match="plain SQL identifier"):
            sql_identifier(bad)
    with pytest.raises(ValueError, match="key must be a plain SQL identifier"):
        sql_identifier("a-b", what="key")


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
        Keyed(table="t", columns=(Column("tokens", "float8", null=False),),
              claim=True, ttl=60.0)
    with pytest.raises(ValueError, match="ttl"):
        _declaration(claim=True)
    with pytest.raises(ValueError, match="ttl must be positive"):
        _declaration(ttl=0.0)


# --- lazy preparation --------------------------------------------------------


async def test_the_store_touches_no_database_until_a_statement_runs() -> None:
    """The store is built while the app is described; the database is not up."""
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


# --- the claim ---------------------------------------------------------------


async def test_a_returned_row_is_the_claim() -> None:
    """One statement, one round trip, no owner column."""
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


# --- reads, deletes, purges --------------------------------------------------


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


async def test_a_last_touched_stamp_has_no_deadline_to_purge_by() -> None:
    """`updated` is arithmetic, not a deadline: there is nothing it expires at."""
    store = PostgresStore(_FakeDatabase(), _declaration(stamp="updated", deadline=False))
    with pytest.raises(ValueError, match="idle_seconds"):
        await store.purge()


# --- the upsert builder ------------------------------------------------------


def test_the_upsert_builder_keeps_the_shape_every_caller_needs() -> None:
    store = PostgresStore(object(), _declaration(ttl=60.0))
    sql = store.upsert(
        values={"key": "$1", "status": "$2", "expires": store.window("$3")},
        update={"status": "excluded.status"},
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
        store.upsert(values={"key; --": "$1"}, update={"status": "excluded.status"})


def test_a_fixed_ttl_renders_as_a_literal_window() -> None:
    store = PostgresStore(object(), _declaration(ttl=86400.0))
    assert store.window() == (
        "clock_timestamp() + make_interval(secs => 86400.0::float8)"
    )
    with pytest.raises(ValueError, match="ttl"):
        PostgresStore(object(), _declaration()).window()


# --- the memory half ---------------------------------------------------------


def test_claiming_in_memory_is_synchronous_and_that_is_the_point() -> None:
    """No await between the read and the write, so no task can interleave."""
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
    """The window opens at the first attempt, exactly as it does in Postgres.

    `PostgresStore`'s generated `DO UPDATE` leaves the stamp alone on purpose, so
    a slow handler cannot extend its own key. The memory half says the same, or
    one caller gets two retention policies depending on its backend.
    """
    clock = [1000.0]
    store = MemoryStore(ttl=10.0, clock=lambda: clock[0])

    assert store.claim("k") is True
    clock[0] += 6.0                     # a slow handler runs...
    store.set("k", "payload")           # ... and writes its answer
    assert store.read("k") == "payload"

    clock[0] += 4.0                     # 10s after the claim, 4s after the write
    assert store.read("k") is None
    assert store.claim("k") is True     # so the key is reclaimable, not extended


def test_a_memory_write_opens_a_fresh_window_when_the_key_had_expired() -> None:
    """Not moving a deadline is not the same as never setting one."""
    clock = [1000.0]
    store = MemoryStore(ttl=10.0, clock=lambda: clock[0])

    store.set("k", "first")
    clock[0] += 11.0
    assert store.read("k") is None

    store.set("k", "second")            # the old deadline is gone, not inherited
    clock[0] += 9.0
    assert store.read("k") == "second"


def test_a_memory_store_without_a_ttl_expires_nothing() -> None:
    clock = [1000.0]
    store = MemoryStore(clock=lambda: clock[0])
    store.claim("k")
    store.set("k", "payload")
    clock[0] += 1_000_000.0
    assert store.read("k") == "payload"


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
