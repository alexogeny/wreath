"""What `wreath.log` refuses, and what it emits, without a database.

The live half -- ordering under concurrent commits, the horizon, retention --
is `tests/test_log_cursor_live.py`, which needs a real PostgreSQL because the
bug it exists to catch is an interleaving no fake can model.

What is here is the declaration: the DDL a `Log` offers, the guards that fire at
description time rather than at the first append, and the cursor's own
arithmetic-free round trip.
"""

from __future__ import annotations

import pytest

from wreath.log import (
    KEEP_FOREVER,
    Batch,
    Column,
    Cursor,
    Flush,
    Log,
    PostgresLog,
    Record,
)


def _log(**overrides: object) -> Log:
    fields: dict[str, object] = {
        "table": "audit_records",
        "retain": KEEP_FOREVER,
        "columns": (Column("actor", "text", null=False), Column("body", "jsonb")),
    }
    fields.update(overrides)
    return Log(**fields)  # type: ignore[arg-type]


# -- the cursor ------------------------------------------------------------


def test_cursor_round_trips_through_its_wire_form():
    cursor = Cursor(981234567890, 42)
    assert Cursor.decode(cursor.encode()) == cursor


def test_a_missing_cursor_reads_from_the_start():
    assert Cursor.decode(None) == Cursor(0, 0)
    assert Cursor.decode("") == Cursor.start()


@pytest.mark.parametrize(
    "value",
    # The last four are the ones `int()` in a `try` would have accepted: leading
    # whitespace, a leading sign, and a Unicode digit from another script --
    # `"７".isdigit()` is True and `int("７")` is 7, so `isdigit` alone does not
    # refuse it. Both halves are checked, because a mutation run found that only
    # the *head* was covered and dropping the tail's check changed nothing any
    # test noticed.
    [
        "7", "7.", ".7", "a.b", "7.b", "-1.0", "0.-1", "7.8.9",
        " 7.8", "+7.8", "７.8", "7.８",
    ],
)
def test_a_cursor_that_did_not_come_from_encode_is_refused(value):
    # The resumption path takes this from a client-supplied `Last-Event-ID`, so
    # it is an index into the log built from request text. It refuses rather
    # than repairs.
    with pytest.raises(ValueError, match="not a log cursor"):
        Cursor.decode(value)


def test_the_cursor_orders_by_transaction_before_sequence():
    # The property the whole design rests on: a row committed later but
    # allocated earlier sorts *after* the cursor, so it cannot be skipped.
    early_allocation_late_commit = Cursor(xid=200, seq=3)
    late_allocation_early_commit = Cursor(xid=100, seq=5)
    assert late_allocation_early_commit < early_allocation_late_commit


# -- declaration guards ----------------------------------------------------


def test_retention_has_no_default():
    # A chunk buffer and an audit trail want opposite answers, and a default
    # would silently give one of them the other's.
    with pytest.raises(TypeError):
        Log(table="whatever")  # type: ignore[call-arg]


def test_a_negative_retention_is_refused():
    with pytest.raises(ValueError, match="positive number of seconds"):
        _log(retain=-1)


def test_a_table_name_that_is_not_an_identifier_is_refused():
    with pytest.raises(ValueError, match="plain SQL identifier"):
        _log(table="audit records; drop table users")


def test_a_reserved_word_as_a_table_name_is_refused():
    with pytest.raises(ValueError, match="reserved SQL word"):
        _log(table="order")


def test_a_schema_name_that_is_not_an_identifier_is_refused():
    # The schema is interpolated into every generated statement, so it is
    # checked for the same reason the table is. An empty schema is the
    # deliberate exception: it means "wherever search_path points".
    with pytest.raises(ValueError, match="plain SQL identifier"):
        _log(schema="public; drop schema wreath cascade")
    assert _log(schema="").qualified_table == "audit_records"


def test_a_payload_column_may_not_shadow_a_column_the_log_owns():
    # `seq` and `xid` are the cursor. A payload column of the same name would
    # compile and then quietly answer the wrong question.
    for name in ("seq", "xid", "at", "stream"):
        with pytest.raises(ValueError, match="collides with a column the log owns"):
            _log(columns=(Column(name, "text"),))


