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
                f'ALTER TABLE "{schema}"."workflow_steps_instances" DROP COLUMN trace_context'
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
