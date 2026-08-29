"""Durable, at-least-once background jobs backed by PostgreSQL.

A replacement for Celery/arq for teams already running Postgres: enqueue jobs
(transactionally, in the same commit as your business writes), and a supervised
pool of workers claims them with `FOR UPDATE SKIP LOCKED` + fencing tokens,
retries with backoff, and dead-letters on exhaustion. `NOTIFY` is used only as
a latency doorbell — correctness never depends on a notification arriving, the
workers also poll.

**Delivery is at-least-once.** A crash between a job's side effect and its
completion `UPDATE` yields a re-run on lease expiry. Make handlers idempotent;
pass `key=` to `JobRunner.enqueue` for exactly-once *enqueue* (a unique
index drops duplicates) and use it to guard non-idempotent side effects.

Multi-tenancy note (design 01 §5): jobs live in one dedicated system schema with
a `tenant` column, never relying on `search_path` for isolation — a
database-global `NOTIFY` name would otherwise wake the wrong tenant's workers.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import inspect
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import _nplusone
from . import telemetry as _telemetry

# Re-exported under this module's historic names: the supervision moved to
# `_doorbell`, the names callers and tests already reach for did not.
from ._doorbell import BACKOFF_BASE as DOORBELL_BACKOFF_BASE  # noqa: F401
from ._doorbell import BACKOFF_CAP as DOORBELL_BACKOFF_CAP  # noqa: F401
from ._doorbell import Doorbell
from ._doorbell import delay as _doorbell_delay  # noqa: F401
from ._doorbell import sleep_or_stop as _sleep_or_stop
from ._jobcore import compute_backoff, dedup_key, validate_identifier
from ._leased import claim_sql, fenced_update_sql
from ._nplusone import NPlusOneDetected as _NPlusOneDetected
from ._pgcatalog import column_exists
from .postgres import PostgresError
from .temporal import Duration, Instant, Recurrence

JobHandler = Callable[..., Awaitable[None]]


# The bounded SQL-safe identifier rule lives in `_jobcore` so jobs and
# messaging share one definition; kept as a module-local alias for readability.
_validate_identifier = validate_identifier

#: Reconnect backoff for the NOTIFY doorbell, re-exported from
#: `wreath._doorbell`, which the bus shares. Matching `messaging.MessageBus`
#: is no longer a thing to keep true by hand: both hold the same supervisor.


class JobVanished(RuntimeError):
    """A deduplicated `JobRunner.launch` found no row to hand back.

    The insert conflicted -- so the work *was* already enqueued -- but the
    surviving row was gone by the time it was read, which is what a retention
    sweep over completed jobs looks like from here. There is no id to watch, and
    inventing one is worse than saying so: a task id of `"None"` 404s on the
    status endpoint and streams forever on the SSE one, and the client cannot
    tell that from a job that failed. Re-launch (nothing is holding the key any
    more) or treat the earlier run as finished.
    """


def _channel(schema: str, queue: str) -> str:
    """The LISTEN/NOTIFY channel shared by producers and the runner.

    Refused rather than truncated when it is too long: PostgreSQL truncates a
    channel name silently, so two queues whose names agree in their first 63
    bytes would share one doorbell and wake each other's workers -- which
    presents as latency, not as an error.
    """
    return validate_identifier(f"wj_{schema}_{queue}", "doorbell channel")


@dataclass(frozen=True, slots=True)
class JobContext:
    """Handed to a job handler as its first argument."""

    job_id: int
    task: str
    attempt: int
    fence: int
    tenant: str
    key: str | None
    #: The runner's `ProgressRegistry`, or None.
    progress: Any = None
    #: The `traceparent` of the request that enqueued this job, or None when it
    #: was enqueued outside a traced request. Exposed so a handler can log the
    #: cause; the runner has already rebound it, so an outbound call the handler
    #: makes joins the same trace without the handler doing anything.
    trace_context: str | None = None

    @property
    def task_id(self) -> str:
        """This job's progress key. The job id, so there is one identifier."""
        return str(self.job_id)

    def report(self, percent: float, message: str = "") -> None:
        """Tell whoever is watching how far along this job is.

        A no-op when the runner has no progress registry, so a handler can
        report unconditionally. Only *progress* -- the runner sets `done` and
        `failed` itself, because it is the thing that actually knows whether
        the job finished, is about to retry, or was dead-lettered.
        """
        if self.progress is not None:
            self.progress.report(self.task_id, percent, message)


@dataclass(frozen=True, slots=True)
class TaskHandle:
    """What a caller gets back from `JobRunner.launch`.

    Small on purpose: an id to watch and the state at hand-back time. Everything
    after that comes from the progress stream.
    """

    task_id: str
    state: str = "queued"

    def as_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "state": self.state}


@dataclass(frozen=True, slots=True)
class _Task:
    name: str
    func: JobHandler
    #: Captured once at registration, so binding a job's args costs no
    #: introspection per run. See `_run`: a row enqueued by an older release
    #: can carry an arity this signature no longer accepts.
    signature: inspect.Signature
    max_attempts: int
    backoff_kind: str
    backoff_base: float
    backoff_factor: float
    backoff_cap: float
    backoff_jitter: float
    #: Seconds this handler may run before it is cancelled, or `None` to take
    #: the runner's default (a fraction of the lease). See `JobRunner.task`.
    timeout: float | None = None
    #: How many times one model may be hydrated in a single attempt before that
    #: is treated as a defect, or `None` to observe without a ceiling. See
    #: `JobRunner.task`.
    query_budget: int | None = None


@dataclass(frozen=True, slots=True)
class _Schedule:
    task: str
    recurrence: Recurrence
    args: tuple[Any, ...]
    tenant: str
    misfire: str  # "skip" only, for this cut


@dataclass(slots=True)
class _Claimed:
    id: int
    task: str
    args: list[Any]
    tenant: str
    attempts: int
    max_attempts: int
    fence: int
    key: str | None
    #: The `traceparent` of the request that enqueued this job, or None when it
    #: was enqueued outside a traced request or against a version-1 schema.
    trace_context: str | None = None