def test_dedup_is_only_reserved_when_it_was_declared():
    _log(columns=(Column("dedup", "text"),))  # no dedup index: the name is free
    with pytest.raises(ValueError, match="collides"):
        _log(dedup=True, columns=(Column("dedup", "text"),))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"bytes": 0}, "Flush\\(bytes=...\\) must be positive"),
        ({"every": 0}, "positive number of seconds"),
        ({"capacity": 0}, "Flush\\(capacity=...\\) must be positive"),
    ],
)
def test_a_flush_policy_that_never_flushes_is_refused(kwargs, message):
    with pytest.raises(ValueError, match=message):
        Flush(**kwargs)


# -- the DDL ---------------------------------------------------------------


def test_the_table_carries_both_halves_of_the_cursor():
    ddl = _log().statements()[0]
    assert "seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY" in ddl
    # xid8, not xid: the 32-bit type wraps and a wrapped cursor compares wrongly.
    assert "xid xid8 NOT NULL DEFAULT pg_current_xact_id()" in ddl


def test_a_payload_columns_nullability_reaches_the_ddl():
    # `null=False` that renders as nullable is a constraint the declaration
    # claims and the table does not have -- and nothing downstream would notice
    # until a NULL arrived.
    create = _log().statements()[0]
    assert "    actor text NOT NULL" in create
    assert "    body jsonb," in create


def test_the_cursor_index_leads_with_the_ordering_key():
    statements = _log().statements()
    assert any("(xid, seq)" in s for s in statements)
    assert any("(stream, xid, seq)" in s for s in statements)


def test_a_keep_forever_log_declares_no_age_index():
    # Nothing purges by age, so an index on `at` would be a write cost with no
    # reader.
    assert not any("_at_idx" in s for s in _log(retain=KEEP_FOREVER).statements())
    assert any("_at_idx" in s for s in _log(retain=3600).statements())


def test_dedup_declares_a_unique_index_scoped_to_the_stream():
    statements = _log(dedup=True).statements()
    unique = [s for s in statements if "_dedup_idx" in s]
    assert len(unique) == 1
    assert "UNIQUE" in unique[0]
    # Scoped to the stream: two subjects may legitimately carry the same
    # idempotency key, and a global unique index would drop the second one.
    assert "(stream, dedup) WHERE dedup IS NOT NULL" in unique[0]


def test_dedup_declares_the_column_the_index_is_over():
    # The index and the column are two statements, and an index over a column
    # the table does not have fails at apply time -- a long way from here.
    create = _log(dedup=True).statements()[0]
    assert "dedup text" in create
    assert "dedup text" not in _log(dedup=False).statements()[0]


def test_the_component_names_the_relation_it_needs():
    component = _log().schema_claim("audit_log")
    assert component.name == "audit_log"
    assert component.relations == ("audit_records",)
    assert component.schema == "wreath"


def test_an_unqualified_log_lands_where_search_path_points():
    assert _log(schema="").qualified_table == "audit_records"
    assert _log().qualified_table == '"wreath".audit_records'


# -- generated statements --------------------------------------------------


class _Database:
    """Enough of `wreath.postgres.Database` to register statements."""

    def __init__(self) -> None:
        self.registered: dict[str, str] = {}

    def statement(self, name: str, sql: str, *, workload: str = "read") -> object:
        if name in self.registered:
            raise ValueError(f"duplicate PostgreSQL statement: {name}")
        self.registered[name] = sql
        return object()


def test_every_read_stops_at_the_horizon():
    log = PostgresLog(_Database(), _log())
    for name in ("read", "read_all"):
        sql = log.sql(name)
        assert "pg_snapshot_xmin(pg_current_snapshot())" in sql, name
        # The row comparison, not the OR-expansion: the planner drives the
        # composite index from one and has to work to recognise the other.
        assert "(l.xid, l.seq) >" in sql, name
        assert "ORDER BY l.xid, l.seq" in sql, name


def test_a_keep_forever_log_has_no_purge_statement():
    log = PostgresLog(_Database(), _log(retain=KEEP_FOREVER))
    with pytest.raises(ValueError, match="no SQL named 'purge'"):
        log.sql("purge")


def test_a_retaining_log_has_a_purge_statement_carrying_its_window():
    # Both directions, so "there is no purge" is a fact about KEEP_FOREVER
    # rather than about the statement never being defined at all.
    log = PostgresLog(_Database(), _log(retain=3600))
    sql = log.sql("purge")
    assert "DELETE FROM" in sql
    assert "3600.0" in sql
    # `clock_timestamp()`, never `now()`: inside a transaction `now()` is frozen
    # at its start, so a long purge would age rows against a stale clock.
    assert "clock_timestamp()" in sql


