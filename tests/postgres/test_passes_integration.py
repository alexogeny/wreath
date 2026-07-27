"""Live-PostgreSQL checks for the two chunked-pass claims a fake cannot settle.

Skipped unless ``WREATH_TEST_POSTGRES_DSN`` points at a throwaway database. The
fake-driver suite in ``tests/passes/`` proves the shapes; these two prove the
things only a real planner and a real lock manager can:

* **the composite keyset really is one index scan** -- ``EXPLAIN``, not faith.
  The whole complexity argument for keyset over ``OFFSET`` rests on it, and it
  is exactly the sort of claim that stays true in a comment long after an
  expanded ``OR`` has quietly replaced it with a bitmap-or over two scans;
* **a chunk that cannot get its lock dies as a chunk failure**, on
  ``statement_timeout``, rather than becoming the long-running transaction the
  whole design exists to prevent. That is the failure mode that turns a
  backfill into a stuck lease and then into two workers on one pass.
"""

from __future__ import annotations

import asyncio
import datetime
import os

import pytest

from wreath.passes import ChunkedPass, DutyCycle, Key, Purge, Rows, Sealed, Table
from wreath.postgres import Database

pytestmark = pytest.mark.skipif(
    not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
    reason="set WREATH_TEST_POSTGRES_DSN to run live chunked-pass integration tests",
)

#: One schema per xdist worker. Six workers sharing one schema race on
#: `CREATE SCHEMA IF NOT EXISTS` and `DROP SCHEMA CASCADE` -- `IF NOT EXISTS` is
#: not atomic against a concurrent creator -- and PostgreSQL reports the race as
#: `duplicate key value violates unique constraint "pg_type_typname_nsp_index"`,
#: a catalog error nobody would read as a test-isolation problem. Measured:
#: green serially, failing under `-n 6`. Same shape and same fix as
#: `tests/test_replay_live_faults.py`.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_test_passes_{_WORKER}"
_TABLE = f'"{_SCHEMA}".replays'


async def _apply(database, sql):
    connection = await database.acquire("write")
    try:
        for statement in (part.strip() for part in sql.split(";\n")):
            if statement:
                await connection.execute(statement)
    finally:
        await database.release("write", connection)


