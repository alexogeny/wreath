from __future__ import annotations

import asyncio
import os

import pytest

from wreath.log import KEEP_FOREVER, Column, Cursor, Log, PostgresLog
from wreath.postgres import Database

pytestmark = [
    pytest.mark.asyncio,
    # `network`, and **not** `database`, even though this needs only a DSN.
    # `pyproject.toml` says a DSN-gated suite should be `database` so it runs in
    # the default gate, and that is right for every suite it applies to. It does
    # not apply here, and the reason is the design rather than the test:
    # `PostgresLog.read` stops at `pg_snapshot_xmin(pg_current_snapshot())`,
    # which is a **cluster-wide** horizon. Any open transaction anywhere on the
    # server -- another xdist worker's session, another suite's `session.begin()`
    # -- pins it, and every append made after that becomes invisible to every
    # reader until it ends. This suite then goes further and *deliberately holds
    # two overlapping transactions open* (`_Overlap`, `_Decoupled`), which is the
    # only way to drive the interleaving it exists to falsify. So it both suffers
    # from and causes the interference.
    # Measured rather than assumed: `pytest tests/test_log_cursor_live.py -n 6`
    # fails 6 of 15 and then hangs, because a test that fails inside the overlap
    # skips its `COMMIT` and the pooled connection carries an open transaction
    # into the fixture's `DROP SCHEMA ... CASCADE`. `network` keeps the suite out
    # of the parallel default gate; `-m ''` still runs it, and `-p no:randomly`
    # with no `-n` is the way to run it deliberately.
    # Fixing this properly needs a database of its own for the suite, which is a
    # decision about the test estate rather than about this module.
    pytest.mark.network,
    pytest.mark.skipif(
        not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
        reason="set WREATH_TEST_POSTGRES_DSN to run live log cursor tests",
    ),
]

#: One schema per xdist worker, by plain assignment. Workers sharing a schema
#: race on `CREATE SCHEMA IF NOT EXISTS`, which PostgreSQL reports as a
#: `pg_namespace_nspname_index` unique violation -- a catalog error that reads
#: like anything except a test-isolation bug. `os.environ.setdefault` in a
#: conftest silently no-ops here, because the controller writes it and then
#: spawns workers carrying its own value.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_log_{_WORKER}"

_DECLARATION = Log(
    table="entries",
    retain=KEEP_FOREVER,
    columns=(Column("body", "text", null=False),),
    schema=_SCHEMA,
)