def test_a_prepared_statement_name_that_would_be_truncated_is_refused():
    # PostgreSQL truncates at 63 bytes rather than refusing, so two logs
    # agreeing in their first 63 would share one prepared statement.
    with pytest.raises(ValueError, match="PostgreSQL truncates"):
        PostgresLog(_Database(), _log(table="a" * 60))


def test_a_dedup_log_binds_the_dedup_column_in_its_insert():
    # The unique index is only half of it: the statement has to actually supply
    # the column, or `ON CONFLICT DO NOTHING` conflicts on nothing and every
    # retry appends a second row.
    log = PostgresLog(_Database(), _log(dedup=True))
    sql = log.sql("append_once")
    assert "dedup" in sql.split("\n")[0]
    assert "ON CONFLICT DO NOTHING" in sql
    # Four columns bound: the stream, two payload columns, and dedup.
    assert "VALUES ($1, $2, $3, $4)" in sql


def test_a_log_without_dedup_binds_no_dedup_column():
    log = PostgresLog(_Database(), _log(dedup=False))
    assert "dedup" not in log.sql("append")
    assert "VALUES ($1, $2, $3)" in log.sql("append")


def test_append_once_is_refused_on_a_log_with_no_unique_index():
    log = PostgresLog(_Database(), _log(dedup=False))
    with pytest.raises(ValueError, match="declared without dedup=True"):
        import asyncio

        asyncio.run(log.append_once("s", dedup="k", actor="me"))


def test_a_log_without_dedup_declares_no_append_once_statement_at_all():
    # Not the same claim as the refusal above. A statement defined here is a
    # statement *prepared* on every connection of the pool the first time it is
    # asked for, and one whose `ON CONFLICT DO NOTHING` has no unique index to
    # conflict on would silently do nothing rather than de-duplicate. So the
    # right state for a log that did not ask for dedup is that the SQL does not
    # exist, which is a different assertion from "calling it raises".
    log = PostgresLog(_Database(), _log(dedup=False))
    with pytest.raises(ValueError, match="no SQL named 'append_once'"):
        log.sql("append_once")
    assert "append_once" in PostgresLog(_Database(), _log(dedup=True))._defined


@pytest.mark.parametrize("limit", [0, -1, -512])
def test_a_read_with_a_non_positive_limit_is_refused(limit):
    # `LIMIT 0` returns nothing and advances no cursor, so a reader that asked
    # for it would poll forever seeing an empty log; a negative one is a
    # PostgreSQL error from three frames further down. Refused here, where the
    # message can say which argument was wrong.
    import asyncio

    log = PostgresLog(_Database(), _log())
    with pytest.raises(ValueError, match="limit must be positive"):
        asyncio.run(log.read("s", after=Cursor.start(), limit=limit))


# -- binding ---------------------------------------------------------------


def _bind(log: PostgresLog, stream: str, **values: object) -> list[object]:
    return log._bind(stream, dict(values))


def test_binding_orders_the_payload_the_way_the_statement_expects():
    log = PostgresLog(_Database(), _log())
    assert _bind(log, "s", body={"a": 1}, actor="me") == ["s", "me", {"a": 1}]


def test_a_missing_not_null_column_names_the_column_not_the_placeholder():
    log = PostgresLog(_Database(), _log())
    with pytest.raises(ValueError, match="actor is NOT NULL and was not supplied"):
        _bind(log, "s", body=None)


def test_a_nullable_column_may_simply_be_left_out():
    log = PostgresLog(_Database(), _log())
    assert _bind(log, "s", actor="me") == ["s", "me", None]


def test_a_misspelled_column_is_refused_by_name():
    log = PostgresLog(_Database(), _log())
    # The refusal names both halves: what was passed, and what the log actually
    # declares. Asserting only the first would pass whichever branch fired.
    with pytest.raises(ValueError, match="declares no column named bodyy; it has actor, body"):
        _bind(log, "s", actor="me", bodyy=1)