@pytest.fixture
async def database():
    dsn = os.environ["WREATH_TEST_POSTGRES_DSN"]
    db = Database("main", dsn, pools={"write": {"min_size": 1, "max_size": 4}})
    await db.start()
    from wreath.passes import schema_sql

    await _apply(db, f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"')
    await _apply(db, schema_sql(_SCHEMA))
    await _apply(
        db,
        f"CREATE TABLE IF NOT EXISTS {_TABLE} (\n"
        "  key text PRIMARY KEY,\n"
        "  herd_id bigint NOT NULL,\n"
        "  expires timestamptz NOT NULL\n"
        ");\n"
        f'CREATE INDEX IF NOT EXISTS replays_expires_key_idx ON {_TABLE} (expires, key);\n'
        f'CREATE INDEX IF NOT EXISTS replays_herd_key_idx ON {_TABLE} (herd_id, key)',
    )
    await _apply(db, f"TRUNCATE {_TABLE}")
    await _apply(db, f'TRUNCATE "{_SCHEMA}".passes')
    try:
        yield db
    finally:
        await _apply(db, f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await db.stop()


async def _seed(database, count):
    now = datetime.datetime.now(datetime.UTC)
    connection = await database.acquire("write")
    try:
        for index in range(count):
            await connection.execute(
                f"INSERT INTO {_TABLE} (key, herd_id, expires) VALUES ($1, $2, $3)",
                f"k{index:05d}",
                index % 10,
                now - datetime.timedelta(seconds=count - index),
            )
    finally:
        await database.release("write", connection)


def _purge(**overrides):
    options = {
        "over": Table("replays", schema=_SCHEMA),
        "units": Rows(
            key=(
                Key("expires", "timestamptz", indexed=True),
                Key("key", "text", unique=True),
            ),
            limit=50,
            within="2s",
        ),
        "frontier": Sealed(),
        "work": Purge(),
        "pace": DutyCycle(1.0),
        "schema": _SCHEMA,
    }
    options.update(overrides)
    return ChunkedPass("purge_replays", **options)


async def _explain(connection, sql, *args):
    # `Record` is a sequence with positional and column-name access -- no
    # `.keys()`, no `.values()`. `EXPLAIN (FORMAT TEXT)` returns one column, so
    # row[0] is the plan line.
    rows = await connection.fetch("EXPLAIN (FORMAT TEXT) " + sql, *args)
    return "\n".join(str(row[0]) for row in rows)


async def test_a_composite_keyset_really_is_one_index_scan(database):
    """The complexity argument's first correctness condition, checked by the planner.

    The discriminator is **`Index Cond` versus `Filter`**, not the scan node.
    Both the row comparison and the expanded-OR form come back as an "Index Only
    Scan" on this table, so asserting on the node name would have passed for the
    regression it exists to catch. Only the row comparison reaches the index as a
    *condition* -- the OR form is a filter, which means reading the whole index
    and discarding, which is the `N` in the `OFFSET` arithmetic wearing a
    different plan.
    """
    await _seed(database, 500)
    connection = await database.acquire("write")
    try:
        await connection.execute("ANALYZE " + _TABLE)
        keyset = await _explain(
            connection,
            f"SELECT herd_id, key FROM {_TABLE} WHERE (herd_id, key) > ($1, $2) "
            "ORDER BY herd_id, key LIMIT 50",
            3,
            "k00100",
        )
        expanded = await _explain(
            connection,
            f"SELECT herd_id, key FROM {_TABLE} "
            "WHERE (herd_id > $1 OR (herd_id = $1 AND key > $2)) "
            "ORDER BY herd_id, key LIMIT 50",
            3,
            "k00100",
        )
    finally:
        await database.release("write", connection)

    assert "Index Scan" in keyset or "Index Only Scan" in keyset
    # The row comparison is pushed into the index as a seek.
    assert "Index Cond" in keyset
    assert "ROW(herd_id, key) > ROW(" in keyset
    assert "Filter:" not in keyset
    # A bitmap-or or a sort would mean the comparison had been expanded.
    assert "BitmapOr" not in keyset
    assert "Sort" not in keyset

    # And the control: the same predicate written as ORs is *not* a seek, which
    # is what makes the assertions above discriminating rather than decorative.
    assert "Filter:" in expanded
    assert "Index Cond" not in expanded


async def test_a_chunk_that_cannot_get_its_lock_fails_as_a_chunk(database):
    """A lock wait must end the chunk, not extend the transaction past the lease."""
    await _seed(database, 100)
    walk = _purge(units=Rows(
        key=(
            Key("expires", "timestamptz", indexed=True),
            Key("key", "text", unique=True),
        ),
        limit=50,
        within="250ms",
    ))

    # Hold a row the first chunk must delete, from another connection, for
    # longer than the chunk's own budget.
    blocker = await database.acquire("write")
    try:
        await blocker.execute("BEGIN")
        await blocker.execute(
            f"SELECT key FROM {_TABLE} ORDER BY expires, key LIMIT 1 FOR UPDATE"
        )

        started = asyncio.get_running_loop().time()
        result = await walk.run_shift(database, sleep=lambda _s: asyncio.sleep(0))
        elapsed = asyncio.get_running_loop().time() - started

        # §6.4 is about the *shape* of the failure, not its label. What it
        # forbids is the chunk becoming a transaction that outlives its lease;
        # the state it lands in afterwards is whatever the declared policy says.
        assert result.error is not None
        assert "statement timeout" in result.error
        # It gave up on its own budget rather than waiting out the blocker: three
        # attempts at 250ms, nowhere near the blocker's lifetime.
        assert elapsed < 5.0
        # The hole is recorded with its position and something that reproduces it.
        assert result.holes == 1
        status = await walk.status(database)
        assert status.holes_open == 1
        # And it bars the gate, so no irreversible step can run over the gap.
        assert status.gate_barred is True
        # The cursor did not move, so the chunk is retried from its start.
        assert status.cursor is None

        # The label follows the declared failure policy, and `halt` is the
        # default -- it parks the cursor before the hole and stops. `skip` is the
        # policy that reports `failed`, because it advances past the hole and
        # keeps going. Asserting the policy rather than one string is what stops
        # this test going stale the next time a default moves.
        assert walk.on_chunk_failure == "halt"
        assert result.stopped == "blocked"
    finally:
        await blocker.execute("ROLLBACK")
        await database.release("write", blocker)


async def test_a_walk_over_a_real_table_finishes_and_leaves_nothing_behind(database):
    await _seed(database, 220)
    walk = _purge()

    result = await walk.run(database, sleep=lambda _s: asyncio.sleep(0))

    assert result.complete is True
    assert result.rows == 220
    connection = await database.acquire("write")
    try:
        assert await connection.fetchval(f"SELECT count(*) FROM {_TABLE}") == 0
    finally:
        await database.release("write", connection)


async def test_the_cursor_and_the_work_commit_together(database):
    """A crash between them is the one thing one transaction per chunk rules out."""
    await _seed(database, 120)
    walk = _purge()

    await walk.run_shift(database, budget=0.0, sleep=lambda _s: asyncio.sleep(0))
    result = await walk.run(database, sleep=lambda _s: asyncio.sleep(0))

    connection = await database.acquire("write")
    try:
        remaining = await connection.fetchval(f"SELECT count(*) FROM {_TABLE}")
        units = await connection.fetchval(
            f'SELECT units_done FROM "{_SCHEMA}".passes WHERE name = $1', "purge_replays"
        )
    finally:
        await database.release("write", connection)

    # Every chunk that advanced the cursor also deleted its rows, so the counts
    # cannot disagree: 120 rows in chunks of 50 is three units.
    assert remaining == 0
    assert units == 3
    assert result.complete is True


# --- stage two and three, against a real planner -------------------------------


async def test_the_dead_letter_table_is_created_by_the_same_ddl(database):
    # One `schema_sql()` for both tables, because a hole with nowhere to go is
    # the silent skip the whole design refuses -- and a pass whose ledger
    # migrated but whose dead-letter table did not would do exactly that.
    connection = await database.acquire("write")
    try:
        present = await connection.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", f"{_SCHEMA}.pass_holes"
        )
    finally:
        await database.release("write", connection)

    assert present is True


async def test_reltuples_answers_for_a_real_table(database):
    # The free denominator, and the reason it is the default. It is also the one
    # that can answer -1 for a table ANALYZE has never seen, which is why the
    # code checks rather than trusts.
    await _seed(database, 200)
    connection = await database.acquire("write")
    try:
        await connection.execute(f"ANALYZE {_TABLE}")
        estimate = await connection.fetchval(
            "SELECT reltuples::bigint FROM pg_class WHERE oid = $1::regclass", _TABLE
        )
    finally:
        await database.release("write", connection)

    assert estimate is not None
    assert estimate >= 0


async def test_the_denominator_can_be_measured_more_than_once(database):
    """The default denominator, called twice on one connection.

    `$1::regclass` made PostgreSQL infer the *parameter* as `regclass`, which no
    binary encoder here can write. The first execution survived it and every
    later one raised, so a pass measured once and then failed forever -- and a
    recurring pass re-measures every cycle. Only a real server infers the type,
    so no fake could have caught it, and one call could not either.
    """
    from wreath._passes.progress import Estimated

    await _seed(database, 40)
    connection = await database.acquire("write")
    try:
        await connection.execute("ANALYZE " + _TABLE)
        first = await Estimated().measure(connection, table=_TABLE, keys=())
        second = await Estimated().measure(connection, table=_TABLE, keys=())
        third = await Estimated().measure(connection, table=_TABLE, keys=())
        # A table that is not there is None, not an exception: `to_regclass`
        # answers NULL where the cast would have raised.
        missing = await Estimated().measure(
            connection, table='"nope"."nope"', keys=()
        )
    finally:
        await database.release("write", connection)

    assert first == second == third == 40
    assert missing is None


async def test_the_rate_window_rolls_over_on_the_databases_clock(database):
    # `now()` rather than `clock_timestamp()` in the window update, so the
    # rollover test cannot disagree with itself between the CASE arms inside one
    # transaction. A real server is the only place that distinction is visible.
    await _seed(database, 200)
    walk = _purge()
    await walk.run(database)

    status = await walk.status(database)

    assert status.progress.denominator_kind == "estimated"
    # Several chunks ran inside one window, so there is a real rate to report.
    assert status.units_done > 1


async def test_a_requeued_unit_survives_a_round_trip_through_jsonb(database):
    # The pending array is jsonb, and a cursor encodes timestamps as ISO
    # strings. A unit that cannot be read back is a hole that can never clear.
    await _seed(database, 60)
    walk = _purge()
    await walk.run(database)

    stamp = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    assert await walk.requeue(database, (stamp, "kzzz")) is True

    status = await walk.status(database)
    assert status.pending == 1


# --- the terminal gate --------------------------------------------------------


async def test_validate_constraint_takes_only_share_update_exclusive(database):
    """The claim §10.4 rests on, and the one a fake driver can never settle.

    ``VALIDATE CONSTRAINT`` is worth reaching for only because it scans without
    blocking reads or writes. If it took ``ACCESS EXCLUSIVE`` the whole argument
    would invert: verification would be the outage the pass spent an hour of
    chunking to avoid.
    """
    await _apply(
        database,
        f"DROP TABLE IF EXISTS {_TABLE} CASCADE;\n"
        f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}";\n'
        f"CREATE TABLE {_TABLE} (key text PRIMARY KEY, "
        "expires timestamptz NOT NULL, converted text);\n"
        f"INSERT INTO {_TABLE} (key, expires, converted) "
        "VALUES ('a', now(), 'x'), ('b', now(), 'y');\n",
    )
    connection = await database.acquire("write")
    try:
        await connection.execute(
            f"ALTER TABLE {_TABLE} ADD CONSTRAINT converted_present "
            "CHECK (converted IS NOT NULL) NOT VALID"
        )
        async with connection.transaction() as tx:
            await tx.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT converted_present")
            modes = await tx.fetch(
                "SELECT mode FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                "WHERE c.relname = 'replays' AND l.pid = pg_backend_pid()"
            )
        held = {row["mode"] for row in modes}
        assert "ShareUpdateExclusiveLock" in held
        assert "AccessExclusiveLock" not in held
    finally:
        await connection.execute(
            f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS converted_present"
        )
        await database.release("write", connection)


