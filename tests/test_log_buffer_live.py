from __future__ import annotations

import asyncio
import os
import time

import pytest

from wreath.log import (
    DEFAULT_LIMIT,
    KEEP_FOREVER,
    Column,
    Cursor,
    Flush,
    Log,
    PostgresLog,
    retention_pass,
)
from wreath.postgres import Database

pytestmark = [
    pytest.mark.asyncio,
    # `network` rather than `database`, against `pyproject.toml`'s general rule:
    # every read here stops at a cluster-wide horizon that any other worker's
    # open transaction pins. See `tests/test_log_cursor_live.py` for the
    # measurement and the reasoning.
    pytest.mark.network,
    pytest.mark.skipif(
        not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
        reason="set WREATH_TEST_POSTGRES_DSN to run live log buffer tests",
    ),
]

#: One schema per xdist worker, by plain assignment -- never
#: `os.environ.setdefault`, which no-ops because the controller writes it during
#: collection and then spawns workers carrying its own value. See
#: `tests/_camera_trap.py`.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_logbuf_{_WORKER}"

#: A byte threshold no single row reaches and a time threshold long enough that
#: nothing crosses it by accident, so each test picks the trigger it is about.
_CHUNKS = Log(
    table="chunks",
    retain=KEEP_FOREVER,
    columns=(Column("body", "text", null=False),),
    schema=_SCHEMA,
    flush=Flush(bytes=64, every=10.0, capacity=8),
)

#: The retention half needs its own table, because `retain=` is what decides
#: whether the age index and the purge statement exist at all.
_EVENTS = Log(
    table="events",
    retain=30.0,
    columns=(Column("body", "text", null=False),),
    schema=_SCHEMA,
)


async def _apply(database, *statements: str) -> None:
    connection = await database.acquire("write")
    try:
        for statement in statements:
            if statement.strip():
                await connection.execute(statement)
    finally:
        await database.release("write", connection)