@pytest.mark.parametrize("stream", ["", None, 7, b"bytes"])
def test_a_row_needs_a_stream_that_is_a_non_empty_string(stream):
    # Both halves of the guard: not a string at all, and a string with nothing
    # in it. A stream name is a partition key, and an empty or non-string one
    # silently collects rows nobody can read back by name.
    log = PostgresLog(_Database(), _log())
    with pytest.raises(ValueError, match="non-empty stream name"):
        _bind(log, stream, actor="me")


def test_a_log_with_no_payload_columns_still_names_what_it_has():
    # A log may legitimately carry nothing but a stream -- "this happened" is a
    # record. The refusal has to stay readable there, and "it has " with nothing
    # after it is not.
    log = PostgresLog(_Database(), _log(columns=()))
    assert _bind(log, "s") == ["s"]
    with pytest.raises(ValueError, match="declares no column named body; it has none"):
        _bind(log, "s", body=1)


# -- batches ---------------------------------------------------------------


def test_an_empty_batch_returns_the_cursor_it_was_given():
    # Otherwise a quiet log rewinds to the start on every poll and replays its
    # whole tail.
    log = PostgresLog(_Database(), _log())
    after = Cursor(7, 3)
    assert log._batch([], after) == Batch((), after)


def test_a_batch_carries_the_cursor_of_its_last_row():
    log = PostgresLog(_Database(), _log())
    rows = [("100", 1, "s", "me", None), ("101", 2, "s", "you", None)]
    batch = log._batch(rows, Cursor.start())
    assert batch.cursor == Cursor(101, 2)
    assert [record.values["actor"] for record in batch] == ["me", "you"]
    assert batch.records[0] == Record(Cursor(100, 1), "s", {"actor": "me", "body": None})


# -- the batched append ----------------------------------------------------


def test_the_rungs_are_powers_of_two_down_to_one():
    log = PostgresLog(_Database(), _log())
    assert log._rungs == (512, 256, 128, 64, 32, 16, 8, 4, 2, 1)
    # Descending and ending at one is what makes the search for a rung total:
    # there is always one that fits whatever is left.
    assert log._rungs[-1] == 1


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [(1, 1), (2, 2), (3, 2), (7, 4), (8, 8), (511, 256), (512, 512), (1000, 512)],
)
def test_a_batch_takes_the_largest_rung_that_fits(remaining, expected):
    log = PostgresLog(_Database(), _log())
    assert log._rung_for(remaining) == expected


def test_a_batch_of_any_size_decomposes_into_its_rungs():
    log = PostgresLog(_Database(), _log())
    for size in (1, 3, 7, 100, 333, 512, 513, 1000, 1300):
        remaining = size
        taken = []
        while remaining:
            rung = log._rung_for(remaining)
            taken.append(rung)
            remaining -= rung
        assert sum(taken) == size
        # Nothing repeats below the top rung, so the statement count is bounded
        # by the number of rungs plus however many whole top rungs it took.
        assert len(taken) <= len(log._rungs) + size // log._rungs[0]


def test_the_batch_statement_binds_every_column_of_every_row():
    log = PostgresLog(_Database(), _log())
    sql = log.sql("b4")
    # Three columns -- the stream and two payload columns -- times four rows.
    assert "($1, $2, $3), ($4, $5, $6), ($7, $8, $9), ($10, $11, $12)" in sql
    assert "RETURNING" not in sql, "a batched insert promises a count, not cursors"


def test_the_batch_statement_omits_the_dedup_column():
    # `append_once` is a one-row conflict check and a batch has no per-row answer
    # to hand back, so the batched path carries the payload and nothing else.
    log = PostgresLog(_Database(), _log(dedup=True))
    assert "dedup" not in log.sql("b2")
    assert "dedup" in log.sql("append_once")


def test_the_top_rung_is_clamped_by_postgresqls_parameter_limit():
    # 65535 parameters per statement is a hard server limit, so a wide log takes
    # a lower top rung rather than building a statement the server refuses.
    wide = PostgresLog(
        _Database(),
        _log(columns=tuple(Column(f"c{index}", "text") for index in range(199))),
    )
    assert wide._rungs[0] * len(wide._batch_columns) <= 65535
    assert wide._rungs[0] == 256
    narrow = PostgresLog(_Database(), _log())
    assert narrow._rungs[0] == 512


