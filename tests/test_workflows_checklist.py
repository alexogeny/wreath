"""`wreath.workflows`: durable multi-step sagas, and the properties that define one.

`wreath.jobs` gives one durable unit of work -- claimed with `FOR UPDATE SKIP
LOCKED`, fenced, retried, dead-lettered. What it cannot say is "these steps in
order, and if step 4 fails, undo 3 then 2", durably, so that a worker crash
resumes at step 4 rather than re-running step 1. That is what this covers.

The tests are ordered as the contract was written: declaration, ordering, resume,
result threading, compensation and its two boundaries, the rename trap,
exactly-once start, progress, and the storage rule a second durable table
inherits from `jobs`.
"""

from __future__ import annotations

import os

import pytest

from wreath import _pytest_plugin
from wreath.workflows import (
    InMemoryWorkflowStore,
    PostgresWorkflowStore,
    UnknownWorkflowInstance,
    Workflow,
    WorkflowDefinitionChanged,
)

_DSN = os.environ.get(_pytest_plugin.DSN_ENV)


def test_a_workflow_declares_ordered_steps() -> None:
    """The declaration is the whole API surface a caller sees first."""
    checkout = Workflow("checkout")

    @checkout.step
    async def reserve_stock(context) -> str:
        return "reserved"

    @checkout.step
    async def charge_card(context) -> str:
        return "charged"

    assert [step.name for step in checkout.steps] == ["reserve_stock", "charge_card"]


async def test_steps_run_in_declaration_order() -> None:
    order: list[str] = []
    flow = Workflow("ordered")

    @flow.step
    async def first(context) -> None:
        order.append("first")

    @flow.step
    async def second(context) -> None:
        order.append("second")

    await flow.run(store=_memory_store())
    assert order == ["first", "second"]


async def test_a_completed_step_does_not_run_again_on_resume() -> None:
    """The property that distinguishes this from a `for` loop over callables.

    A crash between step 2 and step 3 must resume at step 3. If step 1 re-runs,
    every non-idempotent side effect in it happens twice, which is the failure
    people write sagas to avoid in the first place.
    """
    runs: list[str] = []
    store = _memory_store()
    flow = Workflow("resumable")

    @flow.step
    async def one(context) -> None:
        runs.append("one")

    @flow.step
    async def two(context) -> None:
        runs.append("two")
        if len(runs) < 3:
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await flow.run(store=store, key="instance-1", compensate=False)
    await flow.resume(store=store, key="instance-1")
    assert runs == ["one", "two", "two"], "step `one` must not have re-run"


async def test_a_step_result_is_visible_to_later_steps() -> None:
    """Otherwise every step re-reads what the one before it just computed."""
    flow = Workflow("threaded")

    @flow.step
    async def make_id(context) -> str:
        return "order-7"

    @flow.step
    async def use_id(context) -> str:
        return context.results["make_id"]

    outcome = await flow.run(store=_memory_store())
    assert outcome.results["use_id"] == "order-7"


async def test_failure_compensates_completed_steps_in_reverse_order() -> None:
    """Reverse order is not cosmetic: step 3's undo may depend on step 2's state."""
    undone: list[str] = []
    flow = Workflow("compensating")

    @flow.step(compensate=lambda context: undone.append("one"))
    async def one(context) -> None:
        pass

    @flow.step(compensate=lambda context: undone.append("two"))
    async def two(context) -> None:
        pass

    @flow.step
    async def three(context) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await flow.run(store=_memory_store())
    assert undone == ["two", "one"]


async def test_the_failing_step_is_not_compensated() -> None:
    """A step that raised did not complete, so there is nothing of its to undo.

    Compensating it would run an undo against state that was never established --
    the shape that turns a recoverable failure into a corrupt one.
    """
    undone: list[str] = []
    flow = Workflow("boundary")

    @flow.step(compensate=lambda context: undone.append("failing"))
    async def failing(context) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await flow.run(store=_memory_store())
    assert undone == []


