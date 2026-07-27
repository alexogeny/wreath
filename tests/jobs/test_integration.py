"""Live-PostgreSQL integration tests for the durable jobs runner.

Skipped unless ``WREATH_TEST_POSTGRES_DSN`` points at a throwaway database. These
exercise the real claim/complete/retry/fencing SQL against Postgres; the fake-DB
unit tests cover the pure paths.
"""

from __future__ import annotations

import os

import pytest

from wreath.jobs import JobRunner
from wreath.postgres import Database

pytestmark = pytest.mark.skipif(
    not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
    reason="set WREATH_TEST_POSTGRES_DSN to run live jobs integration tests",
)

_SCHEMA = "wreath_test_jobs"


async def _apply(db, sql):
    connection = await db.acquire("write")
    try:
        for statement in (s.strip() for s in sql.split(";\n")):
            if statement:
                await connection.execute(statement)
    finally:
        await db.release("write", connection)


@pytest.fixture
async def runner():
    dsn = os.environ["WREATH_TEST_POSTGRES_DSN"]
    db = Database("main", dsn, pools={"write": {"min_size": 1, "max_size": 4}})
    await db.start()
    r = JobRunner(db, name="work", schema=_SCHEMA, concurrency=1, lease=1.0)
    await _apply(db, r.schema_sql())
    await _apply(db, f'TRUNCATE "{_SCHEMA}".jobs')
    try:
        yield r
    finally:
        await _apply(db, f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await db.stop()


async def test_enqueue_claim_complete(runner):
    @runner.task("noop")
    async def noop(ctx):
        pass

    job_id = await runner.enqueue("noop", "arg1")
    assert job_id is not None

    claimed = await runner._claim(1)
    assert len(claimed) == 1
    job = claimed[0]
    assert job.task == "noop" and job.args == ["arg1"]

    await runner._complete(job)
    # A second claim finds nothing: the job is done.
    assert await runner._claim(1) == []


async def test_idempotent_enqueue_key(runner):
    @runner.task("noop")
    async def noop(ctx):
        pass

    first = await runner.enqueue("noop", key="once")
    second = await runner.enqueue("noop", key="once")
    assert first is not None
    assert second is None  # deduplicated by the unique index


async def test_skip_locked_hands_each_job_once(runner):
    @runner.task("noop")
    async def noop(ctx):
        pass

    await runner.enqueue("noop", "a")
    await runner.enqueue("noop", "b")
    first = await runner._claim(2)
    # Both leased in one batch; a concurrent claimer would skip them.
    assert {tuple(j.args) for j in first} == {("a",), ("b",)}
    assert await runner._claim(2) == []


async def test_fencing_blocks_stale_completion(runner):
    @runner.task("noop")
    async def noop(ctx):
        pass

    await runner.enqueue("noop")
    job = (await runner._claim(1))[0]
    # Simulate a lease-expiry reclaim by the sweeper: back to 'ready' with a
    # bumped fence, out from under the worker still holding the old fence.
    await runner._exec(
        f"UPDATE \"{_SCHEMA}\".jobs SET state='ready', fence = fence + 1, "
        "owner=NULL, lease_expiry=NULL WHERE id = $1",
        job.id,
    )
    # The stale worker's completion (WHERE fence = old) must not land.
    await runner._complete(job)
    still_there = await runner._claim(1)
    assert len(still_there) == 1  # re-claimable, not marked done by the stale worker


async def test_the_sweeper_reclaims_only_its_own_queue(runner):
    """Every queue in a schema shares one `jobs` table, partitioned by `queue`.

    The sweep was not partitioned with it. A queue whose own workers are down
    keeps its in-flight rows `leased` until it comes back -- unless some other
    queue in the same schema sweeps them, bumping `attempts` on its own lease
    interval until they exhaust `max_attempts` and dead-letter. Jobs destroyed
    by the deploy of a service that does not own them, and neither queue's
    counters record it.
    """
    other = JobRunner(
        runner._db, name="other", schema=_SCHEMA, concurrency=1, lease=1.0
    )

    @runner.task("noop")
    async def noop(ctx):
        pass

    @other.task("noop")
    async def other_noop(ctx):
        pass

    await other.enqueue("noop")
    claimed = await other._claim(1)
    assert len(claimed) == 1

    # Expire the other queue's lease, then sweep from *this* queue.
    connection = await runner._db.acquire("write")
    try:
        await connection.execute(
            f'UPDATE "{_SCHEMA}".jobs SET lease_expiry = now() - interval \'1 hour\''
            " WHERE queue = $1",
            "other",
        )
        await runner._reclaim_expired()
        state, attempts = await connection.fetchrow(
            f'SELECT state, attempts FROM "{_SCHEMA}".jobs WHERE queue = $1',
            "other",
        )
        assert (state, attempts) == ("leased", 0), (
            "the 'work' queue's sweeper touched the 'other' queue's row"
        )

        # And the owning queue still reclaims it, so the scoping did not
        # simply disable the sweep.
        await other._reclaim_expired()
        state, attempts = await connection.fetchrow(
            f'SELECT state, attempts FROM "{_SCHEMA}".jobs WHERE queue = $1',
            "other",
        )
        assert (state, attempts) == ("ready", 1)
    finally:
        await runner._db.release("write", connection)
