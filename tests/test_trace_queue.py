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


def test_the_trace_column_arrives_as_an_additive_step() -> None:
    runner = JobRunner(_FakeDatabase(), name="work", schema="wtq_decl")
    component = runner.component()

    versions = [step.version for step in component.steps]
    assert versions == sorted(versions), "steps apply in order"
    assert versions == list(range(1, len(versions) + 1)), "versions are dense from 1"
    assert component.target_version == versions[-1]

    first = " ".join(component.steps[0].statements)
    assert "trace_context" not in first, "version 1 must stay as it shipped"

    carrying = [
        step
        for step in component.steps
        if any("trace_context" in statement for statement in step.statements)
    ]
    assert [step.version for step in carrying] == [2]


def test_every_schema_step_is_idempotent() -> None:
    component = JobRunner(_FakeDatabase(), name="work", schema="wtq_decl").component()
    for step in component.steps:
        for statement in step.statements:
            assert "IF NOT EXISTS" in statement or "IF EXISTS" in statement, (
                f"version {step.version} statement is not re-appliable: {statement}"
            )


def test_every_notify_in_the_queue_carries_an_empty_payload() -> None:
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


async def _bootstrap(
    db: Database, runner: JobRunner, schema: str, *, upto: int | None = None
) -> None:
    """Apply the component's steps, stopping after `upto`.

    `upto=None` applies **all** of them, which is what a test wanting a working
    table means. Writing the current head as a literal there pinned these tests
    to whatever the last version happened to be, so adding a step broke five
    tests that were not about versioning at all -- the same brittleness the
    declaration test above had, one layer down.

    Stopping short is how the "new build meets an older schema" case is built:
    a deployment whose DBA has applied version 1 by hand and not yet the rest.
    Pass a number only where *that* is the subject.
    """
    from wreath.schema import Component, bootstrap

    full = runner.component()
    partial = Component(
        name=full.name,
        schema=full.schema,
        relations=full.relations,
        steps=full.steps if upto is None else tuple(s for s in full.steps if s.version <= upto),
    )
    await bootstrap(db, [partial], schema=schema)


async def test_a_database_down_at_boot_does_not_stop_the_runner_starting() -> None:

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
    assert "trace_context" not in runner._columns, "unresolved, so a later enqueue retries"
    assert any("worker" in name for name in supervisor.spawned), (
        "the runner must still come up: the probe is not a precondition of work"
    )


@requires_db
async def test_a_new_build_against_a_version_one_schema_still_enqueues_and_runs() -> None:
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
    from wreath.schema import Component, bootstrap

    schema = _schema("rollfwd")
    db = await _database(schema)
    try:
        runner = JobRunner(db, name="work", schema=schema, concurrency=1, lease=1.0)
        full = runner.component()
        head = full.target_version
        assert await bootstrap(db, [full], schema=schema) == {"jobs": head}

        older = Component(
            name=full.name,
            schema=full.schema,
            relations=full.relations,
            steps=(full.steps[0],),
        )
        assert await bootstrap(db, [older], schema=schema) == {"jobs": head}
    finally:
        await _drop(db, schema)


@requires_db
async def test_enqueue_persists_the_context_and_the_runner_restores_it() -> None:
    schema = _schema("carry")
    db = await _database(schema)
    try:
        runner = JobRunner(db, name="work", schema=schema, concurrency=1, lease=1.0)
        seen: dict[str, object] = {}

        @runner.task("look")
        async def look(ctx):
            seen["context"] = ctx.trace_context
            seen["bound"] = telemetry.outbound_context.get()

        await _bootstrap(db, runner, schema)

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
    schema = _schema("doorbell")
    db = await _database(schema)
    try:
        runner = JobRunner(db, name="work", schema=schema, concurrency=1, lease=1.0)

        @runner.task("noop")
        async def noop(ctx):
            pass

        await _bootstrap(db, runner, schema)

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
            f"the doorbell delivered {payloads!r}; a traced enqueue must still ring an empty bell"
        )
    finally:
        await _drop(db, schema)


@requires_db
async def test_an_untraced_job_binds_none_not_a_hollow_pair() -> None:
    schema = _schema("hollow")
    db = await _database(schema)
    try:
        runner = JobRunner(db, name="work", schema=schema, concurrency=1, lease=1.0)
        seen: dict[str, object] = {}

        @runner.task("peek")
        async def peek(ctx):
            seen["bound"] = telemetry.outbound_context.get()

        await _bootstrap(db, runner, schema)
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
    schema = _schema("nocost")
    db = await _database(schema)
    try:
        runner = JobRunner(db, name="work", schema=schema, concurrency=1, lease=1.0)

        @runner.task("noop")
        async def noop(ctx):
            pass

        await _bootstrap(db, runner, schema)

        await runner.enqueue("noop")
        assert "trace_context" not in runner._columns, (
            "an untraced enqueue resolved the schema shape it had no use for"
        )

        token = telemetry.outbound_context.set((_TRACEPARENT, ""))
        try:
            await runner.enqueue("noop")
        finally:
            telemetry.outbound_context.reset(token)
        assert runner._columns.get("trace_context") is True, "a traced enqueue must resolve it"
    finally:
        await _drop(db, schema)


@requires_db
async def test_an_unpropagated_enqueue_writes_null_not_an_empty_string() -> None:
    schema = _schema("absent")
    db = await _database(schema)
    try:
        runner = JobRunner(db, name="work", schema=schema, concurrency=1, lease=1.0)

        @runner.task("noop")
        async def noop(ctx):
            pass

        await _bootstrap(db, runner, schema)

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