async def test_a_constraint_that_does_not_hold_names_the_offending_row(database):
    # The other half of why the database is the right verifier: when it says no
    # it says which row, which a hand-written SELECT would have to be asked for
    # separately and usually is not.
    from wreath._passes.gate import Constraint

    await _apply(
        database,
        f"DROP TABLE IF EXISTS {_TABLE} CASCADE;\n"
        f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}";\n'
        f"CREATE TABLE {_TABLE} (key text PRIMARY KEY, "
        "expires timestamptz NOT NULL, converted text);\n"
        f"INSERT INTO {_TABLE} (key, expires, converted) "
        "VALUES ('a', now(), 'x'), ('b', now(), NULL);\n",
    )
    connection = await database.acquire("write")
    try:
        verdict = await Constraint("converted_present", "converted IS NOT NULL").check(
            connection, walk=_purge()
        )
        assert verdict.ok is False
        # A logic error, not a could-not-run: this must not be retried.
        assert verdict.transient is False
        assert "does not hold" in verdict.detail
    finally:
        await connection.execute(
            f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS converted_present"
        )
        await database.release("write", connection)


async def test_a_bucketed_walk_agrees_with_date_trunc_on_a_real_server(database):
    """Whether Python's bucket boundaries match the database's.

    Deliberately an integration test rather than a claim in the guide: the
    ordering of ``date_trunc`` against ``AT TIME ZONE`` is reasoned from
    documented semantics everywhere else in this codebase, and reasoning is not
    measurement. Auckland is the interesting zone because its transitions land
    on dates the northern hemisphere never exercises.
    """
    connection = await database.acquire("write")
    try:
        for moment in (
            datetime.datetime(2026, 4, 5, 3, tzinfo=datetime.UTC),
            datetime.datetime(2026, 9, 27, 15, tzinfo=datetime.UTC),
            datetime.datetime(2026, 7, 27, 12, tzinfo=datetime.UTC),
        ):
            from wreath.temporal import Day

            theirs = await connection.fetchval(
                "SELECT (date_trunc('day', $1::timestamptz AT TIME ZONE "
                "'Pacific/Auckland') AT TIME ZONE 'Pacific/Auckland')",
                moment,
            )
            ours = Day.floor(moment, "Pacific/Auckland")
            assert theirs == ours.astimezone(datetime.UTC), moment
    finally:
        await database.release("write", connection)