class JobRunner:
    """A named queue of durable jobs on one application database.

    Obtain via `wreath.Wreath.jobs`. Register handlers with
    `task`, enqueue with `enqueue`, and schedule recurring work with
    `schedule`. The runner is a supervised service — its workers, sweeper,
    and scheduler run for the process lifetime.
    """

    def __init__(
        self,
        database: Any,
        *,
        name: str,
        workload: str = "write",
        concurrency: int = 8,
        lease: Any = 30.0,
        poll_interval: Any = 5.0,
        schema: str = "wreath",
        batch: int = 1,
        progress: Any = None,
        attempts: Any = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        # A bare number is seconds, which is what these parameters have always
        # meant; `Duration` is the spelling that says so.
        lease = Duration.of(lease).total_seconds()
        poll_interval = Duration.of(poll_interval).total_seconds()
        if lease <= 0 or poll_interval <= 0:
            raise ValueError("lease and poll_interval must be positive")
        if batch < 1:
            raise ValueError("batch must be >= 1")
        self._db = database
        self._name = _validate_identifier(name, "job runner name")
        self._schema = _validate_identifier(schema, "schema")
        self._workload = workload
        self._concurrency = concurrency
        self._lease = lease
        self._poll = poll_interval
        self._batch = batch
        self._progress = progress
        #: A `wreath.recording.AttemptRecorder`, or None. Deny-by-default is
        #: two layers deep: no recorder records nothing, and a recorder whose
        #: policy has no triggers records nothing either.
        self._attempts = attempts
        self._tasks: dict[str, _Task] = {}
        self._schedules: list[_Schedule] = []
        self._passes: list[tuple[str, Any]] = []
        self._table = f'"{self._schema}".jobs'
        self._channel = _channel(self._schema, self._name)
        # The queue is a propagation seam: work enqueued here runs in a later
        # process and belongs to the trace of the request that caused it. Arming
        # the latch here is what makes the request pipeline bind a context for an
        # application that has jobs but no outbound HTTP client -- otherwise the
        # binding is skipped and every job row is enqueued without a cause.
        _telemetry.propagates()
        #: Whether this database's `jobs` table has the version-2 column. `None`
        #: until the first enqueue asks; see `_carries_trace`.
        #: Column-presence answers, resolved once and shared by the enqueue
        #: and claim paths.
        self._columns: dict[str, bool] = {}
        # Runtime (set at start()):
        self._supervisor: Any = None
        # One waiter per worker, not one shared Event. A shared Event is
        # cleared by whichever worker parks next, so a `set()` from the doorbell
        # could be consumed by a worker that was about to claim anyway while
        # another worker -- already waiting -- slept the full poll interval with
        # work sitting there. Each worker clears its own waiter immediately
        # before claiming, so a wake that lands mid-claim is still remembered.
        self._waiters: list[asyncio.Event] = []
        self._doorbell = Doorbell(
            database=database,
            workload=workload,
            pump=self._pump,
            channels=(self._channel,),
        )
        self._inflight: set[asyncio.Future[Any]] = set()
        #: Jobs this process claimed and has not started running. A batch claim
        #: leases several at once, and a shutdown between the claim and the run
        #: used to leave them leased until the lease expired -- so a rolling
        #: deploy parked `batch - 1` jobs for `lease` seconds per restart, which
        #: reads as a queue that stalls whenever you deploy.
        self._claimed_not_started: list[_Claimed] = []
        #: This process's identity on a claimed row. It was the *queue* name,
        #: so every worker on a queue shared one and `owner` answered "which
        #: queue" -- a question the `queue` column already answers. Nothing read
        #: it back, which is why that went unnoticed.
        #:
        #: Correctness never depended on it and still does not: the fence is
        #: what stops a superseded worker's bookkeeping landing. What this buys
        #: is the diagnosis -- during an incident, `SELECT owner FROM ... WHERE
        #: state = 'leased'` now names the process holding each job.
        #:
        #: Generated rather than derived from a host name or pid, both of which
        #: repeat across containers and restarts. Prefixed with the queue so a
        #: human reading a row still sees which queue it belongs to.
        self._worker_id = f"{self._name}:{uuid.uuid4().hex[:12]}"
        #: Sweeps that raised. The sweeper suppresses everything so a transient
        #: error cannot end the loop, which also meant a sweeper that had never
        #: once succeeded -- a missing table, a revoked grant -- was
        #: indistinguishable from one with nothing to reclaim.
        self.sweep_errors = 0
        #: Scheduler ticks that raised, for the same reason.
        self.schedule_errors = 0
        #: Times the startup probe for the version-2 `trace_context` column
        #: could not reach the database. Non-zero means this runner may be
        #: enqueuing without trace context that the schema could hold -- an
        #: observability gap, never a correctness one.
        self.trace_probe_errors = 0
        #: Jobs stopped by `cancel`. Kept apart from `dead_lettered` because the
        #: cause is opposite: a dead-lettered job ran out of attempts, and a
        #: cancelled one was told to stop while it still had some. A queue whose
        #: `dead` rows are mostly cancellations is healthy; one whose `dead` rows
        #: are mostly exhaustions is not, and one counter cannot say which.
        self.cancelled = 0
        #: Jobs that exhausted their attempts. The one outcome nothing else
        #: reports: a dead-lettered job is silent in the logs and invisible in
        #: the queue depth, because it has left the queue.
        self.dead_lettered = 0
        #: Failures *after* a job was claimed -- recording the outcome, not
        #: running the handler. Non-zero means jobs are being re-run on lease
        #: expiry rather than completing, which used to end a worker silently.
        self.run_errors = 0
        #: Handlers cancelled for outrunning their deadline. Distinct from
        #: `run_errors`: the handler did not fail, it was stopped, and the number
        #: that matters is how often the deadline is the thing deciding.
        self.run_timeouts = 0
        #: Passes whose shift could not be enqueued. A pass that is never driven
        #: does nothing at all, and the ledger cannot tell that apart from a
        #: pass with no work to do.
        self.pass_drive_errors = 0
        #: Attempts failed for crossing a declared `query_budget`. Counted
        #: separately from `run_errors` because it is the one failure whose
        #: cause is a defect in the handler rather than in what it was calling.
        self.query_budget_exceeded = 0
        #: N+1 findings observed in attempts that declared no budget. These did
        #: not fail anything; the number is how often something was worth
        #: looking at. A finding nobody counts is the blind spot in a new place.
        self.nplusone_findings = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def _listen_conn(self) -> Any:
        """The doorbell's held connection. Delegated rather than stored, so
        there is one owner of the connection's lifetime and not two."""
        return self._doorbell.connection

    @property
    def doorbell_reconnects(self) -> int:
        """Doorbell connections lost, plus every failed attempt to open one
        (including the attempt made on the startup path). Correctness never
        depended on the doorbell, which is exactly why losing it is invisible
        without a number: the runner keeps working at `poll_interval`.

        There is deliberately no companion `handler_errors` here. The bus keeps
        one because its pump dispatches to subscriber callbacks; this pump only
        sets an event, so a counter for user-code failures could never be
        anything but zero and would imply a distinction the runner cannot make.
        """
        return self._doorbell.reconnects

    def stats(self) -> dict[str, int]:
        """Every counter this runner keeps, by name. See `MessageBus.stats`."""
        return {
            "run_errors": self.run_errors,
            "run_timeouts": self.run_timeouts,
            "sweep_errors": self.sweep_errors,
            "schedule_errors": self.schedule_errors,
            "dead_lettered": self.dead_lettered,
            "cancelled": self.cancelled,
        }

    def counters(self) -> Any:
        """This runner's counters, for `wreath.metrics.collect`."""
        from .metrics import Counters

        return Counters(subsystem="jobs", instance=self._name, values=self.stats())

    @property
    def progress(self) -> Any:
        """The registry this runner reports to, for the status/stream endpoints."""
        return self._progress

    def task(
        self,
        name: str,
        *,
        retries: int = 5,
        backoff: str = "exp",
        backoff_base: float = 1.0,
        backoff_factor: float = 2.0,
        backoff_cap: float = 3600.0,
        backoff_jitter: float = 0.2,
        timeout: float | None = None,
        query_budget: int | None = None,
    ) -> Callable[[JobHandler], JobHandler]:
        """Decorator registering an async `handler(ctx, *args)` under `name`.

        `timeout` is how long the handler may run before it is cancelled and the
        attempt fails; `None` takes the runner's default, `DEADLINE_FRACTION` of
        the lease. **It must end inside the lease**, and that is checked here.

        `query_budget` is how many times one model may be hydrated within a
        single attempt before that is a defect. Declaring it makes the attempt
        *fail* at the query that crossed the line, with a traceback naming the
        loop -- which is the only way to find an N+1 in work nobody is watching.
        Leave it unset and the attempt is merely observed: counted, and reported
        if a guard exists in this process, but never failed. That default is
        deliberate. A guard that raises inside a durable job converts a slow job
        into a failed one and then into a retry storm, and wreath does not know
        whether five hundred queries was the defect or the point of the task.

        There is no heartbeat, so a handler still running when its lease expires
        is reclaimed by the sweeper and started again by another worker — two
        copies of the same job, doing the same work, and the fence only stops
        the loser's *bookkeeping* from landing. That was the whole cost of an
        unbounded handler: not a stuck worker, a second charge on the card.
        `drive()` has always refused a pass whose shift could outlast the lease
        for this reason; an ordinary task simply had no bound to check.
        """
        _validate_identifier(name, "task name")
        if name in self._tasks:
            raise ValueError(f"duplicate task: {name!r}")
        if retries < 0:
            raise ValueError("retries cannot be negative")
        if timeout is not None:
            if timeout <= 0:
                raise ValueError("timeout must be positive")
            if timeout >= self._lease:
                raise ValueError(
                    f"task {name!r} declares timeout={timeout:g}s but runner "
                    f"{self._name!r} leases jobs for {self._lease:g}s. A handler "
                    "still running when its lease expires is reclaimed and run "
                    "again by another worker, so a handler has to finish -- or be "
                    "cancelled -- before then."
                )
        _nplusone.check_budget(query_budget, f"task {name!r}")

        def register(func: JobHandler) -> JobHandler:
            self._tasks[name] = _Task(
                name=name,
                func=func,
                signature=inspect.signature(func),
                max_attempts=retries + 1,
                backoff_kind=backoff,
                backoff_base=backoff_base,
                backoff_factor=backoff_factor,
                backoff_cap=backoff_cap,
                backoff_jitter=backoff_jitter,
                timeout=timeout,
                query_budget=query_budget,
            )
            return func

        return register

    def schedule(
        self,
        task: str,
        *,
        cron: str | None = None,
        recurrence: Recurrence | None = None,
        args: tuple[Any, ...] = (),
        tenant: str = "",
        misfire: str = "skip",
    ) -> None:
        """Enqueue `task` on a repeating schedule. Idempotent across instances.

        Every app instance runs the scheduler, but each firing's enqueue carries
        a deterministic `key` so the unique index makes exactly one row win — no
        leader election needed.

        `cron=` is a five-field expression **read in UTC**, which is what it has
        always meant. `recurrence=` takes a `wreath.temporal.Recurrence`, which
        carries its own zone:

        ```python
        runner.schedule("rebalance", recurrence=Recurrence.cron("0 3 * * *", tz=depot_tz))
        ```

        Pass one or the other. A zone is the difference between "03:00" and
        "03:00 for half the year and 04:00 for the other half", and a depot
        operator reading a schedule means the former.
        """
        if misfire != "skip":
            raise ValueError("only misfire='skip' is supported in this cut")
        _both = "schedule() takes exactly one of cron= (UTC) or recurrence= (which "
        if cron is not None:
            if recurrence is not None:
                raise ValueError(_both + "carries its own zone); both were given")
            # `cron=` predates zones and is documented as UTC, so it stays UTC.
            # Silently reading it on the process's local zone would move every
            # existing schedule on the first deploy after this landed.
            resolved = Recurrence.cron(cron)
        elif recurrence is not None:
            resolved = recurrence
        else:
            raise ValueError(_both + "carries its own zone); neither was given")
        self._schedules.append(
            _Schedule(
                task=task, recurrence=resolved, args=tuple(args), tenant=tenant, misfire=misfire
            )
        )

    def drive(self, walk: Any, *, cron: str | None = None) -> str:
        """Run a `ChunkedPass` on this queue. Returns its task name.

        One registration path rather than a registry the runner has to reconcile
        at startup: this registers the task that runs a shift, arranges for the
        ledger row to be seeded on first contact with the database, adds the
        schedule when one is given, and enqueues the first shift when the runner
        starts. The dependency is visible at the call site because the job runner
        is literally the thing that drives the walk:

        ```python
        jobs.drive(normalize_grades)
        jobs.drive(purge_replays, cron="*/5 * * * *")
        ```
        **A shift must be shorter than the lease**, and this is where that is
        checked. There is no heartbeat: a handler still running when its lease
        expires is reclaimed by the sweeper, picked up by a second worker, and
        re-claimed again when the first one's fenced completion matches nothing.
        A pass survives that -- the cursor's compare-and-swap serialises the
        duplicates -- but it must not *rely* on surviving something it can simply
        not do.

        A recurring pass needs `cron=`: nothing else would start its next
        cycle, and a pass that quietly stops after one cycle is worse than one
        that refuses to be declared.
        """
        shift = float(walk.shift)
        if shift >= self._lease:
            raise ValueError(
                f"pass {walk.name!r} has shift={shift:g}s but runner {self._name!r} "
                f"leases jobs for {self._lease:g}s. A handler still running when "
                "its lease expires is reclaimed while it runs, so a shift must "
                "end and re-enqueue before then; the chain is "
                "statement_timeout < within < shift < lease < command_timeout."
            )
        if walk.recurring and cron is None:
            raise ValueError(
                f"pass {walk.name!r} has a re-derived frontier, so it runs in "
                "cycles and nothing would start the next one. Give it a "
                'schedule: jobs.drive(pass, cron="*/5 * * * *").'
            )
        task = _pass_task_name(walk.name, walk.tenant)
        if task in self._tasks:
            raise ValueError(f"pass {walk.name!r} is already driven by this runner")

        async def run_shift(ctx: JobContext) -> None:
            stopping = self._supervisor.stopping if self._supervisor is not None else None
            # Stamped before the work, so a shift that dies mid-chunk still
            # leaves evidence that something was driving this pass -- otherwise
            # a crashing shift and an absent scheduler look identical.
            await self._record_drive_failure(walk, None)
            result = await walk.run_shift(self._db, stopping=stopping)
            if result.error is not None and result.stopped == "failed":
                raise RuntimeError(f"pass {walk.name!r} chunk failed: {result.error}")
            ctx.report(0.0, f"pass {walk.name}: {result.rows} rows in {result.chunks} chunks")
            if result.stopped == "blocked":
                # Re-enqueuing a blocked pass would turn `halt` back into a
                # retry loop, which is the silent skip halting exists to refuse.
                return
            if result.should_continue or (result.chunks and not result.complete):
                await self._enqueue_next_shift(task, walk)

        # Retries are the runner's ordinary backoff. A shift is safe to re-run
        # from wherever the ledger says it got to, so there is nothing special
        # to arrange here.
        # The deadline is the shift itself, not the runner's default: a pass has
        # already declared how long a chunk may take and that declaration was
        # checked against the lease above. Taking the default instead would
        # cancel a pass whose shift is legitimately longer than it.
        self.task(task, timeout=shift)(run_shift)
        self._passes.append((task, walk))
        if cron is not None:
            self.schedule(task, cron=cron, tenant=walk.tenant)
        return task

    async def _enqueue_next_shift(self, task: str, walk: Any) -> None:
        """Queue the next shift of `walk`. Raises if it cannot.

        Deliberately *not* suppressed. Called from inside `run_shift` this is
        the handler's own failure, so letting it propagate hands the problem to
        the retry machinery that already exists — the shift is re-run from
        wherever the ledger says it got to, and the pass carries on. Swallowing
        it instead stalled the pass until the next cron tick, and a
        non-recurring pass has no next tick: it simply stopped, at whatever
        percentage it had reached, with the ledger still reading `walking`.
        """
        # A fresh dedup key per shift, bucketed by the minute so a pass that
        # cannot make progress re-enqueues at most once a minute instead of
        # spinning, and self-heals as soon as it can advance again.
        key = f"pass:{walk.name}:{walk.tenant}:{int(time.time() // 60)}"
        await self.enqueue(task, key=key, tenant=walk.tenant)

    async def _start_passes(self) -> None:
        """Give every driven pass its first shift, once the database is up.

        One pass that cannot be started must not take the others with it, so the
        isolation here is per pass rather than around the loop — but it is
        counted, because a pass that is never driven does nothing at all and the
        ledger cannot tell that apart from a pass with no work to do.
        """
        for task, walk in self._passes:
            try:
                status = await walk.status(self._db)
                if status is not None and status.phase == "done" and not walk.recurring:
                    continue
                await self._enqueue_next_shift(task, walk)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - one pass must not strand the rest
                self.pass_drive_errors += 1
                # A counter is enough to know *something* is wrong; it is not
                # enough to know *which* pass, and `wreath passes status` is
                # where somebody looks. So the failure is written to the pass's
                # own ledger row, which is the only place that survives this
                # process and the only place the CLI reads.
                await self._record_drive_failure(walk, error)
            else:
                await self._record_drive_failure(walk, None)

    async def _record_drive_failure(self, walk: Any, error: BaseException | None) -> None:
        """Stamp the ledger with this drive attempt, so a silent one is visible.

        Best-effort by necessity: if the database is what failed, this cannot
        record that it failed. The counter still moved, and the ledger's
        `driven_at` going stale is itself the signal -- a pass nothing has
        driven for five minutes reads as `blocked` whether or not the reason
        was written.
        """
        with contextlib.suppress(PostgresError, TimeoutError, OSError):
            connection = await self._db.acquire(walk.workload)
            try:
                await walk.ledger.mark_driven(
                    connection, error=None if error is None else repr(error)
                )
            finally:
                await self._db.release(walk.workload, connection)

    async def _carries(self, column: str) -> bool:
        """Whether this database's `jobs` table has `column`. Cached per runner.

        A deployment whose role cannot `CREATE SCHEMA` applies the DDL by hand,
        so there is always a window where this build is newer than what the DBA
        has applied -- and a statement naming a column that is not there fails
        the *enqueue*. Turning a schema step into a queue outage is the wrong
        trade, so the shape of the table is a precondition this checks rather
        than an error it catches: a broad `except` here would swallow a revoked
        grant and a genuine driver fault alongside the one case it means to
        survive, and inside a caller's `tx` it would poison their transaction.

        Cheap enough to leave unlocked. Two concurrent first-enqueues may both
        ask; the read is idempotent and they agree.

        Parameterised by column rather than written once per column: the
        catalog query underneath is `_pgcatalog.column_exists`, which this used
        to restate inline -- a fifth spelling of a query the tree already had.
        """
        known = self._columns.get(column)
        if known is None:
            connection = await self._db.acquire(self._workload)
            try:
                known = await column_exists(
                    connection, schema=self._schema, table="jobs", column=column
                )
            finally:
                await self._db.release(self._workload, connection)
            self._columns[column] = known
        return known

    async def _carries_trace(self) -> bool:
        """Whether the version-2 trace column is present."""
        return await self._carries("trace_context")

    async def _carries_priority(self) -> bool:
        """Whether the version-3 priority column is present.

        Read on the enqueue path *and* resolved at `start` for the claim loop,
        because the claim orders by it and a claim that named a missing column
        would stop the queue rather than degrade it.
        """
        return await self._carries("priority")

    async def enqueue(
        self,
        task: str,
        *args: Any,
        tx: Any = None,
        run_at: Any = None,
        key: str | None = None,
        tenant: str = "",
        max_attempts: int | None = None,
        priority: int = 0,
        coalesce: bool = False,
    ) -> int | None:
        """Insert a job. Returns its id, or `None` if a `key` deduplicated it.

        Pass `tx` (an open `connection.transaction()`) to enqueue atomically
        with your business writes — the job becomes visible only if that
        transaction commits (exactly-once *enqueue*). `key` sets an idempotency
        key: a second enqueue with the same `(queue, key)` is dropped.

        `priority` orders the claim: ready jobs are taken `priority DESC,
        run_at`, so a higher number goes first. It is a lane, not a promise —
        a strict priority queue starves its low lane by definition, and sizing
        for that stays the caller's.

        `coalesce` changes what a repeated `key` does. By default the second
        enqueue is **dropped**, which is right for "run this once" and wrong
        when the second call carries something the first did not: the arguments
        are replaced, the run time becomes the *earlier* of the two, and the
        priority the *higher*. Only while the row is still `ready` — a leased
        job is being worked, and rewriting its arguments underneath the worker
        would hand it inputs it never read. Returns the existing id rather than
        `None`, because the work is still pending and the caller asked for it.

        The calling request's trace context rides the row when the schema has
        somewhere to put it, so the job names the request that caused it. It
        rides the *row* and never the `NOTIFY`: that payload is a doorbell, is
        capped at 8000 bytes, and is deliberately empty.
        """
        if task not in self._tasks:
            raise KeyError(f"unknown task: {task!r} (register with @runner.task)")
        if coalesce and key is None:
            raise ValueError(
                "coalesce= needs a key: there is nothing to coalesce onto without "
                "one, and a silent no-op would read as a merge that happened"
            )
        payload = json.dumps(list(args))
        dk = dedup_key(self._name, key) if key is not None else None
        max_att = max_attempts if max_attempts is not None else self._tasks[task].max_attempts
        bound = _telemetry.outbound_context.get()
        # The traceparent only. `tracestate` is vendor routing for the *next*
        # hop of a live call; a job resumes the trace rather than continuing a
        # conversation, and storing it would age in the queue.
        parent = bound[0] if bound else None
        # `LEAST`/`GREATEST` rather than a read-then-write: the merge has to be
        # part of the same statement, or two callers interleave and one of the
        # two updates is lost -- which is the whole reason the drop-on-conflict
        # form was safe and a hand-rolled merge is not.
        conflict = (
            "DO UPDATE SET args = excluded.args, "
            "run_at = LEAST(j.run_at, excluded.run_at), "
            "priority = GREATEST(j.priority, excluded.priority), "
            "updated_at = now() "
            "WHERE j.state = 'ready'"
            if coalesce
            else "DO NOTHING"
        )
        # The default enqueue never names `priority` and therefore never probes
        # for it: the column has a server-side DEFAULT, so omitting it writes
        # the same row, and a probe on the common path would put a catalog read
        # on an enqueue that has no use for the answer. Only a caller that
        # actually asks for an ordering pays for one, once.
        has_priority = bool(priority) and await self._carries_priority()
        if priority and not has_priority:
            # Dropping a requested ordering silently would let a caller believe
            # a job was prioritised when the column to hold it does not exist.
            raise RuntimeError(
                f"priority={priority} was requested but this database's jobs table "
                "has no priority column; apply the schema step (wreath schema sql "
                "--component jobs) or drop the argument"
            )
        columns = "priority, " if has_priority else ""
        if parent is not None and await self._carries_trace():
            sql = (
                f"INSERT INTO {self._table} AS j "
                "(queue, task, args, tenant, state, run_at, max_attempts, "
                f"dedup_key, {columns}trace_context) "
                "VALUES ($1, $2, $3::jsonb, $4, 'ready', COALESCE($5, now()), "
                f"$6, $7, {'$9, ' if has_priority else ''}$8) "
                "ON CONFLICT (queue, dedup_key) WHERE dedup_key IS NOT NULL "
                f"{conflict} RETURNING id"
            )
            params = (
                self._name,
                task,
                payload,
                tenant,
                run_at,
                max_att,
                dk,
                parent,
                *((priority,) if has_priority else ()),
            )
            return await self._insert(sql, params, tx)
        sql = (
            f"INSERT INTO {self._table} AS j "
            "(queue, task, args, tenant, state, run_at, max_attempts, dedup_key"
            f"{', priority' if has_priority else ''}) "
            "VALUES ($1, $2, $3::jsonb, $4, 'ready', COALESCE($5, now()), $6, $7"
            f"{', $8' if has_priority else ''}) "
            "ON CONFLICT (queue, dedup_key) WHERE dedup_key IS NOT NULL "
            f"{conflict} RETURNING id"
        )
        params = (
            self._name,
            task,
            payload,
            tenant,
            run_at,
            max_att,
            dk,
            *((priority,) if has_priority else ()),
        )
        return await self._insert(sql, params, tx)

    async def _insert(self, sql: str, params: tuple[Any, ...], tx: Any) -> int | None:
        if tx is not None:
            job_id = await tx.fetchval(sql, *params)
            await tx.execute("SELECT pg_notify($1, '')", self._channel)
            return job_id
        connection = await self._db.acquire(self._workload)
        try:
            job_id = await connection.fetchval(sql, *params)
            await connection.execute("SELECT pg_notify($1, '')", self._channel)
        finally:
            await self._db.release(self._workload, connection)
        return job_id

    async def launch(
        self,
        task: str,
        *args: Any,
        tx: Any = None,
        run_at: Any = None,
        key: str | None = None,
        tenant: str = "",
        max_attempts: int | None = None,
    ) -> TaskHandle:
        """Enqueue `task` and return a handle the client can watch.

        The long-mutation shape: a request that cannot finish in a request
        enqueues durable work and hands back an id instead of a timeout:

        ```python
        @app.post("/herd/imports")
        async def start_import(request, path: str):
            return (await jobs.launch("import_herd", path)).as_dict()

        @app.get("/herd/imports/{task_id}/stream")
        async def watch(request):
            return progress_stream(jobs.progress, request.path_params["task_id"])
        ```
        The **job id is the task id**, so there is one identifier rather than
        two to correlate. With a progress registry configured the task is seeded
        as `queued` right here, so a client that starts polling immediately
        sees a pending task rather than a 404 it will read as a failure.

        A `key` that deduplicates does not lose the caller: the surviving row
        is looked up and its handle returned, so submitting the same work twice
        yields the same task to watch rather than nothing at all. If that row is
        gone by the time it is read -- purged after completing, in the window
        between the conflict and the lookup -- there is nothing to watch and
        `JobVanished` is raised rather than a handle whose id is the
        string `"None"`.
        """
        job_id = await self.enqueue(
            task,
            *args,
            tx=tx,
            run_at=run_at,
            key=key,
            tenant=tenant,
            max_attempts=max_attempts,
        )
        if job_id is not None:
            if self._progress is not None:
                self._progress.report(str(job_id), 0, "queued", state="queued")
            return TaskHandle(task_id=str(job_id))
        # Deduplicated by `key`: the work is already queued or running, and the
        # caller still needs to know which task to watch.
        existing = await self._existing_job_id(key, tx=tx)
        if existing is None:
            # `str(None)` here would mint the four-character task id "None" and
            # hand it to a client as something to poll.
            raise JobVanished(
                f"launch({task!r}, key={key!r}) was deduplicated but the surviving "
                "job row was gone when it was read; there is no task to watch"
            )
        task_id = str(existing)
        # Seed only if this worker has nothing, because progress fan-out is
        # at-most-once with no replay: a worker that started after the original
        # `launch`, or that missed the publish, has no entry for a task that is
        # genuinely running, and would hand back a handle that 404s on status
        # and streams forever. Seeding *unconditionally* would be the opposite
        # bug -- the original may be at 70% on this very worker, and every
        # watching client would see the import start over.
        if self._progress is not None and self._progress.get(task_id) is None:
            self._progress.report(
                task_id,
                0,
                "already in flight on another worker",
                state="running",
            )
        return TaskHandle(task_id=task_id, state="running")

    async def _existing_job_id(self, key: str | None, *, tx: Any = None) -> Any:
        sql = f"SELECT id FROM {self._table} WHERE queue=$1 AND dedup_key=$2"
        params = (self._name, dedup_key(self._name, key) if key is not None else None)
        if tx is not None:
            return await tx.fetchval(sql, *params)
        connection = await self._db.acquire(self._workload)
        try:
            return await connection.fetchval(sql, *params)
        finally:
            await self._db.release(self._workload, connection)

    def component(self) -> Any:
        """This runner's claim on the wreath schema.

        The queue's tables are wreath's furniture, not the application's data
        model, so they live in the `wreath` schema and never appear in the
        application's migration artifact -- nobody declared a job queue. `Wreath`
        collects this during lifespan and brings it up to date before any worker
        starts; a deployment that cannot grant `CREATE SCHEMA` applies the same
        statements by hand and is refused at startup, by name, if it has not.

        Statements are individual rather than semicolon-joined because the driver
        prepares every statement and PostgreSQL refuses `cannot insert multiple
        commands into a prepared statement`. See `wreath.schema`.
        """
        from .schema import Component, Step

        t = self._table
        return Component(
            name="jobs",
            schema=self._schema,
            relations=("jobs",),
            steps=(
                Step(
                    version=1,
                    statements=(
                        f"CREATE TABLE IF NOT EXISTS {t} (\n"
                        "  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n"
                        "  queue text NOT NULL,\n"
                        "  task text NOT NULL,\n"
                        "  args jsonb NOT NULL DEFAULT '[]'::jsonb,\n"
                        "  tenant text NOT NULL DEFAULT '',\n"
                        "  state text NOT NULL DEFAULT 'ready',\n"
                        "  run_at timestamptz NOT NULL DEFAULT now(),\n"
                        "  attempts int NOT NULL DEFAULT 0,\n"
                        "  max_attempts int NOT NULL DEFAULT 5,\n"
                        "  lease_expiry timestamptz,\n"
                        "  owner text,\n"
                        "  fence bigint NOT NULL DEFAULT 0,\n"
                        "  dedup_key text,\n"
                        "  last_error text,\n"
                        "  created_at timestamptz NOT NULL DEFAULT now(),\n"
                        "  updated_at timestamptz NOT NULL DEFAULT now()\n"
                        ")",
                        f"CREATE INDEX IF NOT EXISTS jobs_claim_idx ON {t} "
                        "(queue, run_at) WHERE state = 'ready'",
                        f"CREATE INDEX IF NOT EXISTS jobs_lease_idx ON {t} "
                        "(lease_expiry) WHERE state = 'leased'",
                        f"CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedup_idx ON {t} "
                        "(queue, dedup_key) WHERE dedup_key IS NOT NULL",
                    ),
                ),
                Step(
                    version=2,
                    statements=(
                        # The W3C `traceparent` of the request that enqueued this
                        # job, so a failure at 03:00 names its cause. Nullable and
                        # defaulted-absent, which is what makes the step safe for
                        # the previous release to keep running against: an older
                        # build never writes it and never reads it.
                        # One text column rather than three integers. 55 bytes is
                        # the interchange format, it is what a tracing UI accepts
                        # pasted in, and splitting it would mean reassembling it
                        # at every read for no gain.
                        f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS trace_context text",
                    ),
                ),
                Step(
                    version=3,
                    statements=(
                        # A lane, not a promise. Claim order becomes
                        # `priority DESC, run_at`, so a higher number is taken
                        # first among ready jobs -- and a starved low-priority
                        # job stays the caller's problem to size for, because a
                        # strict priority queue starves by definition.
                        # Defaulted rather than nullable: `ORDER BY ... DESC`
                        # sorts NULL first, so an older build's rows would
                        # outrank everything the moment this landed.
                        f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS priority int NOT NULL DEFAULT 0",
                        # Replaced rather than added to: a scan ordered by
                        # (priority DESC, run_at) cannot use an index keyed on
                        # (queue, run_at), so leaving the old one would keep the
                        # old plan and pay to maintain two indexes.
                        f"DROP INDEX IF EXISTS {self._schema}.jobs_claim_idx",
                        f"CREATE INDEX IF NOT EXISTS jobs_claim_idx ON {t} "
                        "(queue, priority DESC, run_at) WHERE state = 'ready'",
                    ),
                ),
            ),
        )

    def schema_sql(self) -> str:
        """DDL for the jobs table + indexes, semicolon-joined.

        A derivation of `component()`, not a second copy. It was both for one
        stage of this work -- the same sixteen columns written out twice -- which
        is two spellings of one truth and exactly the shape this repository
        treats as a defect. Retained for a caller applying the DDL itself;
        `wreath schema sql` is the supported spelling.
        """
        return self.component().sql()

    async def start(self, supervisor: Any) -> None:
        self._supervisor = supervisor
        # Establish the schema shape once, here, so `_claim` never pays for it
        # in the worker loop. A producer that only ever enqueues resolves it on
        # its first enqueue instead; between them every process that needs the
        # answer has it before it needs it.
        # Caught narrowly and counted, never raised: this runs on the startup
        # path, and a database that is down at boot must not stop the runner
        # coming up -- that exact failure once left a process with no doorbell
        # for its entire life, which is why `Doorbell.open` below reports rather
        # than raises. A runner that starts against a down database keeps the
        # answer unresolved and carries no trace context until an `enqueue`
        # resolves it; `trace_probe_errors` is what says that happened, because
        # a silent degradation is the shape this repository treats as a defect.
        try:
            await self._carries_trace()
            await self._carries_priority()
        except PostgresError, OSError:
            self.trace_probe_errors += 1
        # A dedicated held connection for the NOTIFY doorbell (never leased back
        # while listening — design 01 §5). Correctness does not depend on it, so
        # a failure to establish it degrades to pure polling — but *permanently*
        # losing it is a different thing from riding out a blip. Connect once
        # here so a runner starting against a healthy database is listening by
        # the time `start` returns, and spawn the loop regardless: it owns every
        # subsequent connection including this one having failed. Spawning it
        # only on a successful connect — as this did — meant a database that was
        # down at boot left the process with no doorbell for its entire life.
        await self._doorbell.open()
        supervisor.spawn(
            f"jobs:{self._name}:doorbell",
            self._doorbell.run(supervisor.stopping),
        )
        for index in range(self._concurrency):
            supervisor.spawn(f"jobs:{self._name}:worker:{index}", self._worker())
        supervisor.spawn(f"jobs:{self._name}:sweeper", self._sweeper())
        if self._schedules:
            supervisor.spawn(f"jobs:{self._name}:scheduler", self._scheduler())
        if self._passes:
            supervisor.spawn(f"jobs:{self._name}:passes", self._start_passes())

    async def drain(self, deadline: float) -> None:
        # Stop-fetch is signalled by supervisor.stopping; here we wait for the
        # bounded in-flight handlers to settle, then release the listen conn.
        loop = asyncio.get_running_loop()
        while self._inflight and loop.time() < deadline:
            # No catch: `asyncio.wait` does not raise for a task that failed,
            # and the only documented raise -- an empty set -- is impossible
            # under the `while self._inflight` above. Guarding beats catching.
            pending = tuple(self._inflight)
            await asyncio.wait(pending, timeout=max(0.0, deadline - loop.time()))
        # Claimed and never started: hand them back rather than making the next
        # worker wait out the lease.
        await self._release_unstarted()
        await self._doorbell.release()

    async def purge(self, *, older_than: float) -> None:
        """Delete finished rows older than `older_than` seconds.

        Nothing calls this for you, for the same reason nothing purges
        `wreath.store`: a background sweep duplicates across workers and
        swallows its own failures. Run it from a scheduled job.

        Only `done` and `dead` rows go: a `dead` one has exhausted its
        attempts and is a record rather than work, and keeping either forever is
        what makes `launch(key=...)` eventually raise `JobVanished` --
        the retention this table always assumed and never had.
        """
        if older_than <= 0:
            raise ValueError("older_than must be positive")
        await self._exec(
            f"DELETE FROM {self._table} WHERE queue=$1 "
            "AND state IN ('done', 'dead') "
            "AND updated_at < now() - ($2 || ' seconds')::interval",
            self._name,
            f"{float(older_than):.3f}",
        )

    async def cancel(
        self, job_id: int | None = None, *, key: str | None = None, reason: str = "cancelled"
    ) -> bool:
        """Stop a queued or leased job. Returns whether a row moved.

        Name it by `id`, or by the `key` an `enqueue`/`launch` deduplicated on
        -- the second is what a caller has when the id belongs to whichever
        worker enqueued first.

        **The fence is bumped, and that is the whole mechanism.** There is no
        signal to a worker in another process and this does not invent one: the
        row goes to `dead`, its fence moves, and the running attempt's
        completion `UPDATE` -- which is already `WHERE id=$1 AND fence=$2` --
        matches nothing when it lands. So a cancelled job stops being retried
        immediately, and the attempt in flight stops being able to say anything
        about the queue. What it does *not* do is interrupt that attempt's side
        effects, for the same reason a lease expiry does not: nothing here can
        reach into another process's event loop, and pretending otherwise is how
        a "cancelled" job charges a card.

        A job that has already reached `done` or `dead` is left alone and
        answers `False`; cancelling something that finished is not an error, it
        is a race the caller lost.

        Raises:
            ValueError: neither or both of `job_id` and `key` were given.
        """
        if (job_id is None) == (key is None):
            raise ValueError(
                "cancel() takes exactly one of job_id= and key=: naming both would "
                "leave which one selects the row undecided, and naming neither "
                "would cancel the whole queue"
            )
        # Branching on `key` rather than on `job_id`, so the narrowing is real:
        # the check above has already established that exactly one is given, and
        # spelling it this way means `dedup_key` is handed a `str` instead of a
        # `key or ""` fallback for a `None` that cannot arrive here.
        if key is not None:
            selector, value = "dedup_key=$2", dedup_key(self._name, key)
        else:
            selector, value = "id=$2", job_id
        rows = await self._fetch(
            f"UPDATE {self._table} SET state='dead', fence=fence+1, "
            "last_error=$3, owner=NULL, lease_expiry=NULL, updated_at=now() "
            f"WHERE queue=$1 AND {selector} AND state IN ('ready', 'leased') "
            "RETURNING id",
            self._name,
            value,
            reason[:2000],
        )
        if not rows:
            return False
        self.cancelled += 1
        if self._progress is not None:
            # Closed out here rather than through `_report_terminal`, which
            # takes a `_Claimed` this path never had: nothing was claimed, a row
            # was told to stop. A watching client would otherwise sit at
            # whatever percentage the last report left, forever.
            for row in rows:
                self._progress.report(
                    str(row["id"]),
                    self._last_percent(row["id"]),
                    reason,
                    state="failed",
                    error=reason,
                )
        return True

    async def _pump(self, connection: Any) -> None:
        """Wake parked workers until the connection's stream ends.

        Returning is the ordinary end of a dropped connection, and
        `Doorbell` reopens on it.

        **Unlike the bus's pump there is no user code on this path** — a
        notification only sets an event — so there is nothing here that could
        raise on someone else's behalf, nothing to catch, and no second counter
        to keep apart from an outage. `MessageBus._pump` catches per dispatch and
        counts `handler_errors` for exactly the reason this one does not.
        """
        if connection is None:
            return
        async for _notification in connection.notifications():
            self._wake_workers()

    async def _worker(self) -> None:
        stopping = self._supervisor.stopping
        wake = self._new_waiter()
        try:
            await self._work(stopping, wake)
        finally:
            self._waiters.remove(wake)

    async def _work(self, stopping: asyncio.Event, wake: asyncio.Event) -> None:
        while not stopping.is_set():
            # Cleared *before* the claim: a doorbell that rings while this claim
            # is in flight leaves the waiter set, so the park below returns at
            # once instead of sleeping through work that has already arrived.
            wake.clear()
            try:
                claimed = await self._claim(self._batch)
            except Exception:  # noqa: BLE001 - a transient DB error must not kill the worker
                await self._park(wake)
                continue
            if not claimed:
                await self._park(wake)
                continue
            self._claimed_not_started.extend(claimed)
            for job in claimed:
                if stopping.is_set():
                    break
                self._discard_claim(job)
                try:
                    await self._run(job)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - see below
                    # `_claim` was already guarded; the *run* was not, so a
                    # transient database error while recording an outcome
                    # (`_complete`/`_fail` both issue an UPDATE) propagated out
                    # of this loop and ended the worker for the life of the
                    # process. The job stays leased and the sweeper reclaims it,
                    # which is the same path a crashed worker takes.
                    self.run_errors += 1
                    await self._park(wake)

    def _discard_claim(self, job: _Claimed) -> None:
        """Stop tracking `job` as claimed-but-unstarted."""
        for index, pending in enumerate(self._claimed_not_started):
            if pending is job:
                del self._claimed_not_started[index]
                return

    async def _release_unstarted(self) -> None:
        """Hand back everything claimed and never started.

        Fenced like every other transition: a job the sweeper already reclaimed
        has a newer fence, so this cannot pull it out from under whoever has it
        now. `attempts` is deliberately *not* bumped -- the job was never
        attempted, only held.
        """
        pending, self._claimed_not_started = self._claimed_not_started, []
        for job in pending:
            # Narrowed: the lease expires and another worker takes the job, so a
            # database failure here costs latency and nothing else. A bug in the
            # statement is not that, and must not be lost on the shutdown path.
            with contextlib.suppress(PostgresError, TimeoutError, OSError):
                await self._exec(
                    f"UPDATE {self._table} SET state='ready', owner=NULL, "
                    "lease_expiry=NULL, updated_at=now() "
                    "WHERE id=$1 AND fence=$2 AND state='leased'",
                    job.id,
                    job.fence,
                )

    async def _run(self, job: _Claimed) -> None:
        handler = self._tasks.get(job.task)
        ctx = JobContext(
            job_id=job.id,
            task=job.task,
            attempt=job.attempts + 1,
            fence=job.fence,
            tenant=job.tenant,
            key=job.key,
            progress=self._progress,
            trace_context=job.trace_context,
        )
        if handler is None:
            await self._fail(job, f"no handler registered for task {job.task!r}")
            return
        # Bound before the call, because a row enqueued by an older release can
        # carry an arity this handler no longer accepts. Left to the call itself
        # the `TypeError` was raised while building the coroutine -- outside the
        # try below -- so it escaped `_run` entirely: no `_fail`, no `last_error`,
        # no backoff. The job stayed leased until the sweeper reclaimed it and
        # eventually dead-lettered it as "lease expired before completion", which
        # points at a hung handler rather than at a signature change.
        try:
            handler.signature.bind(ctx, *job.args)
        except TypeError as error:
            await self._fail(
                job,
                f"{job.task!r} was enqueued with {len(job.args)} argument(s) that "
                f"its handler no longer accepts ({error}); this job was written by "
                "a different release and retrying cannot fix it",
                handler,
                permanent=True,
            )
            return
        deadline = self.deadline_for(job.task)
        # The trace context is bound around the handler and unbound in a
        # `finally`, so a worker that runs thousands of jobs does not hand job
        # N+1 the context of job N -- the same staleness the request pipeline
        # binds `None` to avoid.
        trace_token = _telemetry.outbound_context.set(
            (job.trace_context, "") if job.trace_context else None
        )
        try:
            await self._run_handler(job, ctx, handler, deadline)
        finally:
            _telemetry.outbound_context.reset(trace_token)

    async def _run_handler(
        self, job: _Claimed, ctx: JobContext, handler: Any, deadline: float
    ) -> None:
        from ._recording_format import AttemptOutcome

        recorder = self._attempts
        trace = recorder.trace() if recorder is not None else None
        # The boundary observers go on before the task is created for the
        # same reason the ledger does: the handler starts running at the
        # first await below, and a boundary crossed before they were
        # installed is a crossing missing from the recording.
        with self._query_scope(handler, job) as ledger, self._observing(trace):
            future = asyncio.ensure_future(handler.func(ctx, *job.args))
            self._inflight.add(future)
            try:
                async with asyncio.timeout(deadline):
                    await future
            except asyncio.CancelledError:
                # Not ours: the supervisor is stopping. `asyncio.timeout`
                # re-raises a cancellation it did not cause, so this stays the
                # shutdown path.
                raise
            except TimeoutError:
                # Ours. The handler has already been cancelled by the timeout;
                # the attempt is charged and retried like any other failure,
                # because a deadline miss is usually a slow dependency rather
                # than a bug. It is recorded as `deadline_cancelled` and not as
                # `raised`: nothing failed, work was stopped.
                self.run_timeouts += 1
                self._record_attempt(job, AttemptOutcome.DEADLINE_CANCELLED, None, trace)
                await self._fail(
                    job,
                    f"{job.task!r} timed out after {deadline:g}s and was cancelled",
                    handler,
                )
                return
            except _NPlusOneDetected as error:
                # A declared budget was crossed. Counted apart from
                # `run_errors` because the cause is a defect in the handler
                # rather than in whatever it was calling, and retrying will
                # reproduce it exactly.
                self.query_budget_exceeded += 1
                self._record_attempt(job, AttemptOutcome.RAISED, error, trace)
                await self._fail(job, repr(error), handler)
                return
            except Exception as error:  # noqa: BLE001 - handler failures drive retry/dead-letter
                self._record_attempt(job, AttemptOutcome.RAISED, error, trace)
                await self._fail(job, repr(error), handler)
                return
            finally:
                self._inflight.discard(future)
                self._report_repetition(ledger)
        self._record_attempt(job, AttemptOutcome.COMPLETED, None, trace)
        await self._complete(job)

    def _observing(self, trace: Any) -> Any:
        """Watch this attempt's boundaries, when one is being recorded.

        Nothing is substituted: an observed attempt does exactly what an
        unobserved one does, and the observers only count crossings. The
        runner's own `_db` is watched through `slots` because it is held on an
        attribute rather than in a name -> database registry, and the scope is
        whatever application the recorder was given (`None` watches only the
        runner's database).
        """
        if trace is None:
            return contextlib.nullcontext()
        from ._replay_adapters import observed_boundaries

        return observed_boundaries(
            self._attempts.scope,
            trace,
            slots=((self, "_db", getattr(self._db, "name", "main")),),
        )

    def _record_attempt(
        self, job: _Claimed, outcome: Any, error: BaseException | None, trace: Any
    ) -> None:
        """Write this attempt down, if an operator armed for it.

        Called **before** the queue row is updated, on purpose. `_fail` and
        `_complete` both issue a statement, and the moment a recording is worth
        most is the moment those cannot land -- a database in trouble is
        precisely when the evidence matters and precisely when recording it
        afterwards would never happen.

        Synchronous, and it never raises: `AttemptRecorder.write` counts what it
        could not do. A recorder that can take a worker down with it is worse
        than no recorder, and the attempt it describes has already happened.

        The boundary trace is snapshotted here, so the runner's own bookkeeping
        statements -- which run after this, inside the same observing scope --
        are not in it. They are the queue's crossings, not the attempt's.
        """
        if self._attempts is None:
            return
        self._write_attempt(self._attempt_record(job, outcome, error, trace), trace)

    def _write_attempt(self, record: Any, trace: Any) -> None:
        """Ask the policy about this attempt, and write it if it says yes.

        The arming question is asked of the *record*, not of the claim, so the
        two producers -- a worker finishing an attempt and the sweeper
        reclaiming an expired one -- cannot drift into asking it differently.

        Both callers have already established that a recorder exists, and there
        is deliberately no second check here: two spellings of one condition
        drift, and the one that was here absorbed the caller's, so removing the
        caller's guard would have gone unnoticed.
        """
        recorder = self._attempts
        if not recorder.captures(
            task=record.task,
            outcome=record.outcome,
            attempt=record.attempt,
            max_attempts=record.max_attempts,
            job_id=record.job_id,
        ):
            return
        recorder.write(record, trace)

    def _attempt_record(
        self, job: _Claimed, outcome: Any, error: BaseException | None, trace: Any
    ) -> Any:
        """Identity, cause, boundaries, outcome -- and the arguments an operator
        allowed by name, if any.

        `argument_count` is what the payload contributes when nobody has named
        one, which is the default. `AttemptPolicy.argument_allowlist` supplies
        the names a positional array does not have, out of the *handler's*
        signature -- so a task this process does not have registered captures
        nothing, rather than falling back to position.
        """
        from ._recording_format import AttemptRecord

        registered = self._tasks.get(job.task)
        arguments = self._attempts.policy.capture_arguments(
            task=job.task,
            handler=None if registered is None else registered.func,
            args=job.args,
            kwargs={},
            # `handler.func(ctx, *job.args)` -- the context is this process's
            # object and never the payload's, so it is aligned past rather than
            # offered as a nameable parameter.
            framework_parameters=1,
        )

        return AttemptRecord(
            job_id=job.id,
            queue=self._name,
            task=job.task,
            attempt=job.attempts + 1,
            max_attempts=job.max_attempts,
            tenant=job.tenant,
            dedup_key=job.key or "",
            fence=job.fence,
            # `getattr` rather than an attribute: the column is optional in the
            # table and the claim only carries it where the queue has it.
            trace_context=getattr(job, "trace_context", None) or "",
            # Never None here: `_record_attempt` returns before this when there
            # is no recorder, and a recorder always makes a trace.
            boundaries=trace.events,
            outcome=str(outcome),
            error_type="" if error is None else type(error).__name__,
            error_message="" if error is None else str(error),
            argument_count=len(job.args),
            arguments=arguments,
        )

    def _query_scope(self, handler: _Task, job: _Claimed) -> Any:
        """The N+1 ledger for one attempt, or nothing when nobody asked.

        An *attempt*, not a job: a retry is a separate execution and starts
        from zero, or a task that legitimately queries ninety times would
        dead-letter itself on the second try.
        """
        return _nplusone.scope_for(
            _nplusone.Origin(kind="job", label=job.task, identifier=str(job.id)),
            budget=handler.query_budget,
        )

    def _report_repetition(self, ledger: Any) -> None:
        """Count what an observing attempt saw, so a finding has an actor.

        Only for the no-budget case: a scope with a budget already failed the
        attempt at the ceiling, and counting it twice would say a defect
        happened once as a failure and once as an observation.
        """
        if ledger is None or ledger.on_exceeded is not None:
            return
        finding = ledger.finding()
        if finding is None:
            return
        self.nplusone_findings += 1

    #: What fraction of the lease a handler may spend before it is cancelled.
    #: The remaining fifth is for the cancellation to land and the failure to be
    #: recorded -- a handler cancelled exactly *at* the lease would be racing the
    #: sweeper for its own row.
    DEADLINE_FRACTION = 0.8

    def deadline_for(self, task: str) -> float:
        """Seconds `task` may run before it is cancelled."""
        registered = self._tasks.get(task)
        declared = registered.timeout if registered is not None else None
        return declared if declared is not None else self._lease * self.DEADLINE_FRACTION

    async def _fail(
        self,
        job: _Claimed,
        error: str,
        handler: _Task | None = None,
        *,
        permanent: bool = False,
    ) -> None:
        """Record a failed attempt, retrying unless the failure cannot succeed.

        `permanent` dead-letters on the first attempt. It is for failures that
        are structural rather than transient -- a job whose arguments no longer
        bind to its handler will not bind on the fourth try either, and spending
        a retry budget on it only delays the diagnosis and hides the real error
        behind a backoff.
        """
        attempts = job.attempts + 1
        max_att = handler.max_attempts if handler is not None else job.max_attempts
        if permanent or attempts >= max_att:
            await self._exec(
                f"UPDATE {self._table} SET state='dead', attempts=$3, last_error=$4, "
                "updated_at=now() WHERE id=$1 AND fence=$2",
                job.id,
                job.fence,
                attempts,
                error[:2000],
            )
            self.dead_lettered += 1
            self._report_terminal(job, "failed", error)
            return
        # A retry is not an ending. Reporting `failed` here would tell a watching
        # client the work is over while the runner is about to try again.
        delay = 0.0
        if handler is not None:
            delay = compute_backoff(
                attempts,
                kind=handler.backoff_kind,
                base=handler.backoff_base,
                factor=handler.backoff_factor,
                cap=handler.backoff_cap,
                jitter=handler.backoff_jitter,
            )
        await self._exec(
            f"UPDATE {self._table} SET state='ready', attempts=$3, "
            "run_at = now() + ($5 || ' seconds')::interval, last_error=$4, "
            "owner=NULL, lease_expiry=NULL, updated_at=now() "
            "WHERE id=$1 AND fence=$2",
            job.id,
            job.fence,
            attempts,
            error[:2000],
            f"{delay:.3f}",
        )

    async def _complete(self, job: _Claimed) -> None:
        # Fenced: a stale worker whose lease expired (fence bumped by the sweeper)
        # cannot mark someone else's re-claim done.
        await self._exec(
            fenced_update_sql(self._table, "state='done', updated_at=now()"),
            job.id,
            job.fence,
        )
        self._report_terminal(job, "done", None)

    def _report_terminal(self, job: _Claimed, state: str, error: str | None) -> None:
        """Close out the task, so a handler never has to remember to.

        Only the runner knows whether a raised exception means "retrying" or
        "given up", and only it sees a job that completed without the handler
        reporting anything. Leaving this to handlers is how progress bars end
        up stuck at 90% forever.
        """
        if self._progress is None:
            return
        self._progress.report(
            str(job.id),
            100 if state == "done" else self._last_percent(job.id),
            state,
            state=state,
            error=error,
        )

    def _last_percent(self, job_id: int) -> float:
        current = self._progress.get(str(job_id))
        return current.percent if current is not None else 0.0

    async def _claim(self, batch: int) -> list[_Claimed]:
        # Read from the cache, never probed here. `_claim` runs in the worker
        # loop, and putting a catalog lookup on that path would buy one bit of
        # schema shape at the cost of a query per poll on every worker. The
        # answer is established once, by `start()` on a worker and by the first
        # `enqueue` on a producer, which between them cover every process that
        # can reach this line. Unknown means "do not select it": a runner that
        # never started and never enqueued has no context to carry anyway.
        trace = ", j.trace_context" if self._columns.get("trace_context") else ""
        # A schema without the version-3 column cannot be ordered by it, and
        # a claim that named it would stop the queue rather than degrade it.
        order = "priority DESC, run_at" if self._columns.get("priority") else "run_at"
        sql = claim_sql(
            self._table,
            key="id",
            alias="j",
            predicate="queue=$1 AND state='ready' AND run_at <= now()",
            order=order,
            limit="$2",
            assignments=(
                "state='leased', owner=$3, "
                "lease_expiry = now() + ($4 || ' seconds')::interval, "
                "fence = j.fence + 1, updated_at=now()"
            ),
            returning=(
                "j.id, j.task, j.args, j.tenant, "
                f"j.attempts, j.max_attempts, j.fence, j.dedup_key{trace}"
            ),
        )
        connection = await self._db.acquire(self._workload)
        try:
            rows = await connection.fetch(
                sql, self._name, batch, self._worker_id, f"{self._lease:.3f}"
            )
        finally:
            await self._db.release(self._workload, connection)
        return [self._row_to_claim(row) for row in rows]

    @staticmethod
    def _row_to_claim(row: Any) -> _Claimed:
        args = row["args"]
        if isinstance(args, (str, bytes)):
            args = json.loads(args)
        # Asked of the row rather than of `self`, because a claim made before an
        # upgrade can still be in flight after one. Guarding on the key's
        # presence keeps both shapes readable by one function.
        try:
            trace = row["trace_context"]
        except KeyError, IndexError:
            trace = None
        return _Claimed(
            id=row["id"],
            task=row["task"],
            args=list(args or []),
            tenant=row["tenant"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            fence=row["fence"],
            key=row["dedup_key"],
            trace_context=trace,
        )

    async def _sweeper(self) -> None:
        stopping = self._supervisor.stopping
        while not stopping.is_set():
            try:
                await self._reclaim_expired()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a transient error must not end the loop
                self.sweep_errors += 1
            await _sleep_or_stop(stopping, self._lease)

    async def _reclaim_expired(self) -> None:
        """Return this queue's expired leases, counting the attempt.

        The fence is bumped so the previous owner's completion UPDATE (WHERE
        fence=old) can no longer land. `attempts` is bumped for the same
        reason a handler exception bumps it: the job *was* attempted. Reclaiming
        without counting meant the one failure mode that never reaches
        `_fail` -- a handler that kills its worker, or a process that dies
        mid-run -- was redelivered forever and could never dead-letter, so a
        poison job outlived every other kind.

        Scoped to `queue`, because every queue in a schema shares this table and
        the sweep is otherwise fleet-wide. Unscoped, a queue whose own workers
        are down had its in-flight jobs reclaimed by an *unrelated* queue's
        sweeper, on that queue's lease interval, until they exhausted
        `max_attempts` and dead-lettered. Jobs lost to a deploy of a service
        that does not own them, and nothing in either queue's counters says so.
        """
        sql = (
            f"UPDATE {self._table} SET "
            "attempts = attempts + 1, "
            "state = CASE WHEN attempts + 1 >= max_attempts THEN 'dead' ELSE 'ready' END, "
            "last_error = COALESCE(last_error, 'lease expired before completion'), "
            "owner=NULL, lease_expiry=NULL, fence=fence+1, updated_at=now() "
            "WHERE queue=$1 AND state='leased' AND lease_expiry < now()"
        )
        if self._attempts is None:
            await self._exec(sql, self._name)
            return
        # Armed. The sweeper is the only actor that *observes* a lease expiry --
        # the worker that held the lease is, by construction, not here to say so
        # -- which is why the projection grows only when something is recording.
        # An unarmed queue keeps the statement it has always issued.
        rows = await self._fetch(
            sql + " RETURNING id, task, tenant, dedup_key, attempts, max_attempts, "
            # The *count*, computed by PostgreSQL, never the arguments. Reading
            # `args` here to measure it would put the payload on the wire for
            # the sake of a number.
            "fence, jsonb_array_length(args) AS argument_count",
            self._name,
        )
        for row in rows:
            self._record_lease_expiry(row)

    async def _scheduler(self) -> None:
        stopping = self._supervisor.stopping
        while not stopping.is_set():
            try:
                await self._tick_schedules()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - as in _sweeper
                self.schedule_errors += 1
            # Wake near the next minute boundary; a coarse 30s tick is enough
            # because the per-minute dedup key makes double-fires harmless.
            await _sleep_or_stop(stopping, 30.0)

    async def _tick_schedules(self) -> None:
        # `_now` reads the database clock as a naive UTC value, which is the one
        # clock every worker agrees on. Placing it on the timeline here is what
        # lets a zoned recurrence be read on its own wall clock below.
        now = Instant.of(await self._now(), assume=datetime.UTC)
        for sched in self._schedules:
            if sched.recurrence.matches_at(now):
                await self.enqueue(
                    sched.task,
                    *sched.args,
                    tenant=sched.tenant,
                    # The bucket is the recurrence's *local* minute, so the hour
                    # that repeats on a fall-back day enqueues once rather than
                    # twice. For a UTC recurrence this is the same string it has
                    # always been.
                    key=f"cron:{sched.task}:{sched.recurrence.bucket_key(now)}",
                )

    async def _now(self) -> Any:
        connection = await self._db.acquire(self._workload)
        try:
            value = await connection.fetchval("SELECT (now() AT TIME ZONE 'UTC')")
        finally:
            await self._db.release(self._workload, connection)
        return value

    def _record_lease_expiry(self, row: Any) -> None:
        """Record one reclaimed lease as the attempt it ended.

        Two facts make this recording worth having and both are on the row.
        The **fence** is the one the vanished worker held -- one less than the
        bump this sweep just applied -- so a recording can say which of two
        workers that both believed they owned the job was speaking. The
        **attempt number** is the one that expired: the sweep has already
        charged it, so `attempts` on the returned row is that attempt.

        There is no boundary trace, and there cannot be: nothing was watching a
        process that is gone. An empty trace on a `lease_expired` recording is
        the truth, not a gap.
        """
        from ._recording_format import AttemptOutcome, AttemptRecord

        self._write_attempt(
            AttemptRecord(
                job_id=row["id"],
                queue=self._name,
                task=row["task"],
                # Already charged by the sweep, so this *is* the attempt that
                # expired rather than the next one.
                attempt=row["attempts"],
                max_attempts=row["max_attempts"],
                tenant=row["tenant"],
                dedup_key=row["dedup_key"] or "",
                # One less than the bump this sweep applied: the fence the
                # vanished worker was holding, which is what tells two claimants
                # apart.
                fence=row["fence"] - 1,
                # The lease-expiry projection does not select `trace_context`:
                # it is an optional column and this sweep runs against tables
                # that predate it. A worker-recorded attempt carries it.
                trace_context="",
                boundaries=(),
                outcome=str(AttemptOutcome.LEASE_EXPIRED),
                argument_count=row["argument_count"],
            ),
            None,
        )

    async def _exec(self, sql: str, *args: Any) -> None:
        connection = await self._db.acquire(self._workload)
        try:
            await connection.execute(sql, *args)
        finally:
            await self._db.release(self._workload, connection)

    async def _fetch(self, sql: str, *args: Any) -> Any:
        connection = await self._db.acquire(self._workload)
        try:
            return await connection.fetch(sql, *args)
        finally:
            await self._db.release(self._workload, connection)

    def _new_waiter(self) -> asyncio.Event:
        """Register a waiter for one worker. See `_waiters`."""
        wake = asyncio.Event()
        self._waiters.append(wake)
        return wake

    def _wake_workers(self) -> None:
        """Wake every parked worker. One doorbell, every waiter."""
        for wake in tuple(self._waiters):
            wake.set()

    async def _park(self, wake: asyncio.Event) -> None:
        # Wait for a doorbell wake or the poll timeout, whichever comes first.
        with contextlib.suppress(asyncio.TimeoutError):
            async with asyncio.timeout(self._poll):
                await wake.wait()


def _pass_task_name(name: str, tenant: str) -> str:
    """A task name for a pass, since a pass name is prose and a task name is not."""
    slug = re.sub(r"[^A-Za-z0-9_]", "_", f"{name}_{tenant}" if tenant else name)
    return _validate_identifier(f"pass_{slug}"[:63], "pass task name")


@dataclass(frozen=True, slots=True)
class JobRow:
    """One queue row as an operator reads it. No handler, no runner, no lease."""

    id: int
    queue: str
    task: str
    tenant: str
    state: str
    attempts: int
    max_attempts: int
    run_at: Any
    updated_at: Any
    last_error: str | None
    #: The `traceparent` of the request that enqueued this job, or `None` when it
    #: was enqueued outside a traced request -- or when this database is still on
    #: the schema version before the column existed. The two are distinguishable
    #: from `wreath schema check`, not from here.
    trace_context: str | None = None

    @property
    def trace_id(self) -> str | None:
        """The 32-hex trace id, for pasting into a tracing UI or `wreath doctor`."""
        return _telemetry.trace_id_of(self.trace_context)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "queue": self.queue,
            "task": self.task,
            "tenant": self.tenant,
            "state": self.state,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            # Both columns are `NOT NULL DEFAULT now()`, so there is no absent
            # case to hedge for -- unlike `PassStatus`, whose timestamps really
            # are nullable.
            "run_at": str(self.run_at),
            "updated_at": str(self.updated_at),
            "last_error": self.last_error,
            "trace_context": self.trace_context,
            "trace_id": self.trace_id,
        }


_JOB_COLUMNS = (
    "id, queue, task, tenant, state, attempts, max_attempts, run_at, updated_at, last_error"
)


async def read_jobs(
    connection: Any,
    *,
    schema: str = "wreath",
    states: tuple[str, ...] = ("dead",),
    queue: str | None = None,
    limit: int = 50,
) -> list[JobRow]:
    """Rows from the queue table, newest first. For `wreath jobs` and forensics.

    Defaults to `dead` because that is the state somebody goes looking for: a
    job that retried until it ran out, hours after the request that enqueued it
    finished. `states=()` reads every state.

    Selects `trace_context` only where the column is there, so this runs against
    a database still on the schema version before propagation and reports every
    row with no trace rather than failing. A build reading an *older* schema and
    a job enqueued outside a traced request both give `None`; telling them apart
    is `wreath schema check`'s job, not this one's.
    """
    traced = await _has_trace_column(connection, schema=schema)
    columns = _JOB_COLUMNS + (", trace_context" if traced else "")
    clauses = []
    args: list[Any] = []
    if states:
        marks = ", ".join(f"${index + 1}" for index in range(len(states)))
        clauses.append(f"state IN ({marks})")
        args.extend(states)
    if queue is not None:
        args.append(queue)
        clauses.append(f"queue = ${len(args)}")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    args.append(int(limit))
    rows = await connection.fetch(
        f'SELECT {columns} FROM "{schema}".jobs{where} '
        f"ORDER BY updated_at DESC, id DESC LIMIT ${len(args)}",
        *args,
    )
    return [_job_row(row, traced=traced) for row in rows]


def _job_row(row: Any, *, traced: bool) -> JobRow:
    """One row, read straight off the record.

    No per-column fallback: the `SELECT` above names every one of these and each
    is `NOT NULL` in the schema, so a missing key would be a defect in this
    function rather than a shape to survive. `trace_context` is the one column
    that may not have been selected, and `traced` is the same answer the
    projection was built from rather than a second guess at it.
    """
    return JobRow(
        id=row["id"],
        queue=row["queue"],
        task=row["task"],
        tenant=row["tenant"],
        state=row["state"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        run_at=row["run_at"],
        updated_at=row["updated_at"],
        last_error=row["last_error"],
        trace_context=row["trace_context"] if traced else None,
    )


async def _has_trace_column(connection: Any, *, schema: str) -> bool:
    """The version-2 column probe, for a reader that holds no runner.

    Separate from `JobRunner._carries_trace` and deliberately uncached: this is
    a CLI's one read against a database it has just connected to, so there is no
    steady state to keep a catalog lookup out of.
    """
    return await column_exists(connection, schema=schema, table="jobs", column="trace_context")