async def test_a_compensation_that_raises_is_counted_not_swallowed() -> None:
    """AGENTS.md's rule, applied to the one place it is most tempting to break.

    Compensation runs while something has already gone wrong, so the temptation is
    `except Exception: pass` to get through the rest of the undo chain. That leaves
    a half-compensated saga with no signal. The remaining compensations must still
    run *and* the failure must be visible.
    """
    undone: list[str] = []
    store = _memory_store()
    flow = Workflow("noisy-undo")

    def _raise(context) -> None:
        raise OSError("undo failed")

    @flow.step(compensate=lambda context: undone.append("first"))
    async def first(context) -> None:
        pass

    @flow.step(compensate=_raise)
    async def second(context) -> None:
        pass

    @flow.step
    async def third(context) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await flow.run(store=store, key="instance-1")

    assert undone == ["first"], "a failed undo must not stop the ones behind it"
    # Asserted *after* the `raises` block, not inside it: a run that compensates
    # re-raises, so anything written after the awaited call inside `pytest.raises`
    # is unreachable and would pass without ever executing.
    status = await flow.status(store=store, key="instance-1")
    assert status is not None
    assert status.state == "compensated"
    assert status.compensation_errors == 1, "the failed undo must leave a signal"


async def test_renaming_a_step_of_a_running_instance_is_refused() -> None:
    """The silent-re-run trap, made loud.

    Progress is recorded per step *name*. Rename a step while an instance is
    mid-flight and the recorded completion no longer matches anything, so a naive
    resume re-runs work it already did -- and reports success. It must refuse
    instead, naming the step it cannot account for.
    """
    store = _memory_store()
    before = Workflow("renamed")

    @before.step
    async def old_name(context) -> None:
        raise RuntimeError("stop here")

    with pytest.raises(RuntimeError):
        await before.run(store=store, key="instance-1", compensate=False)

    after = Workflow("renamed")

    @after.step
    async def new_name(context) -> None:
        pass

    with pytest.raises(WorkflowDefinitionChanged, match="old_name"):
        await after.resume(store=store, key="instance-1")


async def test_one_instance_key_runs_once() -> None:
    """Exactly-once *start*, the same guarantee `jobs.enqueue(key=...)` gives."""
    started: list[int] = []
    store = _memory_store()
    flow = Workflow("keyed")

    @flow.step
    async def only(context) -> None:
        started.append(1)

    await flow.run(store=store, key="instance-1")
    await flow.run(store=store, key="instance-1")
    assert started == [1]


async def test_progress_is_reported_through_the_existing_registry() -> None:
    """Through `wreath.progress`, not a second mechanism.

    The same rule `wreath.mcp` follows for its notification stream: a long-running
    thing reports progress where everything else already reports it, so one status
    endpoint serves both.
    """
    from wreath.progress import ProgressRegistry
    registry = ProgressRegistry()
    flow = Workflow("reporting")

    @flow.step
    async def one(context) -> None:
        pass

    @flow.step
    async def two(context) -> None:
        pass

    await flow.run(store=_memory_store(), key="instance-1", progress=registry)
    assert registry.get("instance-1") is not None


def test_the_postgres_store_keeps_its_own_schema_and_tenant_column() -> None:
    """`jobs.py`'s multi-tenancy rule, which a second durable table must not break.

    Jobs live in one dedicated system schema with a `tenant` column rather than
    relying on `search_path`, because a database-global `NOTIFY` name would
    otherwise wake the wrong tenant's workers. A workflow step table is the same
    kind of object and inherits the same constraint.
    """
    ddl = PostgresWorkflowStore.schema_sql()
    assert "tenant" in ddl
    assert "search_path" not in ddl


def _memory_store() -> InMemoryWorkflowStore:
    """The store the checklist runs against: step records, no database."""
    return InMemoryWorkflowStore()


