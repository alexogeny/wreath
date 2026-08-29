from __future__ import annotations

import os

import pytest

from wreath._jobcore import dedup_key
from wreath.jobs import JobRunner
from wreath.postgres import Database
from wreath.recording import (
    AttemptOutcome,
    AttemptPolicy,
    AttemptRecorder,
    AttemptTrigger,
    AttemptTriggerKind,
    read_attempt_recording,
)
from wreath.replay import replay_attempt

pytestmark = pytest.mark.skipif(
    not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
    reason="set WREATH_TEST_POSTGRES_DSN to run live attempt-recording tests",
)

#: Per xdist worker, and *assigned* rather than defaulted: workers sharing one
#: schema race on `CREATE SCHEMA IF NOT EXISTS`, and PostgreSQL reports that
#: race as a `pg_namespace_nspname_index` unique violation, which reads like
#: anything except a test-isolation bug.
_SCHEMA = f"wreath_test_attempts_{os.environ.get('PYTEST_XDIST_WORKER', 'gw0')}"


async def _apply(db, sql):
    connection = await db.acquire("write")
    try:
        for statement in (s.strip() for s in sql.split(";\n")):
            if statement:
                await connection.execute(statement)
    finally:
        await db.release("write", connection)


@pytest.fixture
async def queue(tmp_path):
    dsn = os.environ["WREATH_TEST_POSTGRES_DSN"]
    db = Database("main", dsn, pools={"write": {"min_size": 1, "max_size": 4}})
    await db.start()
    recorder = AttemptRecorder(
        AttemptPolicy(triggers=(AttemptTrigger(AttemptTriggerKind.FAILURE),)),
        directory=str(tmp_path),
    )
    runner = JobRunner(db, name="work", schema=_SCHEMA, concurrency=1, lease=1.0, attempts=recorder)
    await _apply(db, f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"')
    await _apply(db, runner.schema_sql())
    await _apply(db, f'TRUNCATE "{_SCHEMA}".jobs')
    try:
        yield runner, tmp_path
    finally:
        await _apply(db, f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await db.stop()


async def _rows(runner):
    connection = await runner._db.acquire("write")
    try:
        return await connection.fetch(
            f'SELECT id, state, attempts, fence, dedup_key FROM "{_SCHEMA}".jobs ORDER BY id'
        )
    finally:
        await runner._db.release("write", connection)


async def test_a_reclaimed_lease_is_recorded_as_the_attempt_it_ended(queue):
    runner, directory = queue

    @runner.task("noop")
    async def noop(ctx, *args):
        pass

    job_id = await runner.enqueue("noop", "a", "b", key="once")
    claimed = (await runner._claim(1))[0]
    held_fence = claimed.fence

    await runner._exec(
        f"UPDATE \"{_SCHEMA}\".jobs SET lease_expiry = now() - interval '1 hour' WHERE id = $1",
        job_id,
    )
    await runner._reclaim_expired()

    written = sorted(p.name for p in directory.iterdir())
    assert written == [f"work-{job_id}-1.wfr1"]
    record = read_attempt_recording((directory / written[0]).read_bytes())
    assert record.outcome == AttemptOutcome.LEASE_EXPIRED
    assert record.job_id == job_id
    assert record.attempt == 1
    # The fence the *vanished* worker held, not the one the sweep just minted.
    # Without it a recording cannot say which of two claimants it describes.
    assert record.fence == held_fence
    assert record.dedup_key == dedup_key("work", "once")
    # The count came from `jsonb_array_length` in the projection; the values
    # never left the database.
    assert record.argument_count == 2
    assert b"\x01a" not in (directory / written[0]).read_bytes()
    # Nothing was watching a worker that is gone, so there is no trace to hold.
    assert record.boundaries == ()


async def test_a_reclaimed_lease_with_no_dedup_key_records_an_empty_one(queue):
    runner, directory = queue

    @runner.task("noop")
    async def noop(ctx, *args):
        pass

    job_id = await runner.enqueue("noop")
    await runner._claim(1)
    await runner._exec(
        f"UPDATE \"{_SCHEMA}\".jobs SET lease_expiry = now() - interval '1 hour' WHERE id = $1",
        job_id,
    )
    await runner._reclaim_expired()

    record = read_attempt_recording((directory / f"work-{job_id}-1.wfr1").read_bytes())
    assert record.dedup_key == ""
    assert record.argument_count == 0
    assert record.trace_context == ""


async def test_an_unarmed_sweep_keeps_the_statement_it_always_issued(queue):
    runner, _ = queue
    runner._attempts = None
    issued: list[str] = []
    original = runner._exec

    async def spy(sql, *args):
        issued.append(sql)
        return await original(sql, *args)

    runner._exec = spy
    await runner._reclaim_expired()
    assert len(issued) == 1
    assert "RETURNING" not in issued[0]


async def test_replaying_an_attempt_does_not_touch_the_queue(queue):
    runner, directory = queue

    @runner.task("noisy")
    async def noisy(ctx, *args):
        connection = await runner._db.acquire("write")
        try:
            await connection.fetch(f'SELECT id FROM "{_SCHEMA}".jobs')
        finally:
            await runner._db.release("write", connection)
        raise ValueError("and then it failed")

    job_id = await runner.enqueue("noisy", key="original")
    claimed = (await runner._claim(1))[0]
    await runner._run(claimed)

    before = await _rows(runner)
    # The recorded attempt: one row, failed once, back to ready for a retry.
    assert [(r["id"], r["state"], r["attempts"]) for r in before] == [(job_id, "ready", 1)]
    recording = directory / f"work-{job_id}-1.wfr1"
    record = read_attempt_recording(recording.read_bytes())
    assert record.outcome == AttemptOutcome.RAISED

    # The recording is now taken to a checkout where the same task does every
    # destructive thing a handler could do to the queue. That is the situation
    # this property exists for: a production recording, replayed by somebody
    # who did not write the handler, on a machine with a real database.
    replayer = JobRunner(runner._db, name="work", schema=_SCHEMA, concurrency=1, lease=1.0)

    @replayer.task("noisy")
    async def destructive(ctx, *args):
        await replayer.enqueue("noisy", key=f"spawned-{ctx.attempt}")
        connection = await replayer._db.acquire("write")
        try:
            await connection.execute(f'DELETE FROM "{_SCHEMA}".jobs')
        finally:
            await replayer._db.release("write", connection)
        raise ValueError("and then it failed")

    result = await replay_attempt(replayer, record)
    assert result.outcome == "raised"
    assert result.error_type == "ValueError"
    assert result.matched

    after = await _rows(runner)
    assert [tuple(r[i] for i in range(5)) for r in after] == [
        tuple(r[i] for i in range(5)) for r in before
    ], "the replay changed the queue"
    # And specifically: no new row from the handler's `enqueue`, and the
    # original's dedup key is untouched, so a later real enqueue still dedupes.
    assert len(after) == 1
    assert after[0]["dedup_key"] == dedup_key("work", "original")
    assert await runner.enqueue("noisy", key="original") is None


async def test_the_replay_reached_a_double_rather_than_doing_nothing(queue):
    runner, directory = queue
    seen: list[int] = []

    @runner.task("counted")
    async def counted(ctx, *args):
        seen.append(ctx.attempt)
        connection = await runner._db.acquire("write")
        await connection.execute("SELECT 1")
        await runner._db.release("write", connection)
        raise ValueError("boom")

    job_id = await runner.enqueue("counted")
    await runner._run((await runner._claim(1))[0])
    record = read_attempt_recording((directory / f"work-{job_id}-1.wfr1").read_bytes())
    assert seen == [1]
    # The live attempt's boundary trace saw the handler's acquire/query/release
    # and then the runner's own `_fail` UPDATE on the same database.
    assert len(record.boundaries) >= 3

    result = await replay_attempt(runner, record)
    assert seen == [1, 1]
    assert result.adapters.databases["main"].acquired >= 1
    assert not result.adapters.databases["main"].leaked
