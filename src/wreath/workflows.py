"""Durable multi-step workflows: ordered steps, recorded progress, compensation.

`wreath.jobs` gives you one durable unit of work — claimed with `FOR UPDATE SKIP
LOCKED`, fenced, retried, dead-lettered. That is the right shape for "send this
email" and the wrong one for "reserve the stock, charge the card, book the
courier, and if the courier fails, refund the card and release the stock." The
second shape is a *saga*, and the thing that makes it hard is not the ordering —
it is that a worker can die between any two steps.

    checkout = Workflow("checkout")

    @checkout.step(compensate=release_hold)
    async def reserve_stock(context):
        return await inventory.hold(context.results["order_id"])

    @checkout.step(compensate=refund)
    async def charge_card(context):
        return await payments.charge(context.results["order_id"])

    await checkout.run(store=store, key=f"checkout:{order_id}")

Three properties are the whole point, and each one is a defect people ship
without it:

- **A completed step never runs twice.** Each step's return value is recorded
  before the next one starts, so `resume` re-enters at the first step with no
  record. A plain `for` loop over callables re-runs everything from the top, and
  every non-idempotent side effect happens again.
- **A failure undoes what succeeded, newest first.** Reverse order is not
  cosmetic: step 3's undo routinely depends on state step 2 established. The
  step that *raised* is not compensated — it did not complete, so there is
  nothing of its to undo, and running an undo against state that was never
  established is how a recoverable failure becomes a corrupt one.
- **A compensation that fails is counted, not swallowed.** Compensation runs
  when something has already gone wrong, which is exactly when
  `except Exception: pass` is most tempting and most damaging: it leaves a
  half-compensated saga with no signal. The remaining undos still run, and
  `Outcome.compensation_errors` says how many did not.

**One instance key runs once.** `key=` is the same guarantee
`JobRunner.enqueue(key=...)` gives — a second `run` for a key that already
finished returns the first outcome rather than re-executing it.

**Renaming a step of a live instance is refused.** Completion is recorded per step
*name*, so a rename would leave a record matching nothing and a resume would
silently redo work and report success. `resume` compares the recorded step list
against the definition and raises `WorkflowDefinitionChanged` naming the step it
cannot account for. That is the one failure in this module that would otherwise be
invisible.

Storage is a protocol. `InMemoryWorkflowStore` is for tests and single-process
work; `PostgresWorkflowStore` is durable, and it lives in its own system schema
with a `tenant` column for the reason `jobs` does — never relying on `search_path`
for isolation.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import _nplusone
from . import telemetry as _telemetry
from ._awaitable import is_awaitable
from ._recording_format import (
    COMPENSATION_FAILED,
    COMPENSATION_NONE,
    COMPENSATION_RAN,
)

#: The system schema durable workflow state lives in, matching `wreath.jobs`.
DEFAULT_SCHEMA = "wreath_system"
DEFAULT_TABLE = "workflow_steps"


def _captured_trace() -> str | None:
    """The traceparent to record on an instance, or None when untraced.

    The traceparent only, for the reason `wreath.jobs.JobRunner.enqueue` gives
    and which applies with more force here: `tracestate` is vendor routing for
    the *next hop of a live call*, and a saga that resumes an hour later is
    resuming a trace rather than continuing a conversation. Storing it would age
    in the instance row exactly as it would age in the queue.
    """
    bound = _telemetry.outbound_context.get()
    return bound[0] if bound else None


class WorkflowError(Exception):
    """Base class for every refusal this module raises."""


class WorkflowDefinitionChanged(WorkflowError):
    """A stored instance's steps no longer match the workflow's definition.

    Raised by `resume` rather than absorbed, because the alternative is a resume
    that re-runs completed work and reports success. The message names the step
    that could not be accounted for.
    """


class UnknownWorkflowInstance(WorkflowError):
    """`resume` was given a key that was never started."""


@dataclass(frozen=True, slots=True)
class Step:
    """One step: a name, the work, and optionally how to undo it."""

    name: str
    run: Callable[..., Any]
    compensate: Callable[..., Any] | None = None
    #: Queries to one model allowed inside this step (and its compensation)
    #: before that is a defect, or `None` to observe without a ceiling.
    query_budget: int | None = None


@dataclass(slots=True)
class StepContext:
    """What a step is handed. `results` carries every earlier step's return value.

    Threading results through the context rather than through arguments is what
    lets a resumed run reconstruct them from the store: a step that reads
    `context.results["make_id"]` gets the *recorded* value after a crash, not a
    recomputed one.
    """

    key: str
    workflow: str
    results: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Outcome:
    """What a run produced, whether it completed or compensated."""

    key: str
    workflow: str
    state: str
    results: dict[str, Any] = field(default_factory=dict)
    completed: list[str] = field(default_factory=list)
    compensated: list[str] = field(default_factory=list)
    #: Compensations that raised. Non-zero means the saga is *partly* undone, and
    #: the state it is in needs a human -- which is why this is a count on the
    #: outcome rather than a log line.
    compensation_errors: int = 0


class InMemoryWorkflowStore:
    """Step records in a dict. For tests and single-process work.

    Durable only for the life of the process, which is the honest limit: it
    satisfies the resume contract *within* a run but cannot survive the crash the
    contract exists for. Use `PostgresWorkflowStore` where that matters.
    """

    __slots__ = ("_instances",)

    def __init__(self) -> None:
        self._instances: dict[str, dict[str, Any]] = {}

    async def begin(self, key: str, workflow: str, steps: Sequence[str]) -> dict[str, Any]:
        """Record the instance and its step list, or return what is already there.

        Idempotent, because that is what makes `key=` an exactly-once *start*.
        The step list is recorded here, not derived at resume time, so a later
        rename is detectable at all.
        """
        record = self._instances.get(key)
        if record is None:
            record = {
                "workflow": workflow,
                "steps": list(steps),
                "results": {},
                "state": "running",
                "trace_context": _captured_trace(),
            }
            self._instances[key] = record
        return record

    async def load(self, key: str) -> dict[str, Any] | None:
        return self._instances.get(key)



    async def complete_step(self, key: str, name: str, result: Any) -> None:
        self._instances[key]["results"][name] = result

    async def uncomplete_step(self, key: str, name: str) -> None:
        self._instances[key]["results"].pop(name, None)

    async def finish(self, key: str, state: str, compensation_errors: int = 0) -> None:
        record = self._instances[key]
        record["state"] = state
        record["compensation_errors"] = compensation_errors


class PostgresWorkflowStore:
    """Durable step records in PostgreSQL.

    One row per (instance, step), plus one row per instance carrying the recorded
    step list. Both live in a dedicated system schema with a `tenant` column
    rather than relying on `search_path`, which is `wreath.jobs`'s rule and holds
    for the same reason: schema-qualified state cannot be read by the wrong
    tenant's worker because someone set a search path wrong.
    """

    __slots__ = ("_database", "_schema", "_table", "_tenant", "_trace_column", "_workload")

    def __init__(
        self,
        database: Any,
        *,
        schema: str = DEFAULT_SCHEMA,
        table: str = DEFAULT_TABLE,
        tenant: str = "",
        workload: str = "write",
    ) -> None:
        self._database = database
        self._schema = schema
        self._table = table
        self._tenant = tenant
        self._workload = workload
        #: Tri-state: None until probed, then whether the instances table has the
        #: `trace_context` column. A build newer than its schema must keep
        #: working -- losing the trace is a degradation, losing the saga is not.
        self._trace_column: bool | None = None

    async def _carries_trace(self) -> bool:
        """Whether this database's instances table has `trace_context`.

        Probed once and cached, off the execution path rather than inside it:
        `begin` runs per instance, and a catalog lookup per saga would be a query
        nobody asked for. The same shape `wreath.jobs` uses, and for the same
        reason its first draft had to be moved off `_claim`.
        """
        if self._trace_column is None:
            instances = f"{self._table}_instances"
            connection = await self._database.acquire(self._workload)
            try:
                self._trace_column = bool(
                    await connection.fetchval(
                        "SELECT true FROM pg_attribute a "
                        "JOIN pg_class k ON k.oid = a.attrelid "
                        "JOIN pg_namespace n ON n.oid = k.relnamespace "
                        # `::text` because `nspname` is `name`: without the cast
                        # PostgreSQL infers the parameter as `name` too, which
                        # the driver cannot encode. `wreath-sql-lint` SQL002.
                        "WHERE n.nspname = $1::text AND k.relname = $2::text "
                        "AND a.attname = 'trace_context' "
                        "AND a.attnum > 0 AND NOT a.attisdropped",
                        self._schema,
                        instances,
                    )
                )
            finally:
                await self._database.release(self._workload, connection)
        return self._trace_column

    @classmethod
    def schema_sql(
        cls, *, schema: str = DEFAULT_SCHEMA, table: str = DEFAULT_TABLE
    ) -> str:
        """DDL for the instance and step tables, semicolon-joined.

        `CREATE SCHEMA` is emitted; `CREATE EXTENSION` is not, and neither is any
        `search_path` assignment -- every relation here is schema-qualified at the
        point of use.
        """
        instances = f'"{schema}"."{table}_instances"'
        steps = f'"{schema}"."{table}"'
        return ";\n".join(
            (
                f'CREATE SCHEMA IF NOT EXISTS "{schema}"',
                f"CREATE TABLE IF NOT EXISTS {instances} (\n"
                "  key text NOT NULL,\n"
                "  tenant text NOT NULL DEFAULT '',\n"
                "  workflow text NOT NULL,\n"
                "  steps jsonb NOT NULL,\n"
                "  state text NOT NULL DEFAULT 'running',\n"
                "  compensation_errors int NOT NULL DEFAULT 0,\n"
                # The traceparent of the run that began the instance, so a
                # resume in another process continues one trace rather than
                # starting a second. Nullable: an instance begun untraced is
                # ordinary, and a table created by an older build has no such
                # column at all -- see `_carries_trace`.
                "  trace_context text,\n"
                "  created_at timestamptz NOT NULL DEFAULT now(),\n"
                "  updated_at timestamptz NOT NULL DEFAULT now(),\n"
                "  PRIMARY KEY (tenant, key)\n"
                ")",
                f"CREATE TABLE IF NOT EXISTS {steps} (\n"
                "  key text NOT NULL,\n"
                "  tenant text NOT NULL DEFAULT '',\n"
                "  name text NOT NULL,\n"
                "  result jsonb,\n"
                "  completed_at timestamptz NOT NULL DEFAULT now(),\n"
                "  PRIMARY KEY (tenant, key, name)\n"
                ")",
            )
        )

    async def _connection(self) -> Any:
        return await self._database.acquire(self._workload)

    async def begin(self, key: str, workflow: str, steps: Sequence[str]) -> dict[str, Any]:
        """Insert the instance if absent, then return its record.

        `ON CONFLICT DO NOTHING` rather than a check-then-insert: two workers
        starting one key race, and the unique index is the only arbiter that does
        not have a window between the read and the write.
        """
        from ._json import dumps as _dumps

        instances = f'"{self._schema}"."{self._table}_instances"'
        parent = _captured_trace()
        # No short-circuit on `parent is None`: the `load` two statements below
        # probes unconditionally (it must, to know whether to select the column),
        # so guarding here saved nothing and only gave the same question two
        # spellings. `wreath mutant` reported the guard as survivable, which is
        # what redundant code looks like from the outside.
        carries = await self._carries_trace()
        connection = await self._connection()
        try:
            if carries:
                await connection.execute(
                    f"INSERT INTO {instances} "
                    "(key, tenant, workflow, steps, trace_context) "
                    "VALUES ($1, $2, $3, $4::jsonb, $5) "
                    "ON CONFLICT (tenant, key) DO NOTHING",
                    key,
                    self._tenant,
                    workflow,
                    _dumps(list(steps)).decode("utf-8"),
                    parent,
                )
            else:
                await connection.execute(
                    f"INSERT INTO {instances} (key, tenant, workflow, steps) "
                    "VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (tenant, key) DO NOTHING",
                    key,
                    self._tenant,
                    workflow,
                    _dumps(list(steps)).decode("utf-8"),
                )
        finally:
            await self._database.release(self._workload, connection)
        record = await self.load(key)
        # mutant: allow guard.never-fires/guard.remove-raise -- no test reaches this
        # branch and none can without injecting a failure between the two
        # statements above. The `INSERT ... ON CONFLICT DO NOTHING` guarantees a row
        # exists by the time the `SELECT` runs, so `None` here means something
        # deleted the instance in the microseconds between them: a concurrent purge,
        # or a `DROP SCHEMA` mid-run. Kept as a real `raise` rather than an `assert`
        # (which `python -O` would strip) and rather than deleted, because the
        # alternative is `_execute` reading `record.get` off `None` and reporting an
        # AttributeError from a stack frame that says nothing about the cause.
        if record is None:  # pragma: no cover - unreachable without such an injection
            raise WorkflowError(
                f"workflow instance {key!r} vanished between its INSERT and its "
                "SELECT; something else deleted it or dropped the schema"
            )
        return record

    async def load(self, key: str) -> dict[str, Any] | None:
        from ._json import loads as _loads

        instances = f'"{self._schema}"."{self._table}_instances"'
        steps_table = f'"{self._schema}"."{self._table}"'
        # Selected only where the column exists, so a build newer than its schema
        # resumes the saga untraced instead of failing on an unknown column.
        trace = ", trace_context" if await self._carries_trace() else ""
        connection = await self._connection()
        try:
            rows = await connection.fetch(
                f"SELECT workflow, steps, state, compensation_errors{trace} "
                f"FROM {instances} WHERE tenant = $1 AND key = $2",
                self._tenant,
                key,
            )
            if not rows:
                return None
            row = rows[0]
            done = await connection.fetch(
                f"SELECT name, result FROM {steps_table} WHERE tenant = $1 AND key = $2",
                self._tenant,
                key,
            )
        finally:
            await self._database.release(self._workload, connection)
        # `jsonb` decodes to `str`, always -- both columns are written by
        # `_json.dumps` and read back as text, so there is no already-parsed case
        # to hedge against. This was an `isinstance(..., (bytes, str))` ternary
        # until `wreath mutant` reported the else-branch as dead: forcing the
        # decode kept every test green, which is what an unreachable alternative
        # looks like. A hedge over behaviour you can go and check is not defensive,
        # it is an unread branch that will be wrong whenever it is finally taken.
        return {
            "workflow": row["workflow"],
            "steps": _loads(row["steps"]),
            "results": {entry["name"]: _loads(entry["result"]) for entry in done},
            "state": row["state"],
            "compensation_errors": row["compensation_errors"],
            # Absent from the row when the column is not there; `_execute` reads
            # it with `.get`, so an older schema resumes untraced rather than
            # raising a KeyError from a stack frame that says nothing useful.
            "trace_context": row["trace_context"] if trace else None,
        }

    async def complete_step(self, key: str, name: str, result: Any) -> None:
        from ._json import dumps as _dumps

        steps_table = f'"{self._schema}"."{self._table}"'
        connection = await self._connection()
        try:
            await connection.execute(
                f"INSERT INTO {steps_table} (key, tenant, name, result) "
                "VALUES ($1, $2, $3, $4::jsonb) "
                "ON CONFLICT (tenant, key, name) DO UPDATE SET result = EXCLUDED.result",
                key,
                self._tenant,
                name,
                _dumps(result).decode("utf-8"),
            )
        finally:
            await self._database.release(self._workload, connection)

    async def uncomplete_step(self, key: str, name: str) -> None:
        steps_table = f'"{self._schema}"."{self._table}"'
        connection = await self._connection()
        try:
            await connection.execute(
                f"DELETE FROM {steps_table} WHERE tenant = $1 AND key = $2 AND name = $3",
                self._tenant,
                key,
                name,
            )
        finally:
            await self._database.release(self._workload, connection)

    async def finish(self, key: str, state: str, compensation_errors: int = 0) -> None:
        instances = f'"{self._schema}"."{self._table}_instances"'
        connection = await self._connection()
        try:
            await connection.execute(
                f"UPDATE {instances} SET state = $1, compensation_errors = $2, "
                "updated_at = now() WHERE tenant = $3 AND key = $4",
                state,
                compensation_errors,
                self._tenant,
                key,
            )
        finally:
            await self._database.release(self._workload, connection)


async def _call(handler: Callable[..., Any], context: StepContext) -> Any:
    """Invoke a step or a compensation, awaiting it only if it returns an awaitable.

    Both spellings are allowed because a compensation is frequently a one-liner --
    `lambda context: holds.release(...)` -- and forcing `async def` on it would be
    ceremony with no payoff.
    """
    result = handler(context)
    if is_awaitable(result):
        return await result
    return result


class Workflow:
    """An ordered series of steps, each optionally paired with its undo.

    Declaration order is execution order; there is no dependency graph, because a
    saga's undo chain is inherently a sequence and a graph would make "newest
    first" ambiguous.

    Args:
        name: Recorded with every instance, so a stored record says which
            workflow it belongs to.
    """

    __slots__ = ("_steps", "name")

    def __init__(self, name: str) -> None:
        self.name = name
        self._steps: list[Step] = []

    @property
    def steps(self) -> tuple[Step, ...]:
        """The declared steps, in order."""
        return tuple(self._steps)

    def step(
        self,
        handler: Callable[..., Any] | None = None,
        *,
        compensate: Callable[..., Any] | None = None,
        query_budget: int | None = None,
    ) -> Any:
        """Register a step. Usable bare or with `compensate=`.

        The step's name is the function's name, which is what a stored record
        keys on -- see `WorkflowDefinitionChanged` for what happens when it
        changes underneath a live instance.

        `query_budget` is how many times one model may be hydrated inside this
        step before that is a defect; crossing it raises from the query that
        did. It covers the step's compensation too, which is the half nobody
        exercises -- an undo runs only when something has already gone wrong,
        so an N+1 in one is discovered during an incident or not at all.
        Omitted, the step is observed rather than bounded; see
        `wreath.jobs.JobRunner.task` for why that is the default.
        """
        _nplusone.check_budget(query_budget, f"a step of workflow {self.name!r}")

        def register(function: Callable[..., Any]) -> Callable[..., Any]:
            # `getattr` rather than `function.__name__`: a `functools.partial`, a
            # callable instance, or a lambda bound to a name has no `__name__`, and
            # the step name is what durable records key on -- so a nameless step
            # would be unresumable. Refuse it here, where the traceback still points
            # at the declaration, rather than at a resume weeks later.
            name = getattr(function, "__name__", None)
            if not isinstance(name, str) or not name or name == "<lambda>":
                raise WorkflowError(
                    f"a step of workflow {self.name!r} has no usable __name__ "
                    f"({function!r}). Step names key the durable record of what has "
                    "completed, so a step must be a named function -- wrap a partial "
                    "or a callable object in `async def`."
                )
            if any(existing.name == name for existing in self._steps):
                raise WorkflowError(
                    f"workflow {self.name!r} already has a step named {name!r}; "
                    "step names key durable records, so two of them would share one row"
                )
            self._steps.append(Step(name, function, compensate, query_budget))
            return function

        if handler is not None:
            return register(handler)
        return register

    async def run(
        self,
        *,
        store: Any,
        key: str | None = None,
        compensate: bool = True,
        progress: Any = None,
        recorder: Any = None,
    ) -> Outcome:
        """Start (or rejoin) an instance and execute it to completion.

        A `key` that already finished returns the recorded outcome without
        executing anything -- exactly-once *start*. Omitting `key` generates one,
        which is the right choice only when nothing will ever need to resume it.

        Args:
            store: Anything satisfying the store protocol.
            key: The instance identity. Generated when omitted.
            compensate: Run the undo chain when a step raises. `False` leaves the
                completed steps recorded so `resume` can carry on from them --
                which is what you want for a *retryable* failure, as opposed to
                one that invalidates the whole saga.
            progress: A `wreath.progress.ProgressRegistry` to report step
                completion to.
            recorder: A `wreath.recording.WorkflowStepRecorder`, or None. It
                arms nothing on its own -- a policy with no triggers records
                nothing -- and it is passed per run rather than held on the
                workflow because arming is an operator's decision about a
                deployment, not a property of the saga's definition.

        Raises:
            Exception: whatever the failing step raised, re-raised after the undo
                chain has run.
        """
        instance_key = key if key is not None else f"{self.name}:{uuid.uuid4().hex}"
        record = await store.begin(instance_key, self.name, [s.name for s in self._steps])
        if record.get("state") == "completed":
            return Outcome(
                key=instance_key,
                workflow=self.name,
                state="completed",
                results=dict(record.get("results") or {}),
                completed=list(record.get("results") or {}),
            )
        return await self._execute(
            store=store,
            key=instance_key,
            record=record,
            compensate=compensate,
            progress=progress,
            recorder=recorder,
        )

    async def status(self, *, store: Any, key: str) -> Outcome | None:
        """What the store knows about an instance, or `None` if it never started.

        The way `compensation_errors` is read. A run that compensated re-raises the
        step's own exception -- callers expect the error they would have got
        without a saga around it -- so the summary cannot come back as a return
        value. It is recorded instead, which is the more useful place anyway: a
        half-compensated saga has to be findable *later*, by someone who was not
        holding the traceback.
        """
        record = await store.load(key)
        if record is None:
            return None
        results = dict(record.get("results") or {})
        return Outcome(
            key=key,
            workflow=record.get("workflow") or self.name,
            state=record.get("state") or "running",
            results=results,
            completed=list(results),
            compensation_errors=int(record.get("compensation_errors") or 0),
        )

    async def resume(
        self, *, store: Any, key: str, progress: Any = None, recorder: Any = None
    ) -> Outcome:
        """Carry a started instance on from its first unrecorded step.

        Raises:
            UnknownWorkflowInstance: `key` was never started.
            WorkflowDefinitionChanged: the stored step list names a step this
                workflow no longer declares.
        """
        record = await store.load(key)
        if record is None:
            raise UnknownWorkflowInstance(
                f"no workflow instance {key!r}; `run` starts one, `resume` continues it"
            )
        declared = {step.name for step in self._steps}
        for stored in record.get("steps") or ():
            if stored not in declared:
                raise WorkflowDefinitionChanged(
                    f"instance {key!r} of workflow {self.name!r} recorded a step "
                    f"{stored!r} that the workflow no longer declares. Resuming would "
                    "re-run completed work and report success. Finish or discard the "
                    "instance, or restore the step name."
                )
        if record.get("state") == "completed":
            return Outcome(
                key=key,
                workflow=self.name,
                state="completed",
                results=dict(record.get("results") or {}),
                completed=list(record.get("results") or {}),
            )
        return await self._execute(
            store=store, key=key, record=record, compensate=True, progress=progress,
            recorder=recorder,
        )

    async def _execute(
        self, *, store: Any, key: str, record: dict[str, Any], compensate: bool,
        progress: Any, recorder: Any = None,
    ) -> Outcome:
        """Bind the instance's trace, then execute under it.

        The binding wraps the whole of `_execute_bound`, so the undo chain runs
        under it too -- the plan's requirement that a compensation is *visibly
        part of* the saga rather than an orphan beside it.

        The instance's context wins over whatever the resuming worker holds, and
        that is the point: `run` and `resume` are the same trace even when they
        are different processes on different days. An instance begun untraced
        binds `None` rather than leaving the ambient value in place, for the same
        reason the request pipeline and the job runner both bind `None` -- not
        binding leaks the previous occupant's context, and a trace that names the
        wrong cause is worse than one that names none.
        """
        parent = record.get("trace_context")
        token = _telemetry.outbound_context.set((parent, "") if parent else None)
        try:
            return await self._execute_bound(
                store=store,
                key=key,
                record=record,
                compensate=compensate,
                progress=progress,
                recorder=recorder,
                trace_context=parent or "",
            )
        finally:
            _telemetry.outbound_context.reset(token)

    async def _execute_bound(
        self, *, store: Any, key: str, record: dict[str, Any], compensate: bool,
        progress: Any, recorder: Any = None, trace_context: str = "",
    ) -> Outcome:
        results: dict[str, Any] = dict(record.get("results") or {})
        context = StepContext(key=key, workflow=self.name, results=results)
        reporter = progress.reporter(key) if progress is not None else None
        # Steps already recorded are *completed*, not skipped: they belong on the
        # undo chain, because a failure later still has to unwind them.
        completed = [step for step in self._steps if step.name in results]
        total = len(self._steps) or 1

        for index, step in enumerate(self._steps):
            if step.name in results:
                continue
            witness = _StepWitness(
                self, recorder, key, step, index, completed, trace_context
            )
            try:
                with witness.observing(), _nplusone.scope_for(
                    _nplusone.Origin(kind="step", label=step.name, identifier=key),
                    budget=step.query_budget,
                ):
                    results[step.name] = await _call(step.run, context)
            except BaseException as error:
                undone: list[tuple[str, str]] = []
                if compensate:
                    _names, failures, undone = await self._compensate(
                        store, key, completed, context
                    )
                    await store.finish(key, "compensated", failures)
                else:
                    await store.finish(key, "failed", 0)
                # Written **after** the undo chain, deliberately. The record's
                # reason to exist is that a saga failure mid-way is the least
                # reproducible state in the framework, and "which compensations
                # ran" is most of what makes it so; a record written at the
                # moment of the raise could not carry any of them.
                witness.write(error, undone)
                if reporter is not None:
                    reporter.fail(error, f"step {step.name!r} failed")
                raise
            witness.write(None, ())
            await store.complete_step(key, step.name, results[step.name])
            completed.append(step)
            if reporter is not None:
                reporter.update((index + 1) / total * 100.0, f"step {step.name!r} done")

        await store.finish(key, "completed")
        if reporter is not None:
            reporter.done(f"workflow {self.name!r} complete")
        return Outcome(
            key=key,
            workflow=self.name,
            state="completed",
            results=results,
            completed=[step.name for step in completed],
        )

    async def _compensate(
        self, store: Any, key: str, completed: list[Step], context: StepContext
    ) -> tuple[list[str], int, list[tuple[str, str]]]:
        """Undo the completed steps, newest first, counting every undo that fails.

        The third return value is the chain as a recording reads it --
        `(step, "ran" | "failed" | "none")` newest-first, including the steps
        that declared no compensation at all. "Nothing to undo" and "the undo
        was never reached" are different states of the world and a saga that
        stopped mid-way is exactly where telling them apart matters.
        """
        undone: list[str] = []
        chain: list[tuple[str, str]] = []
        failures = 0
        for step in reversed(completed):
            if step.compensate is None:
                chain.append((step.name, COMPENSATION_NONE))
                await store.uncomplete_step(key, step.name)
                continue
            try:
                with _nplusone.scope_for(
                    _nplusone.Origin(
                        kind="step", label=f"{step.name}:compensate", identifier=key
                    ),
                    budget=step.query_budget,
                ):
                    await _call(step.compensate, context)
            except Exception:  # noqa: BLE001
                # Broad by necessity and counted, per AGENTS.md's third tier: a
                # compensation is arbitrary user code, and the undo chain behind a
                # failing link must still run or the saga is left *more* broken
                # than if nothing had been undone. `Exception` rather than
                # `BaseException`, so a cancellation still propagates and stops
                # the chain -- an operator cancelling a compensation means it.
                failures += 1
                chain.append((step.name, COMPENSATION_FAILED))
                continue
            undone.append(step.name)
            chain.append((step.name, COMPENSATION_RAN))
            await store.uncomplete_step(key, step.name)
        return undone, failures, chain


class _StepWitness:
    """Watches one step for a recorder, and writes it down if the policy arms.

    Kept out of `_execute_bound` because that function is the saga, and a run
    with no recorder must read as though this did not exist -- `observing()` is
    a null context and `write` returns immediately, so an unarmed saga pays one
    attribute test per step and nothing else.

    Nothing here can raise into the saga. The recorder's own `write` never does,
    by contract, and the two things this adds -- deciding the outcome and
    building the record -- are arithmetic over values the caller already holds.
    """

    __slots__ = (
        "_completed", "_key", "_position", "_recorder", "_step", "_trace",
        "_workflow", "trace",
    )

    def __init__(
        self,
        workflow: Workflow,
        recorder: Any,
        key: str,
        step: Step,
        position: int,
        completed: list[Step],
        trace_context: str,
    ) -> None:
        self._workflow = workflow
        self._recorder = recorder
        self._key = key
        self._step = step
        self._position = position
        # The step *before this one in execution order*, which is the cause a
        # reader follows -- not the previous declaration, because a resumed
        # instance may have completed steps this process never ran.
        self._completed = list(completed)
        self._trace = trace_context
        self.trace: Any = None

    def observing(self) -> Any:
        """Watch this step's boundaries, when something is recording it."""
        if self._recorder is None:
            import contextlib

            return contextlib.nullcontext()
        from ._replay_adapters import observed_boundaries

        self.trace = self._recorder.trace()
        return observed_boundaries(self._recorder.scope, self.trace)

    def write(self, error: BaseException | None, chain: Sequence[tuple[str, str]]) -> None:
        """Ask the policy about this step, and write it if the answer is yes."""
        recorder = self._recorder
        if recorder is None:
            return
        from ._recording_format import WorkflowStepRecord

        outcome = self._outcome(error, chain)
        if not recorder.captures(
            workflow=self._workflow.name,
            step=self._step.name,
            outcome=outcome,
            instance=self._key,
        ):
            return
        # Never None here: `write` has already returned when there is no
        # recorder, and `observing()` -- which `_execute_bound` always enters
        # around the step -- makes a trace whenever there is one. The `trace is
        # None` arm that used to be here was unreachable, and a mutation run
        # found it by taking both of its branches with nothing to tell them
        # apart.
        trace = self.trace
        recorder.write(
            WorkflowStepRecord(
                instance=self._key,
                workflow=self._workflow.name,
                step=self._step.name,
                position=self._position,
                after=self._completed[-1].name if self._completed else "",
                tenant="",
                trace_context=self._trace,
                boundaries=trace.events,
                outcome=str(outcome),
                error_type="" if error is None else type(error).__name__,
                error_message="" if error is None else str(error),
                completed_before=len(self._completed),
                compensations=tuple(chain),
            ),
            trace,
        )

    def _outcome(
        self, error: BaseException | None, chain: Sequence[tuple[str, str]]
    ) -> Any:
        """Which of the three outcomes this step reached.

        `compensation_failed` outranks `raised` when both are true, and that
        ordering is the whole point of having two names: a saga whose undo chain
        also broke is in a state a retry cannot reach, and a record that reported
        only the original failure would send a reader to the wrong problem.
        """
        from ._recording_format import COMPENSATION_FAILED, WorkflowStepOutcome

        if error is None:
            return WorkflowStepOutcome.COMPLETED
        if any(state == COMPENSATION_FAILED for _name, state in chain):
            return WorkflowStepOutcome.COMPENSATION_FAILED
        return WorkflowStepOutcome.RAISED


__all__ = [
    "DEFAULT_SCHEMA",
    "DEFAULT_TABLE",
    "InMemoryWorkflowStore",
    "Outcome",
    "PostgresWorkflowStore",
    "Step",
    "StepContext",
    "UnknownWorkflowInstance",
    "Workflow",
    "WorkflowDefinitionChanged",
    "WorkflowError",
]
