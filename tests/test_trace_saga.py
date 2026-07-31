"""Plan 01 stage 3: a saga is one trace, across a resume.

Lane V left the inheritance question unverified. It resolves in two halves, and
they point opposite ways:

* Within one execution, a workflow step already inherits the ambient
  `outbound_context` through the ContextVar the job runner binds -- no mechanism
  needed, but nothing asserted it, so a refactor could silently take it away.
* Across a `resume`, it does not. The instance re-enters in a different worker
  with whatever context that worker happens to hold, which is usually none. That
  is precisely the case durable workflows exist for, so the trace breaks at the
  one moment it is being relied on.

The fix is the same shape stage 2 used for jobs: the context rides the durable
row and is rebound on every execution, so the instance -- not the caller -- owns
the trace.
"""

from __future__ import annotations

import os

import pytest

from wreath import _pytest_plugin, telemetry
from wreath.workflows import InMemoryWorkflowStore, PostgresWorkflowStore, Workflow

PARENT = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
OTHER = "00-" + "c" * 32 + "-" + "d" * 16 + "-01"

_DSN = os.environ.get(_pytest_plugin.DSN_ENV)


def _seen() -> list[tuple[str, object]]:
    return []


class TestASagaIsOneTrace:
    @pytest.mark.asyncio
    async def test_a_step_inherits_the_context_of_the_run_that_started_it(self):
        """The half that already worked. Pinned so a refactor cannot remove it."""
        flow = Workflow("checkout")
        seen = _seen()

        @flow.step
        async def reserve(context):
            seen.append(("reserve", telemetry.outbound_context.get()))
            return {"ok": True}

        store = InMemoryWorkflowStore()
        token = telemetry.outbound_context.set((PARENT, ""))
        try:
            await flow.run(store=store, key="k")
        finally:
            telemetry.outbound_context.reset(token)

        assert seen == [("reserve", (PARENT, ""))]

    @pytest.mark.asyncio
    async def test_a_resumed_step_sees_the_instances_trace_not_the_workers(self):
        """The half that did not. This is the defect stage 3 exists to close.

        The resume deliberately runs with a *different* context bound, not with
        none: binding nothing would pass against an implementation that simply
        leaked the ambient value, which is the bug being fixed.
        """
        flow = Workflow("shipping")
        attempts = {"n": 0}
        seen = _seen()

        @flow.step
        async def pack(context):
            seen.append(("pack", telemetry.outbound_context.get()))
            return {"ok": True}

        @flow.step
        async def ship(context):
            attempts["n"] += 1
            seen.append(("ship", telemetry.outbound_context.get()))
            if attempts["n"] == 1:
                raise RuntimeError("carrier down")
            return {"ok": True}

        store = InMemoryWorkflowStore()
        token = telemetry.outbound_context.set((PARENT, ""))
        try:
            with pytest.raises(RuntimeError):
                await flow.run(store=store, key="k", compensate=False)
        finally:
            telemetry.outbound_context.reset(token)

        seen.clear()
        other = telemetry.outbound_context.set((OTHER, ""))
        try:
            outcome = await flow.resume(store=store, key="k")
        finally:
            telemetry.outbound_context.reset(other)

        assert outcome.state == "completed"
        assert seen == [("ship", (PARENT, ""))], (
            "the resumed step must run under the trace of the run that began the "
            "instance, not the worker's own"
        )

    @pytest.mark.asyncio
    async def test_a_compensation_runs_under_the_instances_trace(self):
        """The plan's words: a compensation is *visibly part of* the saga."""
        flow = Workflow("booking")
        seen = _seen()

        async def release(context):
            seen.append(("release", telemetry.outbound_context.get()))

        @flow.step(compensate=release)
        async def hold(context):
            return {"ok": True}

        @flow.step
        async def charge(context):
            raise RuntimeError("declined")

        store = InMemoryWorkflowStore()
        token = telemetry.outbound_context.set((PARENT, ""))
        try:
            with pytest.raises(RuntimeError):
                await flow.run(store=store, key="k")
        finally:
            telemetry.outbound_context.reset(token)

        assert seen == [("release", (PARENT, ""))]

    @pytest.mark.asyncio
    async def test_an_untraced_instance_binds_nothing_rather_than_leaking(self):
        """An instance begun with no trace must not hand a later worker its own.

        The mirror of the staleness stage 1 and 2 both had to fix: binding
        `(None, "")` instead of `None` crashes `_propagated` on `.encode()`, and
        binding nothing at all leaks whatever the worker already held.
        """
        flow = Workflow("quiet")
        attempts = {"n": 0}
        seen = _seen()

        @flow.step
        async def once(context):
            attempts["n"] += 1
            seen.append(("once", telemetry.outbound_context.get()))
            if attempts["n"] == 1:
                raise RuntimeError("nope")
            return {"ok": True}

        store = InMemoryWorkflowStore()
        with pytest.raises(RuntimeError):
            await flow.run(store=store, key="k", compensate=False)

        seen.clear()
        other = telemetry.outbound_context.set((OTHER, ""))
        try:
            await flow.resume(store=store, key="k")
        finally:
            telemetry.outbound_context.reset(other)

        assert seen == [("once", None)], (
            "an instance that began untraced must bind None on resume, not the "
            "resuming worker's context"
        )


async def _apply_schema(database, schema: str) -> None:
    connection = await database.acquire("write")
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for statement in PostgresWorkflowStore.schema_sql(schema=schema).split(";\n"):
            if statement.strip():
                await connection.execute(statement.strip())
    finally:
        await database.release("write", connection)