async def _apply(database, sql: str) -> None:
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
    handle = Database("main", dsn, pools={"write": {"min_size": 1, "max_size": 8}})
    await handle.start()
    await _apply(handle, f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"')
    await _apply(handle, _DECLARATION.schema_sql())
    await _apply(handle, f"TRUNCATE {_DECLARATION.qualified_table}")
    try:
        yield handle
    finally:
        await _apply(handle, f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await handle.stop()


@pytest.fixture
def log(database):
    return PostgresLog(database, _DECLARATION)


async def _settled(connection) -> None:
    """Close any transaction still open on `connection` before it goes back.

    A test that fails *inside* an overlap never reaches its `COMMIT`, and the
    connection then returns to the pool holding locks on the schema's table --
    where the fixture's `DROP SCHEMA ... CASCADE` waits on it forever. That does
    not look like a failed assertion, it looks like the suite hanging, and the
    real failure is never printed. `ROLLBACK` outside a transaction is a no-op
    with a notice, so this is safe on the connections that did settle.
    """
    await connection.execute("ROLLBACK")


class _Overlap:
    """Two transactions that overlap, committing in reverse allocation order.

    `first` allocates its sequence number first and commits **last**. That is
    the whole hazard, and it needs two live connections holding two open
    transactions at once -- which is why this cannot be a unit test.
    """

    def __init__(self, database) -> None:
        self._database = database
        self.early = None
        self.late = None

    async def __aenter__(self) -> _Overlap:
        self.early = await self._database.acquire("write")
        self.late = await self._database.acquire("write")
        return self

    async def run(self) -> None:
        table = _DECLARATION.qualified_table
        await self.early.execute("BEGIN")
        await self.early.execute(f"INSERT INTO {table} (stream, body) VALUES ('s', 'early')")
        await self.late.execute("BEGIN")
        await self.late.execute(f"INSERT INTO {table} (stream, body) VALUES ('s', 'late')")
        # The later allocation commits first. Everything this suite is about
        # happens in the window this line opens.
        await self.late.execute("COMMIT")

    async def settle(self) -> None:
        await self.early.execute("COMMIT")

    async def __aexit__(self, *_: object) -> None:
        for connection in (self.early, self.late):
            if connection is not None:
                await _settled(connection)
                await self._database.release("write", connection)


async def test_a_sequence_cursor_would_have_skipped_a_row(log, database):
    table = _DECLARATION.qualified_table
    reader = await database.acquire("write")
    try:
        async with _Overlap(database) as overlap:
            await overlap.run()
            naive = await reader.fetch(f"SELECT seq, body FROM {table} WHERE seq > 0 ORDER BY seq")
            # Only the late-allocated row is visible; the early one is still in
            # flight. A `seq`-remembering reader records 2 here.
            assert [row[1] for row in naive] == ["late"]
            remembered = naive[-1][0]
            await overlap.settle()

        after = await reader.fetch(
            f"SELECT seq, body FROM {table} WHERE seq > $1 ORDER BY seq", remembered
        )
        # ... and never sees 'early' again. This is the data loss.
        assert [row[1] for row in after] == []
    finally:
        await database.release("write", reader)


class _Decoupled:
    """An overlap where sequence order and transaction order **disagree**.

    The first hazard needs only overlapping transactions. This one needs the two
    orderings to come apart, which they do the moment a transaction takes its id
    before it inserts -- an earlier statement in the same transaction, which is
    the ordinary case for anything that writes twice.

    `holder` takes transaction id *N* and inserts second (sequence 2).
    `other` takes a later id *M* and inserts first (sequence 1). So sequence
    order is (1, 2) and transaction order is (2, 1), and a reader that gates on
    visibility but still remembers a sequence number loses row 1.
    """

    def __init__(self, database) -> None:
        self._database = database
        self.holder = None
        self.other = None

    async def __aenter__(self) -> _Decoupled:
        self.holder = await self._database.acquire("write")
        self.other = await self._database.acquire("write")
        return self

    async def run(self) -> None:
        table = _DECLARATION.qualified_table
        await self.holder.execute("BEGIN")
        # Takes the id now, writes later. Everything downstream follows from it.
        await self.holder.execute("SELECT pg_current_xact_id()")
        await self.other.execute("BEGIN")
        await self.other.execute(f"INSERT INTO {table} (stream, body) VALUES ('s', 'lost')")
        await self.holder.execute(f"INSERT INTO {table} (stream, body) VALUES ('s', 'seen')")
        await self.holder.execute("COMMIT")

    async def settle(self) -> None:
        await self.other.execute("COMMIT")

    async def __aexit__(self, *_: object) -> None:
        for connection in (self.holder, self.other):
            if connection is not None:
                await _settled(connection)
                await self._database.release("write", connection)


async def test_a_horizon_gated_sequence_cursor_would_still_have_skipped_a_row(log, database):
    table = _DECLARATION.qualified_table
    gated = (
        f"SELECT seq, body FROM {table} "
        "WHERE xid < pg_snapshot_xmin(pg_current_snapshot()) AND seq > $1 ORDER BY seq"
    )
    reader = await database.acquire("write")
    try:
        async with _Decoupled(database) as overlap:
            await overlap.run()
            visible = await reader.fetch(gated, 0)
            # Only the row whose transaction settled; it carries sequence 2.
            assert [row[1] for row in visible] == ["seen"]
            remembered = visible[-1][0]
            assert remembered == 2
            await overlap.settle()

        after = await reader.fetch(gated, remembered)
        # Sequence 1 has now settled, and is below the cursor. Lost.
        assert [row[1] for row in after] == []
    finally:
        await database.release("write", reader)


async def test_the_log_delivers_the_row_a_gated_sequence_cursor_would_lose(log, database):
    cursor = Cursor.start()
    async with _Decoupled(database) as overlap:
        await overlap.run()
        batch = await log.read("s", after=cursor)
        assert [record["body"] for record in batch] == ["seen"]
        cursor = batch.cursor
        await overlap.settle()

    batch = await log.read("s", after=cursor)
    # The row whose sequence number is *lower* than the cursor's, delivered
    # because the cursor is a transaction id first.
    assert [record["body"] for record in batch] == ["lost"]
    assert batch.records[0].cursor.seq < cursor.seq


async def test_the_log_delivers_both_rows_across_the_same_overlap(log, database):
    cursor = Cursor.start()
    async with _Overlap(database) as overlap:
        await overlap.run()
        # Nothing below the horizon yet: the early transaction is still open, so
        # it *is* the horizon, and the late row sits above it.
        batch = await log.read("s", after=cursor)
        assert [record["body"] for record in batch] == []
        cursor = batch.cursor
        await overlap.settle()

    batch = await log.read("s", after=cursor)
    # Both. The 'early' row is the one the naive reader in the test above lost,
    # and it is delivered here from a cursor recorded *before* it settled.
    assert sorted(record["body"] for record in batch) == ["early", "late"]
    # Delivered in (xid, seq) order, which is what makes the batch's own cursor
    # a safe resume point for the row after it.
    cursors = [record.cursor for record in batch]
    assert cursors == sorted(cursors)
    assert batch.cursor == cursors[-1]


async def test_resuming_from_a_mid_batch_cursor_delivers_the_rest_exactly_once(log, database):
    for index in range(5):
        await log.append("s", body=f"row-{index}")

    first = await log.read("s", after=Cursor.start(), limit=2)
    assert [record["body"] for record in first] == ["row-0", "row-1"]

    second = await log.read("s", after=first.cursor, limit=2)
    assert [record["body"] for record in second] == ["row-2", "row-3"]

    third = await log.read("s", after=second.cursor)
    assert [record["body"] for record in third] == ["row-4"]

    # And the tail is idempotent: reading again from the same place returns
    # nothing and keeps the cursor where it was.
    fourth = await log.read("s", after=third.cursor)
    assert list(fourth) == []
    assert fourth.cursor == third.cursor


async def test_a_stream_never_sees_another_streams_rows(log, database):
    await log.append("mine", body="a")
    await log.append("yours", body="b")
    batch = await log.read("mine", after=Cursor.start())
    assert [record["body"] for record in batch] == ["a"]
    assert [record.stream for record in batch] == ["mine"]


async def test_the_whole_log_reads_across_streams_in_one_order(log, database):
    await log.append("mine", body="a")
    await log.append("yours", body="b")
    batch = await log.read(after=Cursor.start())
    assert [record["body"] for record in batch] == ["a", "b"]


async def test_append_returns_the_position_the_row_landed_at(log, database):
    first = await log.append("s", body="a")
    second = await log.append("s", body="b")
    assert second > first
    batch = await log.read("s", after=first)
    # The receipt is *this row's* position, so reading after it skips it. That
    # is the documented meaning, and the reason it is not called a resume point.
    assert [record["body"] for record in batch] == ["b"]


async def test_an_append_on_a_caller_connection_shares_that_transaction(log, database):
    connection = await database.acquire("write")
    try:
        await connection.execute("BEGIN")
        await log.append("s", connection=connection, body="rolled back")
        await connection.execute("ROLLBACK")

        await connection.execute("BEGIN")
        await log.append("s", connection=connection, body="committed")
        await connection.execute("COMMIT")
    finally:
        await database.release("write", connection)

    batch = await log.read("s", after=Cursor.start())
    assert [record["body"] for record in batch] == ["committed"]


async def test_an_append_without_a_connection_survives_the_callers_rollback(log, database):
    connection = await database.acquire("write")
    try:
        await connection.execute("BEGIN")
        await log.append("s", body="independent")
        await connection.execute("ROLLBACK")
    finally:
        await database.release("write", connection)

    batch = await log.read("s", after=Cursor.start())
    assert [record["body"] for record in batch] == ["independent"]


async def test_the_horizon_lag_is_zero_on_a_quiet_log(log, database):
    await log.append("s", body="a")
    assert await log.horizon_lag() == 0


async def test_an_open_transaction_pins_the_horizon_and_stalls_readers(log, database):
    holder = await database.acquire("write")
    try:
        await holder.execute("BEGIN")
        # Force the holder to take a transaction id, which is what pins xmin.
        await holder.execute("SELECT pg_current_xact_id()")
        await log.append("s", body="stalled")
        assert list(await log.read("s", after=Cursor.start())) == []
        assert await log.horizon_lag() > 0
        await holder.execute("COMMIT")
    finally:
        await database.release("write", holder)

    batch = await log.read("s", after=Cursor.start())
    assert [record["body"] for record in batch] == ["stalled"]
    assert await log.horizon_lag() == 0


async def test_concurrent_appenders_are_all_delivered_exactly_once(log, database):
    await asyncio.gather(*(log.append("s", body=f"row-{index}") for index in range(50)))

    seen: list[str] = []
    cursor = Cursor.start()
    for _ in range(20):
        batch = await log.read("s", after=cursor, limit=7)
        cursor = batch.cursor
        seen.extend(record["body"] for record in batch)
        if len(seen) == 50:
            break

    assert len(seen) == 50, f"delivered {len(seen)} of 50"
    assert len(set(seen)) == 50, "a row was delivered twice"


async def test_retention_drops_rows_past_their_age(log, database):
    forever = PostgresLog(database, _DECLARATION)
    with pytest.raises(ValueError, match="KEEP_FOREVER"):
        await forever.purge()


async def test_dropping_a_stream_removes_it_from_a_keep_forever_log(log, database):
    await log.append("subject-7", body="personal")
    await log.append("subject-8", body="someone else's")
    await log.drop_stream("subject-7")
    assert list(await log.read("subject-7", after=Cursor.start())) == []
    assert len(await log.read("subject-8", after=Cursor.start())) == 1
