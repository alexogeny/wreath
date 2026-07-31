"""Trace context across the queue seam (plan 01, stage 2).

A request that enqueues durable work is the cause of that work, and until now
nothing recorded the link: a job that failed at 03:00 named no request. These
tests pin the whole path -- the schema step that makes room for the context, the
enqueue that writes it, the runner that restores it, and the two degradation
directions a fleet mid-rollout actually meets.

The database tests are gated on ``WREATH_TEST_POSTGRES_DSN``. The schema name is
derived per xdist worker by plain assignment: workers sharing one schema race on
``CREATE SCHEMA IF NOT EXISTS``, and ``os.environ.setdefault`` in a conftest
silently no-ops because the controller spawns workers with its own environment.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from wreath import telemetry
from wreath.jobs import JobRunner
from wreath.postgres import Database

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
requires_db = pytest.mark.skipif(
    not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)"
)

_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")


def _schema(suffix: str) -> str:
    return f"wtq_{_WORKER}_{suffix}"


_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


# --- the declaration, no database needed -------------------------------------


def test_the_jobs_component_targets_version_two_with_a_trace_column() -> None:
    """The context needs a column, and the column arrives as an additive step.

    Version 1 is left exactly as it was: rewriting it would change what an
    already-bootstrapped database was told it had, and the marker would then
    overstate history. Appending is the only honest direction.
    """
    runner = JobRunner(_FakeDatabase(), name="work", schema="wtq_decl")
    component = runner.component()

    assert component.target_version == 2
    versions = [step.version for step in component.steps]
    assert versions == [1, 2]

    first = " ".join(component.steps[0].statements)
    assert "trace_context" not in first, "version 1 must stay as it shipped"

    second = " ".join(component.steps[1].statements)
    assert "trace_context" in second
    assert "IF NOT EXISTS" in second, (
        "a step re-applies after a crash between its DDL and its marker, so "
        "every statement in it has to be idempotent"
    )


def test_every_notify_in_the_queue_carries_an_empty_payload() -> None:
    """The context rides the row, never the NOTIFY.

    `pg_notify` is a doorbell, not a transport: PostgreSQL caps the payload at
    8000 bytes and `_jobcore.check_notify_payload` bounds it for that reason.

    Asserted over the whole module rather than over one function, so moving the
    call -- which this change did, into `_insert` -- cannot quietly retire the
    guard. The first version of this test named `enqueue` and went green-to-red
    on a pure refactor, which is the wrong sensitivity: the property belongs to
    the queue, not to a function.
    """
    import inspect

    import wreath.jobs

    source = inspect.getsource(wreath.jobs)
    total = source.count("pg_notify")
    empty = source.count("pg_notify($1, '')")
    assert total > 0, "the doorbell has to ring somehow"
    assert total == empty, (
        f"{total - empty} pg_notify call(s) carry a payload; the channel is a "
        "doorbell and everything the job needs belongs on the row"
    )


class _FakeDatabase:
    """Enough of a Database for construction-time assertions."""

    def lock(self, *args, **kwargs):  # pragma: no cover - never awaited here
        raise NotImplementedError


async def test_claiming_never_asks_the_catalog_about_the_schema() -> None:
    """The worker loop must not pay for a schema question every poll.

    The shape of the table is established once -- by `start()` on a worker, by
    the first `enqueue` on a producer -- and read from the cache here. An
    earlier draft probed inside `_claim`, which put a `pg_attribute` lookup on
    the hot path and broke a robustness test whose database double implements
    only what the claim path is supposed to use. That double was right and the
    draft was wrong: a test that fails because you added an unnecessary query is
    the test doing its job.
    """
    issued: list[str] = []

    class _Connection:
        async def fetch(self, sql, *args):
            issued.append(sql)
            return []

    class _Database:
        async def acquire(self, workload):
            return _Connection()

        async def release(self, workload, connection):
            pass

    runner = JobRunner(_Database(), name="work", schema="wtq_hot")
    assert await runner._claim(1) == []

    assert len(issued) == 1, f"the claim issued {len(issued)} statements: {issued}"
    assert "FOR UPDATE SKIP LOCKED" in issued[0]
    assert "pg_attribute" not in issued[0]
    assert "trace_context" not in issued[0], (
        "an unstarted runner has no cached answer, so it must not select a "
        "column it cannot know is there"
    )


# --- against a live database -------------------------------------------------


async def _database(schema: str) -> Database:
    db = Database("main", _DSN, pools={"write": {"min_size": 1, "max_size": 4}})
    await db.start()
    await _exec(db, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    return db


async def _exec(db: Database, sql: str, *params) -> None:
    connection = await db.acquire("write")
    try:
        await connection.execute(sql, *params)
    finally:
        await db.release("write", connection)


async def _fetchval(db: Database, sql: str, *params):
    connection = await db.acquire("write")
    try:
        return await connection.fetchval(sql, *params)
    finally:
        await db.release("write", connection)


async def _drop(db: Database, schema: str) -> None:
    await _exec(db, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    await db.stop()


async def _bootstrap(db: Database, runner: JobRunner, schema: str, *, upto: int) -> None:
    """Apply the component's steps, stopping after `upto`.

    Stopping short is how the "new build meets an older schema" case is built:
    a deployment whose DBA has applied version 1 by hand and not yet version 2.
    """
    from wreath.schema import Component, bootstrap

    full = runner.component()
    partial = Component(
        name=full.name,
        schema=full.schema,
        relations=full.relations,
        steps=tuple(s for s in full.steps if s.version <= upto),
    )
    await bootstrap(db, [partial], schema=schema)


async def test_a_database_down_at_boot_does_not_stop_the_runner_starting() -> None:
    """The startup probe degrades and counts; it never refuses to start.

    A database down at boot once left a job runner with no doorbell for its
    entire process life, and that is why `Doorbell.open` reports failure rather
    than raising. A schema probe added to the same path has to hold the same
    line -- an *observability* column is the last thing that should be able to
    stop a queue coming up. The counter is what keeps the degradation visible
    rather than silent.
    """

    class _DownDatabase:
        async def acquire(self, workload):
            raise ConnectionError("database is down")

        async def release(self, workload, connection):  # pragma: no cover
            pass

    class _Supervisor:
        def __init__(self) -> None:
            self.stopping = asyncio.Event()
            self.spawned: list[str] = []

        def spawn(self, name, coro):
            self.spawned.append(name)
            coro.close()

    runner = JobRunner(_DownDatabase(), name="work", schema="wtq_down", concurrency=1)
    supervisor = _Supervisor()

    await runner.start(supervisor)

    assert runner.trace_probe_errors == 1
    assert runner._trace_column is None, "unresolved, so a later enqueue retries"
    assert any("worker" in name for name in supervisor.spawned), (
        "the runner must still come up: the probe is not a precondition of work"
    )


@requires_db
async def test_a_new_build_against_a_version_one_schema_still_enqueues_and_runs() -> None:
    """The degradation that matters: no column, no context, no failure.

    A DBA who cannot grant CREATE SCHEMA applies the DDL by hand, and there is
    always a window where the code is newer than what they have applied. The
    queue is the wrong place to discover that -- a job that refuses to enqueue
    because a *telemetry* column is missing has turned an observability feature
    into an outage.
    """
    schema = _schema("v1only")
    db = await _database(schema)
    try:
        runner = JobRunner(db, name="work", schema=schema, concurrency=1, lease=1.0)

        @runner.task("noop")
        async def noop(ctx):
            pass

        await _bootstrap(db, runner, schema, upto=1)

        token = telemetry.outbound_context.set((_TRACEPARENT, ""))
        try:
            job_id = await runner.enqueue("noop", "arg")
        finally:
            telemetry.outbound_context.reset(token)

        assert job_id is not None
        claimed = await runner._claim(1)
        assert len(claimed) == 1
        assert claimed[0].trace_context is None
        await runner._complete(claimed[0])
    finally:
        await _drop(db, schema)


@requires_db
async def test_an_older_build_runs_against_the_upgraded_jobs_schema() -> None:
    """The rolling-deploy direction, on the real component rather than a fake.

    `test_wreath_schema.py` proves the machinery with a synthetic component.
    This proves the actual `jobs` component -- the first in the tree to reach
    version 2 -- behaves the same way.
    """
    from wreath.schema import Component, bootstrap

    schema = _schema("rollfwd")
    db = await _database(schema)
    try:
        runner = JobRunner(db, name="work", schema=schema, concurrency=1, lease=1.0)
        full = runner.component()
        assert await bootstrap(db, [full], schema=schema) == {"jobs": 2}

        older = Component(
            name=full.name, schema=full.schema, relations=full.relations,
            steps=(full.steps[0],),
        )
        assert await bootstrap(db, [older], schema=schema) == {"jobs": 2}
    finally:
        await _drop(db, schema)


@requires_db
async def test_enqueue_persists_the_context_and_the_runner_restores_it() -> None:
    """The whole point: the job knows which request caused it.

    Asserted on both sides of the seam -- written on the row, and rebound while
    the handler runs so a call the *job* makes is a child of the same trace.
    """
    schema = _schema("carry")
    db = await _database(schema)
    try:
        runner = JobRunner(db, name="work", schema=schema, concurrency=1, lease=1.0)
        seen: dict[str, object] = {}

        @runner.task("look")
        async def look(ctx):
            seen["context"] = ctx.trace_context
            seen["bound"] = telemetry.outbound_context.get()

        await _bootstrap(db, runner, schema, upto=2)

        token = telemetry.outbound_context.set((_TRACEPARENT, "vendor=x"))
        try:
            job_id = await runner.enqueue("look")
        finally:
            telemetry.outbound_context.reset(token)

        stored = await _fetchval(
            db, f'SELECT trace_context FROM "{schema}".jobs WHERE id = $1', job_id
        )
        assert stored == _TRACEPARENT

        claimed = await runner._claim(1)
        assert claimed[0].trace_context == _TRACEPARENT
        await runner._run(claimed[0])

        assert seen["context"] == _TRACEPARENT
        assert seen["bound"] == (_TRACEPARENT, "")
    finally:
        await _drop(db, schema)


@requires_db
async def test_the_doorbell_delivers_an_empty_payload() -> None:
    """The source guard above, proved against a live server.

    Source inspection says nobody *wrote* a payload; this says none arrives.
    Together they cover a smuggling change made in either place.
    """
    schema = _schema("doorbell")
    db = await _database(schema)
    try:
        runner = JobRunner(db, name="work", schema=schema, concurrency=1, lease=1.0)

        @runner.task("noop")
        async def noop(ctx):
            pass

        await _bootstrap(db, runner, schema, upto=2)

        listener = await db.acquire("write")
        try:
            # `connection.listen()`, not `execute("LISTEN ...")`. The raw
            # statement registers with PostgreSQL but not with the driver, and
            # the notifications are then never pumped into the iterator -- a
            # diagnostic script proved a bare `execute` delivers nothing at all.
            # This is the same reason `Doorbell` holds its own connection.
            await listener.listen(runner._channel)

            payloads: list[str] = []

            async def watch() -> None:
                # `notifications()` has no timeout of its own and *returns*
                # rather than raising when the connection closes, so the bound
                # is the caller's.
                async for note in listener.notifications():
                    payloads.append(note.payload)
                    return

            # Started *before* the enqueue, and deliberately: the driver pumps
            # its socket while an operation is awaiting, so a connection that is
            # merely idle-and-acquired never reads the NOTIFY at all. Enqueuing
            # first and iterating afterwards times out against a server that
            # behaved perfectly, which is a test bug wearing the costume of a
            # product one.
            watcher = asyncio.ensure_future(watch())
            await asyncio.sleep(0)

            token = telemetry.outbound_context.set((_TRACEPARENT, ""))
            try:
                await runner.enqueue("noop")
            finally:
                telemetry.outbound_context.reset(token)

            try:
                async with asyncio.timeout(5.0):
                    await watcher
            finally:
                watcher.cancel()
        finally:
            await db.release("write", listener)

        assert payloads == [""], (
            f"the doorbell delivered {payloads!r}; a traced enqueue must still "
            "ring an empty bell"
        )
    finally:
        await _drop(db, schema)


@requires_db
async def test_an_untraced_job_binds_none_not_a_hollow_pair() -> None:
    """A job with no cause must bind `None`, never `(None, "")`.

    Found by a surviving mutant, and it is a crash rather than an untidiness.
    `HTTPClient._propagated` treats *any* non-None binding as a context to send
    and goes straight to `parent.encode("ascii")`; a hollow pair therefore
    raises `AttributeError` on the first outbound call an untraced job makes.
    The conditional in `_run` is load-bearing and nothing was asserting it.
    """
    schema = _schema("hollow")
    db = await _database(schema)
    try:
        runner = JobRunner(db, name="work", schema=schema, concurrency=1, lease=1.0)
        seen: dict[str, object] = {}

        @runner.task("peek")
        async def peek(ctx):
            seen["bound"] = telemetry.outbound_context.get()

        await _bootstrap(db, runner, schema, upto=2)
        await runner.enqueue("peek")

        claimed = await runner._claim(1)
        await runner._run(claimed[0])

        assert seen["bound"] is None, (
            f"bound {seen['bound']!r}; a non-None binding reaches "
            "parent.encode() in the outbound client and raises"
        )
    finally:
        await _drop(db, schema)


@requires_db
async def test_an_enqueue_with_no_context_asks_the_database_nothing_extra() -> None:
    """No context to carry means no schema question, ever.

    The `parent is not None` short-circuit is why: an application that never
    propagates -- a worker-only process, a CLI -- must not pay a `pg_attribute`
    lookup to discover a column it has nothing to put in. Dropping that clause
    stores the same NULL, which is exactly why a mutant survived here until this
    test existed: the *value* is unchanged and the *cost* is not.
    """
    schema = _schema("nocost")
    db = await _database(schema)
    try:
        runner = JobRunner(db, name="work", schema=schema, concurrency=1, lease=1.0)

        @runner.task("noop")
        async def noop(ctx):
            pass

        await _bootstrap(db, runner, schema, upto=2)

        await runner.enqueue("noop")
        assert runner._trace_column is None, (
            "an untraced enqueue resolved the schema shape it had no use for"
        )

        token = telemetry.outbound_context.set((_TRACEPARENT, ""))
        try:
            await runner.enqueue("noop")
        finally:
            telemetry.outbound_context.reset(token)
        assert runner._trace_column is True, "a traced enqueue must resolve it"
    finally:
        await _drop(db, schema)


@requires_db
async def test_an_unpropagated_enqueue_writes_null_not_an_empty_string() -> None:
    """Absence has to stay absent.

    An empty string is a value, and a `WHERE trace_context IS NOT NULL` lookup
    would find every untraced job. This is the same distinction the second
    factor's `second_factor_age` makes by omitting the key rather than faking a
    zero.
    """
    schema = _schema("absent")
    db = await _database(schema)
    try:
        runner = JobRunner(db, name="work", schema=schema, concurrency=1, lease=1.0)

        @runner.task("noop")
        async def noop(ctx):
            pass

        await _bootstrap(db, runner, schema, upto=2)

        job_id = await runner.enqueue("noop")

        stored = await _fetchval(
            db, f'SELECT trace_context FROM "{schema}".jobs WHERE id = $1', job_id
        )
        assert stored is None

        claimed = await runner._claim(1)
        assert claimed[0].trace_context is None
        await runner._run(claimed[0])
    finally:
        await _drop(db, schema)