# --- the rewrite record, and the rule that it cannot be deleted ---------------


async def _fact_state(database, *, fact):
    """What ``rewritten_columns`` says about *fact*, through the real driver."""
    from wreath._passes import ledger as _ledger

    connection = await database.acquire("write")
    try:
        return await _ledger.rewritten_columns(
            connection, schema=_SCHEMA, facts=(fact,)
        )
    finally:
        await database.release("write", connection)


@pytest.mark.asyncio
async def test_the_rewrite_record_refuses_delete_update_and_truncate(database) -> None:
    """The belt: a database rule, not a convention.

    All three matter and ``TRUNCATE`` is the one that is easy to miss -- it does
    not fire row-level triggers, so a guard written only as ``BEFORE DELETE``
    leaves the whole record one ``TRUNCATE`` away from being gone.
    """
    from wreath._passes.ledger import Ledger, rewrites_table_name

    fact = "column:app.treks.guarded_status"
    ledger = Ledger(schema=_SCHEMA, name="recode_guarded")
    connection = await database.acquire("write")
    try:
        await ledger.seed(connection, chunk_limit=100, rewrites=fact)
        table = rewrites_table_name(_SCHEMA)
        before = await connection.fetchval(
            f"SELECT count(*) FROM {table} WHERE fact = $1", fact
        )
        assert before == 1, "seeding a re-encoding pass must write the record"

        for statement in (
            f"DELETE FROM {table} WHERE fact = $1",
            f"UPDATE {table} SET fact = 'column:app.treks.other' WHERE fact = $1",
        ):
            with pytest.raises(Exception, match="append-only"):
                await connection.execute(statement, fact)
        with pytest.raises(Exception, match="append-only"):
            await connection.execute(f"TRUNCATE {table}")

        after = await connection.fetchval(
            f"SELECT count(*) FROM {table} WHERE fact = $1", fact
        )
        assert after == 1, "the record survived every attempt to remove it"
    finally:
        await database.release("write", connection)


