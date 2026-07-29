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
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# Re-exported under this module's historic names: the supervision moved to
# `_doorbell`, the names callers and tests already reach for did not.
from ._doorbell import BACKOFF_BASE as DOORBELL_BACKOFF_BASE  # noqa: F401
from ._doorbell import BACKOFF_CAP as DOORBELL_BACKOFF_CAP  # noqa: F401
from ._doorbell import Doorbell
from ._doorbell import delay as _doorbell_delay  # noqa: F401
from ._doorbell import sleep_or_stop as _sleep_or_stop
from ._jobcore import CronSchedule, compute_backoff, dedup_key, validate_identifier
from .postgres import PostgresError

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
    channel = f"wj_{schema}_{queue}"
    if len(channel.encode("utf-8")) > 63:
        raise ValueError(
            f"the doorbell channel for schema {schema!r} and queue {queue!r} is "
            f"{len(channel.encode('utf-8'))} bytes; PostgreSQL truncates a channel "
            "name at 63, which would collide with another queue. Shorten one of them."
        )
    return channel


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


@dataclass(frozen=True, slots=True)
class _Schedule:
    task: str
    cron: CronSchedule
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
        lease: float = 30.0,
        poll_interval: float = 5.0,
        schema: str = "wreath",
        batch: int = 1,
        progress: Any = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
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
        self._tasks: dict[str, _Task] = {}
        self._schedules: list[_Schedule] = []
        self._passes: list[tuple[str, Any]] = []
        self._table = f'"{self._schema}".jobs'
        self._channel = _channel(self._schema, self._name)
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
            database=database, workload=workload, pump=self._pump,
            channels=(self._channel,),
        )
        self._inflight: set[asyncio.Future[Any]] = set()
        #: Jobs this process claimed and has not started running. A batch claim
        #: leases several at once, and a shutdown between the claim and the run
        #: used to leave them leased until the lease expired -- so a rolling
        #: deploy parked `batch - 1` jobs for `lease` seconds per restart, which
        #: reads as a queue that stalls whenever you deploy.
        self._claimed_not_started: list[_Claimed] = []
        self._worker_id = f"{self._name}"
        #: Sweeps that raised. The sweeper suppresses everything so a transient
        #: error cannot end the loop, which also meant a sweeper that had never
        #: once succeeded -- a missing table, a revoked grant -- was
        #: indistinguishable from one with nothing to reclaim.
        self.sweep_errors = 0
        #: Scheduler ticks that raised, for the same reason.
        self.schedule_errors = 0
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
        }

    @property
    def progress(self) -> Any:
        """The registry this runner reports to, for the status/stream endpoints."""
        return self._progress

    # -- registration --------------------------------------------------------

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
    ) -> Callable[[JobHandler], JobHandler]:
        """Decorator registering an async `handler(ctx, *args)` under `name`.

        `timeout` is how long the handler may run before it is cancelled and the
        attempt fails; `None` takes the runner's default, `DEADLINE_FRACTION` of
        the lease. **It must end inside the lease**, and that is checked here.

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
            )
            return func

        return register

    def schedule(
        self,
        task: str,
        *,
        cron: str,
        args: tuple[Any, ...] = (),
        tenant: str = "",
        misfire: str = "skip",
    ) -> None:
        """Enqueue `task` on a cron schedule (UTC). Idempotent across instances.

        Every app instance runs the scheduler, but each minute's enqueue carries a
        deterministic `key` so the unique index makes exactly one row win — no
        leader election needed.
        """
        if misfire != "skip":
            raise ValueError("only misfire='skip' is supported in this cut")
        self._schedules.append(
            _Schedule(task=task, cron=CronSchedule(cron), args=tuple(args), tenant=tenant,
                      misfire=misfire)
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
        #
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

    # -- enqueue -------------------------------------------------------------

    async def enqueue(
        self,
        task: str,
        *args: Any,
        tx: Any = None,
        run_at: Any = None,
        key: str | None = None,
        tenant: str = "",
        max_attempts: int | None = None,
    ) -> int | None:
        """Insert a job. Returns its id, or `None` if a `key` deduplicated it.

        Pass `tx` (an open `connection.transaction()`) to enqueue atomically
        with your business writes — the job becomes visible only if that
        transaction commits (exactly-once *enqueue*). `key` sets an idempotency
        key: a second enqueue with the same `(queue, key)` is dropped.
        """
        if task not in self._tasks:
            raise KeyError(f"unknown task: {task!r} (register with @runner.task)")
        payload = json.dumps(list(args))
        dk = dedup_key(self._name, key) if key is not None else None
        max_att = max_attempts if max_attempts is not None else self._tasks[task].max_attempts
        sql = (
            f"INSERT INTO {self._table} "
            "(queue, task, args, tenant, state, run_at, max_attempts, dedup_key) "
            "VALUES ($1, $2, $3::jsonb, $4, 'ready', COALESCE($5, now()), $6, $7) "
            "ON CONFLICT (queue, dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING "
            "RETURNING id"
        )
        params = (self._name, task, payload, tenant, run_at, max_att, dk)
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
            task, *args, tx=tx, run_at=run_at, key=key, tenant=tenant,
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
                task_id, 0, "already in flight on another worker", state="running",
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

    # -- schema --------------------------------------------------------------

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

    # -- supervised service protocol ----------------------------------------

    async def start(self, supervisor: Any) -> None:
        self._supervisor = supervisor
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
            f"jobs:{self._name}:doorbell", self._doorbell.run(supervisor.stopping),
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
            self._name, f"{float(older_than):.3f}",
        )

    # -- loops ---------------------------------------------------------------

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
                    job.id, job.fence,
                )

    async def _run(self, job: _Claimed) -> None:
        handler = self._tasks.get(job.task)
        ctx = JobContext(
            job_id=job.id, task=job.task, attempt=job.attempts + 1, fence=job.fence,
            tenant=job.tenant, key=job.key, progress=self._progress,
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
        future = asyncio.ensure_future(handler.func(ctx, *job.args))
        self._inflight.add(future)
        try:
            async with asyncio.timeout(deadline):
                await future
        except asyncio.CancelledError:
            # Not ours: the supervisor is stopping. `asyncio.timeout` re-raises
            # a cancellation it did not cause, so this stays the shutdown path.
            raise
        except TimeoutError:
            # Ours. The handler has already been cancelled by the timeout; the
            # attempt is charged and retried like any other failure, because a
            # deadline miss is usually a slow dependency rather than a bug.
            self.run_timeouts += 1
            await self._fail(
                job,
                f"{job.task!r} timed out after {deadline:g}s and was cancelled",
                handler,
            )
            return
        except Exception as error:  # noqa: BLE001 - handler failures drive retry/dead-letter
            await self._fail(job, repr(error), handler)
            return
        finally:
            self._inflight.discard(future)
        await self._complete(job)

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
        self, job: _Claimed, error: str, handler: _Task | None = None,
        *, permanent: bool = False,
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
                job.id, job.fence, attempts, error[:2000],
            )
            self.dead_lettered += 1
            self._report_terminal(job, "failed", error)
            return
        # A retry is not an ending. Reporting `failed` here would tell a watching
        # client the work is over while the runner is about to try again.
        delay = 0.0
        if handler is not None:
            delay = compute_backoff(
                attempts, kind=handler.backoff_kind, base=handler.backoff_base,
                factor=handler.backoff_factor, cap=handler.backoff_cap,
                jitter=handler.backoff_jitter,
            )
        await self._exec(
            f"UPDATE {self._table} SET state='ready', attempts=$3, "
            "run_at = now() + ($5 || ' seconds')::interval, last_error=$4, "
            "owner=NULL, lease_expiry=NULL, updated_at=now() "
            "WHERE id=$1 AND fence=$2",
            job.id, job.fence, attempts, error[:2000], f"{delay:.3f}",
        )

    async def _complete(self, job: _Claimed) -> None:
        # Fenced: a stale worker whose lease expired (fence bumped by the sweeper)
        # cannot mark someone else's re-claim done.
        await self._exec(
            f"UPDATE {self._table} SET state='done', updated_at=now() "
            "WHERE id=$1 AND fence=$2",
            job.id, job.fence,
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
        sql = (
            f"WITH claimable AS ( SELECT id FROM {self._table} "
            "WHERE queue=$1 AND state='ready' AND run_at <= now() "
            "ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT $2 ) "
            f"UPDATE {self._table} j SET state='leased', owner=$3, "
            "lease_expiry = now() + ($4 || ' seconds')::interval, "
            "fence = j.fence + 1, updated_at=now() FROM claimable c WHERE j.id=c.id "
            "RETURNING j.id, j.task, j.args, j.tenant, "
            "j.attempts, j.max_attempts, j.fence, j.dedup_key"
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
        return _Claimed(
            id=row["id"], task=row["task"], args=list(args or []), tenant=row["tenant"],
            attempts=row["attempts"], max_attempts=row["max_attempts"], fence=row["fence"],
            key=row["dedup_key"],
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
        await self._exec(
            f"UPDATE {self._table} SET "
            "attempts = attempts + 1, "
            "state = CASE WHEN attempts + 1 >= max_attempts THEN 'dead' ELSE 'ready' END, "
            "last_error = COALESCE(last_error, 'lease expired before completion'), "
            "owner=NULL, lease_expiry=NULL, fence=fence+1, updated_at=now() "
            "WHERE queue=$1 AND state='leased' AND lease_expiry < now()",
            self._name,
        )

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
        now = await self._now()
        minute_bucket = now.strftime("%Y%m%d%H%M")
        for sched in self._schedules:
            if sched.cron.matches(
                minute=now.minute, hour=now.hour, day=now.day, month=now.month,
                weekday=now.weekday(),
            ):
                await self.enqueue(
                    sched.task, *sched.args, tenant=sched.tenant,
                    key=f"cron:{sched.task}:{minute_bucket}",
                )

    async def _now(self) -> Any:
        connection = await self._db.acquire(self._workload)
        try:
            value = await connection.fetchval("SELECT (now() AT TIME ZONE 'UTC')")
        finally:
            await self._db.release(self._workload, connection)
        return value

    async def _exec(self, sql: str, *args: Any) -> None:
        connection = await self._db.acquire(self._workload)
        try:
            await connection.execute(sql, *args)
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