@pytest.mark.database
@pytest.mark.skipif(not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)")
async def test_postgres_store_survives_a_crash_between_steps() -> None:
    """The claim the in-memory store cannot make, against a real database.

    `InMemoryWorkflowStore` satisfies the resume contract *within* one process,
    which is precisely not the situation sagas exist for. This drives the same
    contract through DDL, JSONB round-tripping and a second `Workflow` object
    built after the first one is gone -- the closest a test gets to "the worker
    died and another picked it up".
    """
    from wreath.postgres import Database
    from wreath.workflows import PostgresWorkflowStore, Workflow

    # Per-worker schema, assigned not defaulted: workers sharing one schema race
    # on CREATE SCHEMA, and PostgreSQL reports that race as a pg_namespace unique
    # violation, which reads like anything except a test-isolation bug.
    worker = os.environ.get("PYTEST_XDIST_WORKER", "solo")
    schema = f"wreath_wf_{worker}"

    database = Database("test", _DSN, pools={"write": {"min_size": 1, "max_size": 2}})
    await database.start()
    try:
        connection = await database.acquire("write")
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            for statement in PostgresWorkflowStore.schema_sql(schema=schema).split(";\n"):
                if statement.strip():
                    await connection.execute(statement.strip())
        finally:
            await database.release("write", connection)

        store = PostgresWorkflowStore(database, schema=schema)
        runs: list[str] = []

        first_attempt = Workflow("payout")

        @first_attempt.step
        async def reserve(context) -> str:
            runs.append("reserve")
            return "hold-1"

        @first_attempt.step
        async def transfer(context) -> str:
            runs.append("transfer")
            raise RuntimeError("bank timed out")

        with pytest.raises(RuntimeError):
            await first_attempt.run(store=store, key="payout-1", compensate=False)

        # A *different* Workflow object, as a restarted worker would build.
        second_attempt = Workflow("payout")

        @second_attempt.step
        async def reserve(context) -> str:  # noqa: F811 - the restarted worker's copy
            runs.append("reserve")
            return "hold-1"

        @second_attempt.step
        async def transfer(context) -> str:  # noqa: F811
            runs.append("transfer")
            return f"sent against {context.results['reserve']}"

        outcome = await second_attempt.resume(store=store, key="payout-1")

        assert runs == ["reserve", "transfer", "transfer"], "reserve must not re-run"
        assert outcome.results["transfer"] == "sent against hold-1", (
            "the resumed step must read the *recorded* result, not a recomputed one"
        )
        assert outcome.state == "completed"

        status = await second_attempt.status(store=store, key="payout-1")
        assert status is not None and status.state == "completed"

        connection = await database.acquire("write")
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await database.release("write", connection)
    finally:
        await database.stop()


async def test_status_of_an_unknown_key_is_none() -> None:
    """`None`, not a fabricated running instance.

    A status endpoint asking about a key that was never started must be able to
    tell "never started" from "started and going", or it reports progress on work
    nobody enqueued.
    """
    flow = Workflow("unknown")

    @flow.step
    async def only(context) -> None:
        pass

    assert await flow.status(store=_memory_store(), key="never-started") is None


async def test_resume_of_an_unknown_key_refuses() -> None:
    flow = Workflow("unknown")

    @flow.step
    async def only(context) -> None:
        pass

    with pytest.raises(UnknownWorkflowInstance, match="never-started"):
        await flow.resume(store=_memory_store(), key="never-started")


@pytest.mark.database
@pytest.mark.skipif(not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)")
async def test_postgres_store_reports_an_unknown_key_as_absent() -> None:
    """The `if not rows` branch, which `wreath mutant` found nothing objected to.

    Removing that guard left every test green: no test had ever asked the Postgres
    store about a key it did not have. The failure it would have caused is not
    subtle -- `row = rows[0]` on an empty result -- but it would have surfaced as
    an IndexError inside the store rather than as `None` on the way out.
    """
    from wreath.postgres import Database
    from wreath.workflows import PostgresWorkflowStore

    worker = os.environ.get("PYTEST_XDIST_WORKER", "solo")
    schema = f"wreath_wf_absent_{worker}"
    database = Database("test", _DSN, pools={"write": {"min_size": 1, "max_size": 2}})
    await database.start()
    try:
        connection = await database.acquire("write")
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            for statement in PostgresWorkflowStore.schema_sql(schema=schema).split(";\n"):
                if statement.strip():
                    await connection.execute(statement.strip())
        finally:
            await database.release("write", connection)

        store = PostgresWorkflowStore(database, schema=schema)
        flow = Workflow("absent")

        @flow.step
        async def only(context) -> None:
            pass

        assert await store.load("never-started") is None
        assert await flow.status(store=store, key="never-started") is None
        with pytest.raises(UnknownWorkflowInstance):
            await flow.resume(store=store, key="never-started")

        connection = await database.acquire("write")
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await database.release("write", connection)
    finally:
        await database.stop()
