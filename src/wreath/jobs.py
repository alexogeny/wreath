"""Durable, at-least-once background jobs backed by PostgreSQL.

A replacement for Celery/arq for teams already running Postgres: enqueue jobs
(transactionally, in the same commit as your business writes), and a supervised
pool of workers claims them with ``FOR UPDATE SKIP LOCKED`` + fencing tokens,
retries with backoff, and dead-letters on exhaustion. ``NOTIFY`` is used only as
a latency doorbell — correctness never depends on a notification arriving, the
workers also poll.

**Delivery is at-least-once.** A crash between a job's side effect and its
completion ``UPDATE`` yields a re-run on lease expiry. Make handlers idempotent;
pass ``key=`` to :meth:`JobRunner.enqueue` for exactly-once *enqueue* (a unique
index drops duplicates) and use it to guard non-idempotent side effects.

Multi-tenancy note (design 01 §5): jobs live in one dedicated system schema with
a ``tenant`` column, never relying on ``search_path`` for isolation — a
database-global ``NOTIFY`` name would otherwise wake the wrong tenant's workers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ._jobcore import CronSchedule, compute_backoff, dedup_key, validate_identifier

JobHandler = Callable[..., Awaitable[None]]


# The bounded SQL-safe identifier rule lives in ``_jobcore`` so jobs and
# messaging share one definition; kept as a module-local alias for readability.
_validate_identifier = validate_identifier


def _channel(schema: str, queue: str) -> str:
    # A bounded LISTEN/NOTIFY channel shared by producers (pg_notify) and the
    # runner's listen connection. Both derive it identically so the doorbell
    # lines up; identifier-safe and <=63 bytes by construction of its inputs.
    return f"wj_{schema}_{queue}"[:63]


@dataclass(frozen=True, slots=True)
class JobContext:
    """Handed to a job handler as its first argument."""

    job_id: int
    task: str
    attempt: int
    fence: int
    tenant: str
    key: str | None
    #: The runner's :class:`~wreath.progress.ProgressRegistry`, or None.
    progress: Any = None

    @property
    def task_id(self) -> str:
        """This job's progress key. The job id, so there is one identifier."""
        return str(self.job_id)

    def report(self, percent: float, message: str = "") -> None:
        """Tell whoever is watching how far along this job is.

        A no-op when the runner has no progress registry, so a handler can
        report unconditionally. Only *progress* -- the runner sets ``done`` and
        ``failed`` itself, because it is the thing that actually knows whether
        the job finished, is about to retry, or was dead-lettered.
        """
        if self.progress is not None:
            self.progress.report(self.task_id, percent, message)