@pytest.fixture
async def database():
    dsn = os.environ["WREATH_TEST_POSTGRES_DSN"]
    handle = Database("main", dsn, pools={"write": {"min_size": 1, "max_size": 6}})
    await handle.start()
    await _apply(handle, f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"')
    await _apply(handle, *_CHUNKS.statements(), *_EVENTS.statements())
    await _apply(
        handle,
        f"TRUNCATE {_CHUNKS.qualified_table}",
        f"TRUNCATE {_EVENTS.qualified_table}",
    )
    try:
        yield handle
    finally:
        await _apply(handle, f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await handle.stop()


@pytest.fixture
def log(database):
    return PostgresLog(database, _CHUNKS)


@pytest.fixture
def events(database):
    return PostgresLog(database, _EVENTS)


async def _bodies(log_, stream="s"):
    return [record["body"] for record in await log_.read(stream, after=Cursor.start())]


class _Counting:
    """A real connection with a tally of the statements run through it.

    The statement count *is* the defect this work fixes, so it has to be an
    assertion rather than a description. Wrapping the caller-connection path is
    how the number becomes visible without `pg_stat_statements`, which is not
    loaded on a stock server and would count the fixture's own DDL besides.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.executed: list[str] = []

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return await self._inner.execute(sql, *args)

    async def fetchrow(self, sql, *args):
        self.executed.append(sql)
        return await self._inner.fetchrow(sql, *args)


async def test_a_thousand_rows_cost_a_handful_of_statements_not_a_thousand(log, database):
    connection = await database.acquire("write")
    counting = _Counting(connection)
    try:
        written = await log.append_many(
            [("s", {"body": f"row-{index:04d}"}) for index in range(1000)],
            connection=counting,
        )
    finally:
        await database.release("write", connection)

    assert written == 1000
    assert len(counting.executed) == 6, counting.executed
    # And the same rows through the shipped one-at-a-time path would have been
    # one statement each, which is the comparison the number is against.
    connection = await database.acquire("write")
    single = _Counting(connection)
    try:
        for index in range(10):
            await log.append("single", connection=single, body=f"row-{index}")
    finally:
        await database.release("write", connection)
    assert len(single.executed) == 10


async def test_a_batched_append_lands_every_row_in_offer_order(log):
    written = await log.append_many([("s", {"body": f"row-{index:03d}"}) for index in range(333)])
    assert written == 333
    assert await _bodies(log) == [f"row-{index:03d}" for index in range(333)]


async def test_a_batch_larger_than_the_top_rung_is_split_and_still_ordered(log):
    written = await log.append_many([("s", {"body": f"row-{index:04d}"}) for index in range(1300)])
    assert written == 1300
    bodies = []
    cursor = Cursor.start()
    while True:
        batch = await log.read("s", after=cursor)
        if not batch:
            break
        cursor = batch.cursor
        bodies.extend(record["body"] for record in batch)
    assert bodies == [f"row-{index:04d}" for index in range(1300)]


async def test_a_batch_may_name_a_different_stream_per_row(log):
    await log.append_many(
        [("photos:1", {"body": "a"}), ("photos:2", {"body": "b"}), ("photos:1", {"body": "c"})]
    )
    assert await _bodies(log, "photos:1") == ["a", "c"]
    assert await _bodies(log, "photos:2") == ["b"]


async def test_an_empty_batch_writes_nothing_and_says_so(log):
    assert await log.append_many([]) == 0
    assert await _bodies(log) == []


async def test_a_batch_on_a_caller_connection_shares_that_transaction(log, database):
    connection = await database.acquire("write")
    try:
        await connection.execute("BEGIN")
        await log.append_many(
            [("s", {"body": f"rolled-{index}"}) for index in range(20)],
            connection=connection,
        )
        await connection.execute("ROLLBACK")

        await connection.execute("BEGIN")
        await log.append_many(
            [("s", {"body": f"kept-{index}"}) for index in range(20)],
            connection=connection,
        )
        await connection.execute("COMMIT")
    finally:
        await database.release("write", connection)

    assert await _bodies(log) == [f"kept-{index}" for index in range(20)]


async def test_a_batch_with_a_payload_the_declaration_refuses_writes_nothing(log):
    rows = [("s", {"body": "fine"}) for _ in range(10)]
    rows.append(("s", {"body": "fine", "boyd": "typo"}))
    with pytest.raises(ValueError, match="declares no column named boyd"):
        await log.append_many(rows)
    assert await _bodies(log) == []


async def test_the_buffer_comes_due_on_the_byte_threshold(log):
    buffer = log.buffered("s")
    buffer.offer(body="x" * 32)
    assert buffer.due is False
    buffer.offer(body="y" * 32)
    # 64 bytes, which is exactly the declared threshold: `>=`, not `>`.
    assert buffer.due is True
    assert await buffer.flush() == 2
    assert buffer.due is False


async def test_the_buffer_comes_due_on_the_time_threshold(database):
    declaration = Log(
        table="chunks",
        retain=KEEP_FOREVER,
        columns=(Column("body", "text", null=False),),
        schema=_SCHEMA,
        flush=Flush(bytes=1_000_000, every=0.05),
    )
    log = PostgresLog(database, declaration)
    buffer = log.buffered("slow")
    buffer.offer(body="one small token")
    assert buffer.due is False, "one token is nowhere near a megabyte"
    await asyncio.sleep(0.06)
    assert buffer.due is True
    assert await buffer.flush() == 1
    assert await _bodies(log, "slow") == ["one small token"]


async def test_whichever_threshold_fires_first_is_the_one_that_flushes(database):
    declaration = Log(
        table="chunks",
        retain=KEEP_FOREVER,
        columns=(Column("body", "text", null=False),),
        schema=_SCHEMA,
        flush=Flush(bytes=64, every=30.0),
    )
    log = PostgresLog(database, declaration)
    buffer = log.buffered("fast")
    started = time.monotonic()
    for _index in range(8):
        buffer.offer(body="x" * 16)
        if buffer.due:
            break
    elapsed = time.monotonic() - started
    # Nowhere near the 30-second time threshold, so the byte one is what fired.
    assert elapsed < 1.0
    assert buffer.due is True
    assert await buffer.flush() == 4


async def test_an_empty_buffer_is_never_due_and_flushing_it_writes_nothing(log):
    buffer = log.buffered("s")
    assert buffer.due is False
    assert await buffer.flush() == 0
    assert await _bodies(log) == []


async def test_an_empty_buffer_is_not_due_however_long_it_has_been_idle(database):
    declaration = Log(
        table="chunks",
        retain=KEEP_FOREVER,
        columns=(Column("body", "text", null=False),),
        schema=_SCHEMA,
        flush=Flush(bytes=1_000_000, every=0.05),
    )
    log = PostgresLog(database, declaration)
    buffer = log.buffered("idle")
    await asyncio.sleep(0.06)
    assert buffer.due is False
    buffer.offer(body="now there is something to write")
    assert buffer.due is True


async def test_a_window_lost_to_a_worker_death_is_counted_not_absorbed(log):
    buffer = log.buffered("s")
    for index in range(5):
        buffer.offer(body=f"in flight {index}")
    assert buffer.pending == 5
    assert log.dropped == 0

    lost = buffer.abandon()

    assert lost == 5
    assert log.dropped == 5
    assert buffer.pending == 0
    assert await _bodies(log) == [], "an abandoned window must not have been written"


async def test_offering_past_the_capacity_drops_and_counts_the_drop(log):
    buffer = log.buffered("s")
    accepted = [buffer.offer(body=f"row-{index}") for index in range(12)]
    # Capacity is 8; the last four were refused rather than silently displacing
    # the queued ones.
    assert accepted.count(True) == 8
    assert accepted.count(False) == 4
    assert log.dropped == 4
    assert await buffer.flush() == 8


async def test_a_failed_flush_counts_the_whole_batch_and_re_raises(log, database):
    buffer = log.buffered("s")
    for index in range(3):
        buffer.offer(body=f"row-{index}")
    buffer.offer(body="fine", boyd="a typo the declaration does not know")

    with pytest.raises(ValueError, match="declares no column named boyd"):
        await buffer.flush()

    assert log.dropped == 4
    assert buffer.pending == 0, "a failed flush must not requeue behind the failure"
    assert await _bodies(log) == []


async def test_a_flush_the_server_refuses_is_counted_too(log):
    from wreath.postgres import PostgresError

    buffer = log.buffered("s")
    for index in range(3):
        buffer.offer(body=f"row-{index}")
    buffer.offer(body=None)

    with pytest.raises(PostgresError):
        await buffer.flush()

    assert log.dropped == 4
    assert await _bodies(log) == []


async def test_a_buffer_that_wrote_nothing_leaves_the_log_empty(log):
    buffer = log.buffered("s")
    for index in range(4):
        buffer.offer(body=f"never written {index}")
    assert await _bodies(log) == []


async def test_ordering_is_preserved_across_flushes_under_the_cursor(log):
    buffer = log.buffered("ordered")
    expected = []
    for flush in range(16):
        for index in range(5):
            body = f"{flush:02d}-{index}"
            expected.append(body)
            buffer.offer(body=body)
        assert await buffer.flush() == 5

    seen = []
    cursor = Cursor.start()
    for _ in range(30):
        batch = await log.read("ordered", after=cursor, limit=7)
        if not batch:
            break
        cursor = batch.cursor
        seen.extend(record["body"] for record in batch)
        cursors = [record.cursor for record in batch]
        assert cursors == sorted(cursors), "a page came back out of cursor order"

    assert seen == expected


async def test_two_buffers_on_one_stream_interleave_without_losing_a_row(log):
    left = log.buffered("shared")
    right = log.buffered("shared")
    for index in range(20):
        (left if index % 2 == 0 else right).offer(body=f"row-{index:02d}")
        if index % 5 == 4:
            await asyncio.gather(left.flush(), right.flush())
    await asyncio.gather(left.flush(), right.flush())

    seen = sorted(await _bodies(log, "shared"))
    assert seen == [f"row-{index:02d}" for index in range(20)]


async def test_a_read_with_no_limit_stops_at_the_default(log):
    assert DEFAULT_LIMIT == 512
    await log.append_many([("bounded", {"body": f"row-{i:04d}"}) for i in range(513)])

    first = await log.read("bounded", after=Cursor.start())
    assert len(first) == 512
    rest = await log.read("bounded", after=first.cursor)
    assert len(rest) == 1
    assert rest.records[0]["body"] == "row-0512"


@pytest.fixture
async def ledger(database):
    """The pass ledger, which a retention walk records its position in."""
    pass_ = retention_pass(_EVENTS, name="probe", schema=_SCHEMA)
    for statement in pass_.schema_sql().split(";\n"):
        if statement.strip():
            await _apply(database, statement)
    return None


async def _age(database, seconds: float) -> None:
    """Push every row's `at` back, so the retention frontier has passed it."""
    await _apply(
        database,
        f"UPDATE {_EVENTS.qualified_table} SET at = at - make_interval("
        f"secs => {float(seconds)!r}::float8)",
    )


async def test_the_retention_walk_deletes_aged_rows_and_counts_them(events, database, ledger):
    await events.append_many([("s", {"body": f"old-{index}"}) for index in range(40)])
    await _age(database, 60.0)
    await events.append_many([("s", {"body": f"new-{index}"}) for index in range(10)])

    walk = retention_pass(_EVENTS, name=f"retain_{_WORKER}", chunk=8, schema=_SCHEMA)
    result = await walk.run(database)

    assert result.rows == 40, "the deletions are counted, not merely performed"
    assert result.chunks >= 5, "paced into chunks rather than one unbounded DELETE"
    assert await _bodies(events) == [f"new-{index}" for index in range(10)]

    status = await walk.status(database)
    assert status is not None
    assert status.rows_done == 40


async def test_the_retention_walk_is_resumable_and_leaves_nothing_behind(events, database, ledger):
    await events.append_many([("s", {"body": f"first-{index}"}) for index in range(20)])
    await _age(database, 60.0)

    walk = retention_pass(_EVENTS, name=f"resume_{_WORKER}", chunk=5, schema=_SCHEMA)
    first = await walk.run(database)
    assert first.rows == 20
    assert await _bodies(events) == []

    await events.append_many([("s", {"body": f"second-{index}"}) for index in range(7)])
    await _age(database, 60.0)
    second = await walk.run(database)
    assert second.rows == 7
    assert await _bodies(events) == []


async def test_the_retention_walk_leaves_a_row_inside_the_window_alone(events, database, ledger):
    await events.append_many([("s", {"body": "young"})])
    walk = retention_pass(_EVENTS, name=f"young_{_WORKER}", chunk=5, schema=_SCHEMA)
    result = await walk.run(database)
    assert result.rows == 0
    assert await _bodies(events) == ["young"]


async def test_the_retention_walk_deletes_rows_of_every_stream(events, database, ledger):
    await events.append_many([("subject-7", {"body": "a"}), ("subject-8", {"body": "b"})])
    await _age(database, 60.0)
    walk = retention_pass(_EVENTS, name=f"streams_{_WORKER}", chunk=5, schema=_SCHEMA)
    assert (await walk.run(database)).rows == 2
    assert await _bodies(events, "subject-7") == []
    assert await _bodies(events, "subject-8") == []