def test_a_command_tag_that_is_not_one_is_refused_rather_than_guessed_at():
    from wreath.log import _rows_in

    assert _rows_in("INSERT 0 512") == 512
    with pytest.raises(RuntimeError, match="malformed command tag"):
        _rows_in("INSERT 0 lots")
    with pytest.raises(RuntimeError, match="expected a PostgreSQL command tag"):
        _rows_in(None)


class _Silent:
    """A connection that answers nothing, for the guards that expect a row.

    `connection=` is duck-typed by design -- a caller passes whatever its ORM
    session is holding -- so "the driver came back with no row" is reachable
    from outside this module rather than only from a broken PostgreSQL.
    """

    def __init__(self, tag: str = "INSERT 0 0") -> None:
        self.tag = tag

    async def fetchrow(self, sql: str, *args: object) -> None:
        return None

    async def execute(self, sql: str, *args: object) -> str:
        return self.tag


def test_an_append_that_comes_back_with_no_row_says_so():
    import asyncio

    log = PostgresLog(_Database(), _log())
    with pytest.raises(RuntimeError, match="cannot legitimately be empty"):
        asyncio.run(log.append("s", connection=_Silent(), actor="me"))


def test_a_batch_that_reports_fewer_rows_than_it_sent_says_so():
    import asyncio

    log = PostgresLog(_Database(), _log())
    with pytest.raises(RuntimeError, match="reported 0 rows for a batch of 2"):
        asyncio.run(
            log.append_many(
                [("s", {"actor": "me"}), ("s", {"actor": "you"})],
                connection=_Silent(),
            )
        )


def test_an_empty_batch_runs_no_statement_at_all():
    import asyncio

    log = PostgresLog(_Database(), _log())
    # `_Silent` would raise on any statement it was handed, so reaching zero
    # proves nothing was sent rather than that something harmless was.
    assert asyncio.run(log.append_many([], connection=_Silent())) == 0


def test_a_batch_is_bound_in_full_before_any_statement_runs():
    import asyncio

    log = PostgresLog(_Database(), _log())
    rows = [("s", {"actor": "fine"})] * 4 + [("s", {"actor": "me", "bodyy": 1})]
    with pytest.raises(ValueError, match="declares no column named bodyy"):
        asyncio.run(log.append_many(rows, connection=_Silent("INSERT 0 4")))


# -- retention -------------------------------------------------------------


def test_a_retention_walk_is_declared_over_the_logs_own_table():
    from wreath.log import retention_pass

    walk = retention_pass(_log(retain=3600.0), name="purge_audit")
    assert walk.name == "purge_audit"
    assert walk.table == '"wreath"."audit_records"'
    # `at` leads because it is the domain the window is measured in, and `seq`
    # follows because a `clock_timestamp()` is not unique and a boundary that is
    # not unique either skips its siblings or loops on them.
    assert [key.name for key in walk.units.keys] == ["at", "seq"]
    assert walk.units.keys[0].indexed is True
    assert walk.units.keys[1].unique is True
    # The frontier *is* the retention window.
    assert walk.frontier.after == 3600.0
    assert walk.recurring is True
    # One undeletable row must not stop the table being kept small forever.
    assert walk.on_chunk_failure == "skip"


def test_a_retention_walk_over_a_keep_forever_log_is_refused():
    from wreath.log import retention_pass

    # The whole erasure design rests on this: retention never carries the
    # transaction-scoped setting the append-only trigger looks for, so the only
    # way an audit record is deleted is an act that names its subject.
    with pytest.raises(ValueError, match="scheduled deletion of evidence"):
        retention_pass(_log(retain=KEEP_FOREVER), name="purge_audit")


def test_a_retention_walk_can_be_asked_for_from_the_log_itself():
    from wreath.log import retention_pass

    declaration = _log(retain=3600.0)
    log = PostgresLog(_Database(), declaration)
    assert log.retention_pass(name="p").table == retention_pass(
        declaration, name="p"
    ).table


def test_an_unqualified_logs_retention_walk_follows_the_search_path():
    from wreath.log import retention_pass

    walk = retention_pass(_log(retain=60.0, schema=""), name="purge_bare")
    assert walk.table == "audit_records"


def test_the_age_index_the_retention_walk_needs_is_declared_with_it():
    # The walk refuses a leading key column with no index, and this is the
    # index: the two have to be declared by the same `retain=`.
    declaration = _log(retain=60.0)
    assert any("_at_idx" in statement for statement in declaration.statements())