@pytest.mark.asyncio
async def test_a_purged_ledger_row_leaves_the_hazard_standing(database) -> None:
    """The braces: what the refusal reads when the belt has been bypassed.

    Three states, and the middle one is the whole point. "Never re-encoded" and
    "re-encoded, then tidied up" are both an absent ledger row; only the record
    tells them apart, and getting that wrong in the unsafe direction is a
    downgrade that reports success over data it has silently stranded.
    """
    from wreath._passes.ledger import Ledger, table_name

    fact = "column:app.treks.purged_status"
    untouched = "column:app.treks.never_recoded"
    ledger = Ledger(schema=_SCHEMA, name="recode_purged")
    connection = await database.acquire("write")
    try:
        await ledger.seed(connection, chunk_limit=100, rewrites=fact)

        found = await _fact_state(database, fact=fact)
        assert [(f.name, f.ledger_row_present) for f in found] == [
            ("recode_purged", True)
        ]
        assert await _fact_state(database, fact=untouched) == []

        # The purge job nobody has written yet, but plausibly will.
        await connection.execute(
            f"DELETE FROM {table_name(_SCHEMA)} WHERE name = $1", "recode_purged"
        )

        found = await _fact_state(database, fact=fact)
        assert [(f.name, f.ledger_row_present) for f in found] == [
            ("recode_purged", False)
        ], "the record must outlive the ledger row and keep the hazard"
        assert (
            await _fact_state(database, fact=untouched) == []
        ), "a column nothing re-encoded stays downgradeable"
    finally:
        await database.release("write", connection)


@pytest.mark.asyncio
async def test_every_schema_sql_statement_runs_on_its_own(database) -> None:
    """Each statement must survive the ``;\\n`` split every caller performs.

    The trigger function's body contains semicolons, so it is written on one
    line for exactly this reason. A future edit that wraps it would split the
    function in half and fail at apply time, which is the kind of breakage that
    only shows up on someone's first deployment.
    """
    from wreath.passes import schema_sql

    schema = f"wreath_test_split_probe_{_WORKER}"
    statements = [
        part.strip() for part in schema_sql(schema).split(";\n") if part.strip()
    ]
    assert len(statements) >= 7, "expected schema, three tables, a function, two triggers"
    connection = await database.acquire("write")
    try:
        for statement in statements:
            await connection.execute(statement)
        # Idempotent: applying twice is how a redeploy reaches it.
        for statement in statements:
            await connection.execute(statement)
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await database.release("write", connection)