@dataclass(frozen=True, slots=True)
class TaskHandle:
    """What a caller gets back from :meth:`JobRunner.launch`.

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
    max_attempts: int
    backoff_kind: str
    backoff_base: float
    backoff_factor: float
    backoff_cap: float
    backoff_jitter: float


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

    Obtain via :meth:`wreath.Wreath.jobs`. Register handlers with
    :meth:`task`, enqueue with :meth:`enqueue`, and schedule recurring work with
    :meth:`schedule`. The runner is a supervised service — its workers, sweeper,
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
        self._table = f'"{self._schema}".jobs'
        self._channel = _channel(self._schema, self._name)
        # Runtime (set at start()):
        self._supervisor: Any = None
        self._wake = asyncio.Event()
        self._listen_conn: Any = None
        self._inflight: set[asyncio.Future[Any]] = set()
        self._worker_id = f"{self._name}"

    @property
    def name(self) -> str:
        return self._name

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
    ) -> Callable[[JobHandler], JobHandler]:
        """Decorator registering an async ``handler(ctx, *args)`` under ``name``."""
        _validate_identifier(name, "task name")
        if name in self._tasks:
            raise ValueError(f"duplicate task: {name!r}")
        if retries < 0:
            raise ValueError("retries cannot be negative")

        def register(func: JobHandler) -> JobHandler:
            self._tasks[name] = _Task(
                name=name,
                func=func,
                max_attempts=retries + 1,
                backoff_kind=backoff,
                backoff_base=backoff_base,
                backoff_factor=backoff_factor,
                backoff_cap=backoff_cap,
                backoff_jitter=backoff_jitter,
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
        """Enqueue ``task`` on a cron schedule (UTC). Idempotent across instances.

        Every app instance runs the scheduler, but each minute's enqueue carries a
        deterministic ``key`` so the unique index makes exactly one row win — no
        leader election needed.
        """
        if misfire != "skip":
            raise ValueError("only misfire='skip' is supported in this cut")
        self._schedules.append(
            _Schedule(task=task, cron=CronSchedule(cron), args=tuple(args), tenant=tenant,
                      misfire=misfire)
        )

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
        """Insert a job. Returns its id, or ``None`` if a ``key`` deduplicated it.

        Pass ``tx`` (an open ``connection.transaction()``) to enqueue atomically
        with your business writes — the job becomes visible only if that
        transaction commits (exactly-once *enqueue*). ``key`` sets an idempotency
        key: a second enqueue with the same ``(queue, key)`` is dropped.
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
        """Enqueue ``task`` and return a handle the client can watch.

        The long-mutation shape: a request that cannot finish in a request
        enqueues durable work and hands back an id instead of a timeout::

            @app.post("/herd/imports")
            async def start_import(request, path: str):
                return (await jobs.launch("import_herd", path)).as_dict()

            @app.get("/herd/imports/{task_id}/stream")
            async def watch(request):
                return progress_stream(jobs.progress, request.path_params["task_id"])

        The **job id is the task id**, so there is one identifier rather than
        two to correlate. With a progress registry configured the task is seeded
        as ``queued`` right here, so a client that starts polling immediately
        sees a pending task rather than a 404 it will read as a failure.

        A ``key`` that deduplicates does not lose the caller: the surviving row
        is looked up and its handle returned, so submitting the same work twice
        yields the same task to watch rather than nothing at all.
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
        return TaskHandle(task_id=str(existing), state="running")

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

    def schema_sql(self) -> str:
        """DDL for the jobs table + indexes. Never auto-applied — run it through
        migrations, consistent with the driver's no-implicit-DDL stance."""
        t = self._table
        return (
            f'CREATE SCHEMA IF NOT EXISTS "{self._schema}";\n'
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
            ");\n"
            f"CREATE INDEX IF NOT EXISTS jobs_claim_idx ON {t} (queue, run_at) "
            "WHERE state = 'ready';\n"
            f"CREATE INDEX IF NOT EXISTS jobs_lease_idx ON {t} (lease_expiry) "
            "WHERE state = 'leased';\n"
            f"CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedup_idx ON {t} (queue, dedup_key) "
            "WHERE dedup_key IS NOT NULL;\n"
        )

    # -- supervised service protocol ----------------------------------------

    async def start(self, supervisor: Any) -> None:
        self._supervisor = supervisor
        # A dedicated held connection for the NOTIFY doorbell (never leased back
        # while listening — design 01 §5). Correctness does not depend on it, so
        # a failure to establish it degrades to pure polling.
        with contextlib.suppress(Exception):
            self._listen_conn = await self._db.acquire(self._workload)
            await self._listen_conn.listen(self._channel)
            supervisor.spawn(f"jobs:{self._name}:doorbell", self._doorbell())
        for index in range(self._concurrency):
            supervisor.spawn(f"jobs:{self._name}:worker:{index}", self._worker())
        supervisor.spawn(f"jobs:{self._name}:sweeper", self._sweeper())
        if self._schedules:
            supervisor.spawn(f"jobs:{self._name}:scheduler", self._scheduler())

    async def drain(self, deadline: float) -> None:
        # Stop-fetch is signalled by supervisor.stopping; here we wait for the
        # bounded in-flight handlers to settle, then release the listen conn.
        loop = asyncio.get_running_loop()
        while self._inflight and loop.time() < deadline:
            pending = tuple(self._inflight)
            with contextlib.suppress(Exception):
                await asyncio.wait(pending, timeout=max(0.0, deadline - loop.time()))
        if self._listen_conn is not None:
            with contextlib.suppress(Exception):
                await self._db.release(self._workload, self._listen_conn)
            self._listen_conn = None

    # -- loops ---------------------------------------------------------------

    async def _doorbell(self) -> None:
        # Turn each NOTIFY on our queue channel into a wake for parked workers.
        if self._listen_conn is None:
            return
        with contextlib.suppress(Exception):
            async for _notification in self._listen_conn.notifications():
                self._wake.set()

    async def _worker(self) -> None:
        stopping = self._supervisor.stopping
        while not stopping.is_set():
            try:
                claimed = await self._claim(self._batch)
            except Exception:  # noqa: BLE001 - a transient DB error must not kill the worker
                await self._park()
                continue
            if not claimed:
                await self._park()
                continue
            for job in claimed:
                if stopping.is_set():
                    break
                await self._run(job)

    async def _run(self, job: _Claimed) -> None:
        handler = self._tasks.get(job.task)
        ctx = JobContext(
            job_id=job.id, task=job.task, attempt=job.attempts + 1, fence=job.fence,
            tenant=job.tenant, key=job.key, progress=self._progress,
        )
        if handler is None:
            await self._fail(job, f"no handler registered for task {job.task!r}")
            return
        future = asyncio.ensure_future(handler.func(ctx, *job.args))
        self._inflight.add(future)
        try:
            await future
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - handler failures drive retry/dead-letter
            await self._fail(job, repr(error), handler)
            return
        finally:
            self._inflight.discard(future)
        await self._complete(job)

    async def _fail(self, job: _Claimed, error: str, handler: _Task | None = None) -> None:
        attempts = job.attempts + 1
        max_att = handler.max_attempts if handler is not None else job.max_attempts
        if attempts >= max_att:
            await self._exec(
                f"UPDATE {self._table} SET state='dead', attempts=$3, last_error=$4, "
                "updated_at=now() WHERE id=$1 AND fence=$2",
                job.id, job.fence, attempts, error[:2000],
            )
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
            with contextlib.suppress(Exception):
                # Reclaim expired leases: bump the fence so the previous owner's
                # completion UPDATE (WHERE fence=old) can no longer land.
                await self._exec(
                    f"UPDATE {self._table} SET state='ready', owner=NULL, "
                    "lease_expiry=NULL, fence=fence+1, updated_at=now() "
                    "WHERE state='leased' AND lease_expiry < now()"
                )
            await _sleep_or_stop(stopping, self._lease)

    async def _scheduler(self) -> None:
        stopping = self._supervisor.stopping
        while not stopping.is_set():
            with contextlib.suppress(Exception):
                await self._tick_schedules()
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

    async def _park(self) -> None:
        # Wait for a doorbell wake or the poll timeout, whichever comes first.
        self._wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            async with asyncio.timeout(self._poll):
                await self._wake.wait()


async def _sleep_or_stop(stopping: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        async with asyncio.timeout(seconds):
            await stopping.wait()