@pytest.mark.database
@pytest.mark.skipif(not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)")
async def test_a_saga_resumed_by_another_worker_is_one_trace() -> None:
    """The claim the in-memory store cannot make, against a real database.

    The resume is driven by a *second* `Workflow` object built after the first is
    gone and with a different context bound -- the closest a test gets to "the
    worker died and another picked it up", which is the only situation the
    instance-owns-the-trace rule exists for.
    """
    from wreath.postgres import Database

    # Per-worker schema, assigned not defaulted: workers sharing one schema race
    # on CREATE SCHEMA, and PostgreSQL reports that race as a pg_namespace unique
    # violation, which reads like anything except a test-isolation bug.
    worker = os.environ.get("PYTEST_XDIST_WORKER", "solo")
    schema = f"wreath_trace_saga_{worker}"

    database = Database("test", _DSN, pools={"write": {"min_size": 1, "max_size": 2}})
    await database.start()
    try:
        await _apply_schema(database, schema)
        store = PostgresWorkflowStore(database, schema=schema)
        seen: list[object] = []

        first = Workflow("payout")

        @first.step
        async def reserve(context):
            return "hold-1"

        @first.step
        async def pay(context):
            raise RuntimeError("gateway down")

        token = telemetry.outbound_context.set((PARENT, ""))
        try:
            with pytest.raises(RuntimeError):
                await first.run(store=store, key="p1", compensate=False)
        finally:
            telemetry.outbound_context.reset(token)

        # A second worker, a second Workflow object, a different ambient trace.
        second = Workflow("payout")

        @second.step
        async def reserve(context):  # noqa: F811 - the other worker's copy
            return "hold-1"

        @second.step
        async def pay(context):  # noqa: F811 - the other worker's copy
            seen.append(telemetry.outbound_context.get())
            return "paid"

        other = telemetry.outbound_context.set((OTHER, ""))
        try:
            outcome = await second.resume(store=store, key="p1")
        finally:
            telemetry.outbound_context.reset(other)

        assert outcome.state == "completed"
        assert seen == [(PARENT, "")], (
            "the resumed step ran under the resuming worker's trace, so the saga "
            "is two traces rather than one"
        )
    finally:
        await database.stop()


@pytest.mark.database
@pytest.mark.skipif(not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)")
async def test_the_column_probe_happens_once_and_is_cached() -> None:
    """The cost property, asserted rather than assumed.

    Written first as "an untraced `begin` never probes", which **failed** and was
    right to: `load` probes unconditionally -- it must, to know whether to select
    the column -- and `begin` calls `load`, so the short-circuit that used to
    guard the probe saved nothing. `wreath mutant` had already reported that
    guard as survivable, which is what redundant code looks like from outside.
    The guard is gone; this pins what actually holds, which is that the catalog
    is asked once per store and never again.
    """
    from wreath.postgres import Database

    worker = os.environ.get("PYTEST_XDIST_WORKER", "solo")
    schema = f"wreath_trace_cost_{worker}"

    database = Database("test", _DSN, pools={"write": {"min_size": 1, "max_size": 2}})
    await database.start()
    try:
        await _apply_schema(database, schema)
        store = PostgresWorkflowStore(database, schema=schema)
        flow = Workflow("quiet")

        @flow.step
        async def only(context):
            return "done"

        assert store._trace_column is None, "the store probed before it was used"

        await flow.run(store=store, key="q1")
        assert store._trace_column is True, "the first use must resolve the column"

        # Plant a wrong answer. A second instance must keep it: the probe is
        # cached, so a worker starting a thousand sagas asks the catalog once.
        # Re-probing would overwrite this back to True and fail the assertion.
        store._trace_column = False
        await flow.run(store=store, key="q2")
        assert store._trace_column is False, (
            "the cached answer was discarded and the catalog re-queried; the "
            "probe is back on the per-saga path"
        )
    finally:
        await database.stop()


@pytest.mark.database
@pytest.mark.skipif(not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)")
async def test_a_new_build_against_an_older_schema_runs_untraced() -> None:
    """Degrade, never fail. The mirror of stage 2's rollout property.

    A schema created before this change has no `trace_context` column. The saga
    must still run and still resume; losing the trace is a degradation, losing
    the workflow is not.
    """
    from wreath.postgres import Database

    worker = os.environ.get("PYTEST_XDIST_WORKER", "solo")
    schema = f"wreath_trace_old_{worker}"

    database = Database("test", _DSN, pools={"write": {"min_size": 1, "max_size": 2}})
    await database.start()
    try:
        await _apply_schema(database, schema)
        # Drop the column back off, reproducing a table an older build created.
        connection = await database.acquire("write")
        try:
            await connection.execute(
                f'ALTER TABLE "{schema}"."workflow_steps_instances" '
                "DROP COLUMN trace_context"
            )
        finally:
            await database.release("write", connection)

        store = PostgresWorkflowStore(database, schema=schema)
        flow = Workflow("legacy")
        seen: list[object] = []
        attempts = {"n": 0}

        @flow.step
        async def only(context):
            attempts["n"] += 1
            seen.append(telemetry.outbound_context.get())
            if attempts["n"] == 1:
                raise RuntimeError("first go")
            return "done"

        token = telemetry.outbound_context.set((PARENT, ""))
        try:
            with pytest.raises(RuntimeError):
                await flow.run(store=store, key="l1", compensate=False)
        finally:
            telemetry.outbound_context.reset(token)

        seen.clear()
        outcome = await flow.resume(store=store, key="l1")
        assert outcome.state == "completed"
        assert seen == [None], "an older schema must resume untraced, not raise"
    finally:
        await database.stop()
