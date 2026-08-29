from __future__ import annotations

import asyncio
import os

import pytest

from wreath.jobs import JobRunner
from wreath.log import Flush, PostgresLog
from wreath.postgres import Database
from wreath.streams import (
    KIND_CANCELLED,
    KIND_CHUNK,
    KIND_END,
    KIND_SUPERSEDED,
    Streams,
    declaration,
)

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
        reason="set WREATH_TEST_POSTGRES_DSN to run live stream tests",
    ),
]

#: One schema per xdist worker, by plain assignment -- never
#: `os.environ.setdefault`, which no-ops because the controller writes it during
#: collection and then spawns workers carrying its own value. See
#: `tests/_camera_trap.py`.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_streams_{_WORKER}"

#: A byte threshold no run here reaches and a time threshold short enough that a
#: producer which pauses flushes what it has, which is what makes the killed
#: worker leave durable rows behind rather than a buffer.
_FLUSH = Flush(bytes=1 << 20, every=0.01, capacity=256)

_LEASE = 1.0


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
    handle = Database("main", dsn, pools={"write": {"min_size": 1, "max_size": 8}})
    await handle.start()
    await _apply(handle, f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"')
    declared = declaration(schema=_SCHEMA, retain=60.0, flush=_FLUSH)
    runner = JobRunner(handle, name="streams", schema=_SCHEMA, concurrency=1, lease=_LEASE)
    await _apply(handle, *declared.statements())
    for statement in runner.schema_sql().split(";\n"):
        await _apply(handle, statement)
    await _apply(
        handle,
        f"TRUNCATE {declared.qualified_table}",
        f'TRUNCATE "{_SCHEMA}".jobs',
    )
    try:
        yield handle
    finally:
        await _apply(handle, f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await handle.stop()


@pytest.fixture
def parts(database):
    """A runner and a `Streams` over one live schema."""
    declared = declaration(schema=_SCHEMA, retain=60.0, flush=_FLUSH)
    runner = JobRunner(database, name="streams", schema=_SCHEMA, concurrency=1, lease=_LEASE)
    streams = Streams(jobs=runner, log=PostgresLog(database, declared), idle=0.4, poll=0.01)
    return runner, streams


def _render(events) -> str:
    """What a client that follows the protocol ends up with on screen.

    Append on `chunk`; **clear** on `superseded`, because that is the whole
    contract -- a client that concatenates a replaced range renders duplicated
    text and blames the model.
    """
    rendered = ""
    for event in events:
        if event.kind == KIND_SUPERSEDED:
            rendered = ""
        elif event.kind == KIND_CHUNK:
            rendered += event.data.decode("utf-8")
    return rendered


#: See `tests/test_streams_reader.py`: a reader that never stops hangs rather
#: than failing, so it is timed out here and reported as the defect it is.
_NEVER_STOPS = 10.0


async def _collect(streams, key, **options):
    events = []
    try:
        async with asyncio.timeout(_NEVER_STOPS):
            async for event in streams.follow(key, **options):
                events.append(event)
    except TimeoutError:
        raise AssertionError(
            f"follow({key!r}, {options}) was still going after {_NEVER_STOPS}s"
        ) from None
    return events


async def test_a_killed_worker_leaves_one_logical_stream_with_no_gap_and_no_duplicate(
    parts,
):
    runner, streams = parts
    attempts: list[int] = []

    @streams.producer("chat", retries=3, timeout=_LEASE * 0.8)
    async def produce(stream) -> None:
        attempts.append(stream.fence)
        for index in range(5):
            await stream.write(f"[{index}]")
        await stream.flush()
        if len(attempts) == 1:
            # The worker is about to be killed. A real SIGKILL takes the task
            # with it; cancelling the run is the same shape from the queue's
            # point of view, and leaves the row leased exactly as a death does.
            await asyncio.sleep(60)
        for index in range(5, 10):
            await stream.write(f"[{index}]")

    await streams.start("chat", key="conversation-1")
    first_claim = await runner._claim(1)
    assert len(first_claim) == 1
    running = asyncio.create_task(runner._run(first_claim[0]))
    await asyncio.sleep(0.25)

    # The client attached to the dying worker's connection reads what landed.
    partial = await _collect(streams, "conversation-1", idle=0.15, poll=0.01)
    chunks = [event for event in partial if event.kind == KIND_CHUNK]
    assert chunks, "the killed attempt must have flushed something durable"
    resume_from = chunks[-1].cursor

    running.cancel()
    await asyncio.gather(running, return_exceptions=True)

    # The lease expires and the sweeper hands the job back with a higher fence.
    await asyncio.sleep(_LEASE + 0.2)
    await runner._reclaim_expired()
    second_claim = await runner._claim(1)
    assert len(second_claim) == 1
    assert second_claim[0].fence > first_claim[0].fence, "the fence must have moved"
    await runner._run(second_claim[0])

    whole = "".join(f"[{index}]" for index in range(10))
    from_zero = await _collect(streams, "conversation-1")
    resumed = await _collect(streams, "conversation-1", since=resume_from)

    assert from_zero[-1].kind == KIND_END
    assert resumed[-1].kind == KIND_END
    assert _render(from_zero) == whole, "the fresh reader saw a gap or a duplicate"
    # The resuming client keeps what it already rendered up to `resume_from`,
    # then applies what follows -- which begins with the supersede.
    already = _render(chunks)
    assert _render_from(already, resumed) == whole
    assert any(event.kind == KIND_SUPERSEDED for event in resumed), (
        "the resuming client must be told its content was replaced, not left "
        "to discover it by rendering the same tokens twice"
    )
    # A fresh reader is never handed the replaced range at all.
    assert not any(event.kind == KIND_SUPERSEDED for event in from_zero)
    assert streams.superseded_rows > 0


def _render_from(already: str, events) -> str:
    """Continue a client's render from what it had before it reconnected."""
    rendered = already
    for event in events:
        if event.kind == KIND_SUPERSEDED:
            rendered = ""
        elif event.kind == KIND_CHUNK:
            rendered += event.data.decode("utf-8")
    return rendered


async def test_a_retry_after_a_fenced_attempt_produces_exactly_one_logical_stream(parts, database):
    runner, streams = parts
    seen: list[int] = []

    @streams.producer("chat", retries=3, timeout=_LEASE * 0.8)
    async def produce(stream) -> None:
        seen.append(stream.fence)
        await stream.write("partial")
        await stream.flush()
        if len(seen) == 1:
            raise RuntimeError("the model hung up")
        await stream.write("-complete")

    await streams.start("chat", key="k")
    first = await runner._claim(1)
    # The runner's own failure path: the attempt is charged and the row goes
    # back to `ready` behind a backoff, which is shortened here rather than
    # waited out.
    await runner._run(first[0])
    await _apply(database, f'UPDATE "{_SCHEMA}".jobs SET run_at = now()')
    second = await runner._claim(1)
    assert second[0].fence > first[0].fence, "a retry claims under a new fence"
    await runner._run(second[0])

    events = await _collect(streams, "k")
    assert _render(events) == "partial-complete"
    assert events[-1].kind == KIND_END
    assert len([event for event in events if event.kind == KIND_END]) == 1
    assert streams.superseded_rows == 1, "the first attempt's chunk was skipped"


async def test_two_attachers_one_from_zero_and_one_mid_stream_both_complete(parts):
    runner, streams = parts

    @streams.producer("chat", timeout=_LEASE * 0.8)
    async def produce(stream, count) -> None:
        for index in range(count):
            await stream.write(f"{index},")
            await stream.flush()
            await asyncio.sleep(0.01)

    await streams.start("chat", key="k", args=(12,))
    claimed = await runner._claim(1)
    running = asyncio.create_task(runner._run(claimed[0]))

    from_zero = asyncio.create_task(_collect(streams, "k", idle=1.5))
    await asyncio.sleep(0.08)
    head = await _collect(streams, "k", idle=0.05)
    mid_cursor = [event for event in head if event.kind == KIND_CHUNK][-1].cursor
    mid = asyncio.create_task(_collect(streams, "k", since=mid_cursor, idle=1.5))

    await running
    whole = "".join(f"{index}," for index in range(12))
    complete = await from_zero
    tail = await mid
    assert _render(complete) == whole
    assert complete[-1].kind == KIND_END
    assert tail[-1].kind == KIND_END
    assert _render_from(_render(head), tail) == whole


async def test_a_stream_whose_producer_never_ran_blocks_then_times_out(parts):
    _runner, streams = parts
    started = asyncio.get_running_loop().time()
    events = await _collect(streams, "nobody-started-this", idle=0.3, poll=0.02)
    elapsed = asyncio.get_running_loop().time() - started
    assert [event.kind for event in events] == ["timeout"]
    assert elapsed >= 0.3


async def test_a_cursor_from_another_stream_cannot_select_this_ones_chunks(parts):
    runner, streams = parts

    @streams.producer("chat", timeout=_LEASE * 0.8)
    async def produce(stream, text) -> None:
        await stream.write(text)

    for key, text in (("a", "alpha"), ("b", "beta")):
        await streams.start("chat", key=key, args=(text,))
        claimed = await runner._claim(1)
        await runner._run(claimed[0])

    events_a = await _collect(streams, "a")
    borrowed = events_a[0].cursor.encode()
    refused = streams.attach("b", since=borrowed)
    assert refused.status == 400
    assert b"belongs to a different stream" in refused.body
    # And b's own stream is intact, so the refusal is the only consequence.
    assert _render(await _collect(streams, "b")) == "beta"


async def test_cancelling_a_stream_returns_the_terminal_record_rather_than_hanging(parts):
    runner, streams = parts

    @streams.producer("chat", timeout=_LEASE * 0.8)
    async def produce(stream) -> None:  # pragma: no cover - never claimed
        await stream.write("never")

    handle = await streams.start("chat", key="k")
    assert await streams.cancel("k", reason="the user closed the tab") is True

    events = await _collect(streams, "k")
    assert [event.kind for event in events] == [KIND_CANCELLED]
    assert events[0].detail == "the user closed the tab"
    assert runner.cancelled == 1
    # The queue row is out of the way and fenced, so no worker will run it.
    assert await runner._claim(1) == []
    assert handle.task_id


async def test_cancel_by_key_and_by_id_are_the_same_door(parts):
    runner, _streams = parts

    @runner.task("noop")
    async def noop(context) -> None:  # pragma: no cover - never claimed
        pass

    first = await runner.enqueue("noop", key="one")
    assert first is not None
    assert await runner.cancel(first) is True
    assert await runner.cancel(first) is False, "a dead row is not cancellable twice"

    second = await runner.enqueue("noop", key="two")
    assert second is not None
    assert await runner.cancel(key="two") is True
    assert runner.cancelled == 2
    with pytest.raises(ValueError) as raised:
        await runner.cancel(second, key="two")
    assert "exactly one of job_id= and key=" in str(raised.value)


async def test_the_replay_is_a_range_scan_over_the_stream_index(parts):
    _runner, streams = parts
    log = streams.log
    for batch in range(8):
        await log.append_many(
            [
                (
                    f"stream-{index % 40}",
                    {
                        "fence": 1,
                        "idx": batch,
                        "kind": KIND_CHUNK,
                        "body": b"x" * 32,
                        "detail": "",
                    },
                )
                for index in range(512)
            ]
        )
    connection = await log.database.acquire("write")
    try:
        await connection.execute(f"ANALYZE {log.table}")
        rows = await connection.fetch(f"EXPLAIN {log.sql('read')}", "stream-7", "0", 0, 512)
    finally:
        await log.database.release("write", connection)
    plan = "\n".join(row[0] for row in rows)
    assert "stream_chunks_stream_idx" in plan, plan


async def test_a_flush_costs_one_statement_per_rung_not_one_per_chunk(parts):
    _runner, streams = parts
    writer_log = streams.log

    class _Counting:
        def __init__(self, inner) -> None:
            self._inner = inner
            self.executed: list[str] = []

        async def execute(self, sql, *args):
            self.executed.append(sql)
            return await self._inner.execute(sql, *args)

    connection = await writer_log.database.acquire("write")
    counting = _Counting(connection)
    try:
        written = await writer_log.append_many(
            [
                (
                    "k",
                    {"fence": 1, "idx": index, "kind": KIND_CHUNK, "body": b"token", "detail": ""},
                )
                for index in range(1000)
            ],
            connection=counting,
        )
    finally:
        await writer_log.database.release("write", connection)
    assert written == 1000
    # 1000 = 512 + 256 + 128 + 64 + 32 + 8.
    assert len(counting.executed) == 6, counting.executed


async def test_the_max_fence_seed_stops_at_the_horizon(parts):
    _runner, streams = parts
    log = streams.log
    await log.append("k", fence=1, idx=0, kind=KIND_CHUNK, body=b"settled", detail="")
    database = log.database
    holding = await database.acquire("write")
    try:
        async with holding.transaction():
            await holding.execute(
                f"INSERT INTO {log.table} (stream_key, fence, idx, kind, body, detail) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                "k",
                2,
                0,
                KIND_CHUNK,
                b"in-flight",
                "",
            )
            # The fence-2 row exists but has not settled, so neither the seed
            # nor the read may see it -- and the fence-1 row must still arrive.
            events = await _collect(streams, "k", idle=0.15, poll=0.01)
    finally:
        await database.release("write", holding)
    assert _render(events) == "settled"


async def test_a_stream_key_is_one_producer_however_many_times_start_is_called(parts):
    runner, streams = parts
    runs: list[int] = []

    @streams.producer("chat", timeout=_LEASE * 0.8)
    async def produce(stream) -> None:
        runs.append(1)
        await stream.write("once")

    first = await streams.start("chat", key="k")
    second = await streams.start("chat", key="k")
    assert first.task_id == second.task_id
    claimed = await runner._claim(2)
    assert len(claimed) == 1
    await runner._run(claimed[0])
    assert runs == [1]
    assert _render(await _collect(streams, "k")) == "once"


async def test_retention_is_a_counted_walk_and_the_declaration_carries_it(parts):
    _runner, streams = parts
    walk = streams.retention_pass(name=f"stream_chunks_{_WORKER}", schema=_SCHEMA)
    assert walk.recurring
    assert streams.log.declaration.retain == 60.0
