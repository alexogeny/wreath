"""Plan 01 stage 3, the passes half: a chunked pass is one trace.

A pass is the third durable instance in the tree, after a job row and a workflow
instance, and it gets the shape both of those settled on: the traceparent rides
the durable row and every execution rebinds it, so the *instance* owns the trace
rather than whichever worker happens to be driving it.

The judgement the plan left open -- what "the instance's trace" means for a pass
nobody requested -- is answered by **capture, never mint**, and by scoping the
capture to a *cycle*. The reasoning is written out at `Ledger.seed`; the tests
below pin each half of it:

* the drive that starts the pass is recorded, and a later shift runs under it
  rather than under its own driver's context;
* a pass that has never been driven with a trace stores SQL `NULL` and binds
  `None`, rather than minting an id nothing will collect or leaking the ambient
  one;
* a recurring pass re-captures at the cycle boundary, so its trace lives one
  cycle instead of the process's lifetime.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from wreath import _pytest_plugin, telemetry
from wreath._passes import ledger as _ledger
from wreath.passes import (
    Apply,
    ChunkedPass,
    Declared,
    DutyCycle,
    Key,
    Purge,
    Rows,
    Sealed,
    Table,
)

from .conftest import expired_rows
from .fakes import FakeDatabase, World

PARENT = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
OTHER = "00-" + "c" * 32 + "-" + "d" * 16 + "-01"

EXPIRES = Key("expires", "timestamptz", indexed=True)
KEY = Key("key", "text", unique=True)

_DSN = os.environ.get(_pytest_plugin.DSN_ENV)


async def _nap(_seconds):
    """Pacing without the wall clock."""


def _watching(seen: list[object], stop: asyncio.Event | None = None):
    """Work that records the trace context each chunk runs under.

    *stop* ends the shift after one chunk, which is how a test gets a pass to be
    genuinely mid-walk: the shift budget is wall-clock and a zero budget returns
    before the first chunk rather than after it.
    """

    async def observe(tx, chunk, binds):
        seen.append(telemetry.outbound_context.get())
        if stop is not None:
            stop.set()
        return await Purge().apply(tx, chunk, binds)

    return Apply(
        observe,
        idempotent=Declared("a delete re-run over the same range matches nothing"),
    )


def _pass(work, *, name: str = "purge_replays", **overrides):
    options = {
        "over": Table("replays"),
        "units": Rows(key=(EXPIRES, KEY), limit=3, within="2s"),
        "frontier": Sealed(),
        "work": work,
        "pace": DutyCycle(1.0),
    }
    options.update(overrides)
    return ChunkedPass(name, **options)


def _bound(parent: str | None):
    return telemetry.outbound_context.set(None if parent is None else (parent, ""))


async def _one_chunk(walk, database, seen: list[object], *, under: str | None):
    """Drive exactly one chunk of *walk*, with *under* bound as the ambient trace."""
    stop = asyncio.Event()
    walk._work = _watching(seen, stop)
    token = _bound(under)
    try:
        return await walk.run_shift(database, sleep=_nap, stopping=stop)
    finally:
        telemetry.outbound_context.reset(token)


class TestTheDriveThatStartsAPassOwnsItsTrace:
    async def test_the_starting_drive_is_recorded_on_the_ledger_row(self):
        world = World("replays", expired_rows(4))
        database = FakeDatabase(world)
        token = _bound(PARENT)
        try:
            await _pass(Purge()).run(database, sleep=_nap)
        finally:
            telemetry.outbound_context.reset(token)

        assert world.ledger_row()["trace_context"] == PARENT

    async def test_a_later_shift_runs_under_the_passs_trace_not_its_drivers(self):
        """The property the whole row exists for, and the strong version of it.

        The second shift is driven with a *different* context bound rather than
        with none: a shift that simply inherited the ambient value would pass
        against a weaker test, and inheriting is exactly the defect.
        """
        world = World("replays", expired_rows(12))
        database = FakeDatabase(world)
        seen: list[object] = []
        walk = _pass(Purge())

        await _one_chunk(walk, database, seen, under=PARENT)
        assert seen == [(PARENT, "")]

        seen.clear()
        await _one_chunk(walk, database, seen, under=OTHER)
        assert seen == [(PARENT, "")], (
            "a later shift ran under its own driver's trace, so a pass is one "
            "trace per shift rather than one trace"
        )

    async def test_an_untraced_pass_binds_none_rather_than_leaking(self):
        """No minting, and no misattribution either.

        A pass first driven by `cron` has no originating request. It stores
        `NULL`, and a shift driven with an unrelated ambient trace binds `None`
        rather than adopting it mid-walk -- the same staleness rule the request
        pipeline, the job runner and the workflow engine all had to adopt,
        because a trace naming the wrong cause is worse than one naming none.
        """
        world = World("replays", expired_rows(12))
        database = FakeDatabase(world)
        seen: list[object] = []
        walk = _pass(Purge())

        await _one_chunk(walk, database, seen, under=None)
        assert seen == [None]
        assert world.ledger_row()["trace_context"] is None

    async def test_the_first_traced_drive_adopts_a_pass_seeded_untraced(self):
        """`COALESCE`, not overwrite: the first drive that *has* a trace names it.

        A pass seeded at boot has `NULL`, and refusing to ever record one would
        make the column dead for the common case. Once written it is not
        replaced, so a third drive does not re-attribute a walk already under
        way.
        """
        world = World("replays", expired_rows(18))
        database = FakeDatabase(world)
        seen: list[object] = []
        walk = _pass(Purge())

        await _one_chunk(walk, database, seen, under=None)
        assert world.ledger_row()["trace_context"] is None

        await _one_chunk(walk, database, seen, under=PARENT)
        assert world.ledger_row()["trace_context"] == PARENT

        seen.clear()
        await _one_chunk(walk, database, seen, under=OTHER)
        assert world.ledger_row()["trace_context"] == PARENT, (
            "a later drive re-attributed a walk that was already under way"
        )
        assert seen == [(PARENT, "")]


class TestACyclesTraceDoesNotOutliveTheCycle:
    async def test_a_new_cycle_recaptures_rather_than_inheriting(self):
        """The retention bound, applied at the only boundary the ledger has.

        A recurring pass runs for the life of the deployment. Carrying one
        drive's traceparent across every cycle would make a trace that never
        ends, which no backend assembles; the cycle is the instance, so the
        cycle boundary is where the trace restarts.
        """
        world = World("replays", expired_rows(3))
        database = FakeDatabase(world)
        walk = _pass(Purge(), name="recurring_purge")

        token = _bound(PARENT)
        try:
            await walk.run(database, sleep=_nap)
        finally:
            telemetry.outbound_context.reset(token)
        assert world.ledger_row()["trace_context"] == PARENT

        world.rows = expired_rows(3)
        other = _bound(OTHER)
        try:
            await walk.run(database, sleep=_nap)
        finally:
            telemetry.outbound_context.reset(other)
        assert world.ledger_row()["trace_context"] == OTHER, (
            "a recurring pass carried one drive's trace into a later cycle, so "
            "the trace never ends"
        )


class TestTheSchemaMayBeOlderThanTheBuild:
    async def test_a_build_meeting_a_version_one_ledger_walks_untraced(self):
        """Losing the trace is a degradation; losing the walk is not.

        A deployment whose role cannot `CREATE SCHEMA` applies the DDL by hand,
        so there is always a window in which this build is newer than the table
        it meets. The column's absence is a precondition the ledger checks, not
        an error it catches -- a broad `except` around the seed would swallow a
        revoked grant with it.
        """
        world = World("replays", expired_rows(4))
        world.trace_column = False
        database = FakeDatabase(world)
        seen: list[object] = []

        token = _bound(PARENT)
        try:
            await _pass(_watching(seen)).run(database, sleep=_nap)
        finally:
            telemetry.outbound_context.reset(token)

        assert seen and seen == [None] * len(seen)
        assert "trace_context" not in world.ledger_row()

    async def test_a_version_one_ledger_is_never_asked_for_the_column(self):
        """Asserted on the *statement*, not on the value it returns.

        A fake hands its ledger row back whatever the projection said, so a
        `SELECT` that wrongly named a column it does not have would still pass a
        value assertion. A real server answers `column "trace_context" does not
        exist`, so the statement is the thing to pin -- which is what a mutant
        sweep reported when both arms of the projection ternary survived.
        """
        world = World("replays", expired_rows(4))
        world.trace_column = False
        database = FakeDatabase(world)

        await _pass(Purge()).run(database, sleep=_nap)

        touching = [
            sql for sql, _args in world.statements
            if "trace_context" in sql and "pg_attribute" not in sql
        ]
        assert not touching, touching

    async def test_a_version_one_ledger_still_begins_a_new_cycle(self):
        """The rollover's own version-1 arm, which the walk's arm does not reach."""
        world = World("replays", expired_rows(3))
        world.trace_column = False
        database = FakeDatabase(world)
        walk = _pass(Purge(), name="recurring_purge")

        await walk.run(database, sleep=_nap)
        world.rows = expired_rows(3)
        token = _bound(PARENT)
        try:
            result = await walk.run(database, sleep=_nap)
        finally:
            telemetry.outbound_context.reset(token)

        assert result.rows == 3, "the second cycle did not walk"
        assert "trace_context" not in world.ledger_row("recurring_purge")

    def test_version_one_of_the_component_has_no_trace_column(self):
        """Step 1 is left exactly as it shipped.

        Rewriting it would change what an already-bootstrapped database was told
        it had, and `wreath.schema` records the version rather than the DDL.
        """
        component = _ledger.component("wreath")
        first = next(step for step in component.steps if step.version == 1)
        assert not any("trace_context" in s for s in first.statements)
        assert component.target_version == 2
        second = next(step for step in component.steps if step.version == 2)
        assert any("trace_context" in s for s in second.statements)

    async def test_the_catalog_is_asked_once_per_ledger(self):
        """The probe is off the chunk path, as it is for jobs and workflows.

        `wreath.jobs`'s first draft put the same lookup inside `_claim`, which a
        robustness double caught immediately. A pass issues its ledger reads
        several times a shift and runs for days, so the same rule holds here.
        """
        world = World("replays", expired_rows(9))
        database = FakeDatabase(world)
        await _pass(Purge()).run(database, sleep=_nap)
        probes = [sql for sql, _args in world.statements if "pg_attribute" in sql]
        assert len(probes) == 1, f"probed the catalog {len(probes)} times"


@pytest.mark.database
@pytest.mark.skipif(not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)")
async def test_a_pass_carries_its_trace_across_shifts_against_a_live_server() -> None:
    """The claim the fake cannot make: the column exists and the value survives.

    Driven by a *second* `ChunkedPass` object with a *different* context bound,
    which is the closest a test gets to "the worker was redeployed mid-backfill".
    """
    from wreath.postgres import Database

    worker = os.environ.get("PYTEST_XDIST_WORKER", "solo")
    schema = f"wreath_trace_passes_{worker}"

    database = Database("test", _DSN, pools={"write": {"min_size": 1, "max_size": 2}})
    await database.start()
    try:
        connection = await database.acquire("write")
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await connection.execute(f'CREATE SCHEMA "{schema}"')
            for statement in _ledger.component(schema).statements():
                await connection.execute(statement)
            await connection.execute(
                f'CREATE TABLE "{schema}"."replays" '
                "(key text PRIMARY KEY, expires timestamptz NOT NULL)"
            )
            for index in range(9):
                await connection.execute(
                    f'INSERT INTO "{schema}".replays (key, expires) '
                    "VALUES ($1, now() - interval '1 hour')",
                    f"k{index:03d}",
                )
        finally:
            await database.release("write", connection)

        seen: list[object] = []

        def build(work):
            return ChunkedPass(
                "live_purge",
                over=Table("replays", schema=schema),
                units=Rows(key=(EXPIRES, KEY), limit=3, within="2s"),
                frontier=Sealed(),
                work=work,
                pace=DutyCycle(1.0),
                schema=schema,
            )

        first = build(Purge())
        stop = asyncio.Event()
        first._work = _watching([], stop)
        token = _bound(PARENT)
        try:
            await first.run_shift(database, sleep=_nap, stopping=stop)
        finally:
            telemetry.outbound_context.reset(token)

        second = build(Purge())
        stop = asyncio.Event()
        second._work = _watching(seen, stop)
        other = _bound(OTHER)
        try:
            await second.run_shift(database, sleep=_nap, stopping=stop)
        finally:
            telemetry.outbound_context.reset(other)

        assert seen == [(PARENT, "")], (
            "a shift driven by a second worker ran under its own trace, so a "
            "backfill spanning a redeploy is two traces rather than one"
        )

        status = await second.status(database)
        assert status is not None
        assert status.trace_context == PARENT
    finally:
        await database.stop()
