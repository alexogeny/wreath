"""JobRunner unit tests using a fake database (real enqueue/schema paths, no DB)."""

from __future__ import annotations

import pytest
from _pgfidelity import check_statement

from wreath._jobcore import dedup_key
from wreath.jobs import JobRunner, JobVanished


class FakeConnection:
    def __init__(self, fetchval_result=1):
        self.calls: list[tuple[str, tuple]] = []
        self._fetchval_result = fetchval_result
        #: Successive `fetchval` results, when a test needs them to differ.
        self.fetchval_script: list | None = None

    async def execute(self, sql, *args):
        check_statement(sql, args)
        self.calls.append((sql, args))
        return "OK"

    async def fetchval(self, sql, *args):
        check_statement(sql, args)
        self.calls.append((sql, args))
        if self.fetchval_script:
            return self.fetchval_script.pop(0)
        return self._fetchval_result

    async def fetch(self, sql, *args):
        check_statement(sql, args)
        self.calls.append((sql, args))
        return []

    async def fetchrow(self, sql, *args):
        check_statement(sql, args)
        self.calls.append((sql, args))
        return None


class FakeDatabase:
    def __init__(self, fetchval_result=1):
        self.connection = FakeConnection(fetchval_result)
        self.acquired = 0
        self.released = 0

    async def acquire(self, workload):
        self.acquired += 1
        return self.connection

    async def release(self, workload, connection):
        self.released += 1


def _runner(db, **kw):
    return JobRunner(db, name="work", **kw)


def test_construction_validates():
    db = FakeDatabase()
    with pytest.raises(ValueError):
        _runner(db, concurrency=0)
    with pytest.raises(ValueError):
        _runner(db, lease=0)
    with pytest.raises(ValueError):
        JobRunner(db, name="bad name")  # space is not identifier-safe


def test_duplicate_task_rejected():
    runner = _runner(FakeDatabase())

    @runner.task("send")
    async def send(ctx):
        pass

    with pytest.raises(ValueError):
        @runner.task("send")
        async def send2(ctx):
            pass


async def test_enqueue_inserts_and_notifies():
    db = FakeDatabase(fetchval_result=42)
    runner = _runner(db)

    @runner.task("send")
    async def send(ctx, order_id):
        pass

    job_id = await runner.enqueue("send", "order-1")
    assert job_id == 42
    assert db.acquired == 1 and db.released == 1
    sqls = [sql for sql, _ in db.connection.calls]
    assert any("INSERT INTO" in s and "jobs" in s for s in sqls)
    assert any("pg_notify" in s for s in sqls)


async def test_enqueue_unknown_task_raises():
    runner = _runner(FakeDatabase())
    with pytest.raises(KeyError):
        await runner.enqueue("nope")


async def test_enqueue_key_passes_hashed_dedup_key():
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("send")
    async def send(ctx):
        pass

    await runner.enqueue("send", key="user-7")
    insert = next(args for sql, args in db.connection.calls if "INSERT INTO" in sql)
    # Params: (queue, task, payload, tenant, run_at, max_attempts, dedup_key, ...).
    # Indexed from the front deliberately: a new parameter is appended, and
    # indexing from the end made adding one look like a behaviour change.
    assert insert[6] == dedup_key("work", "user-7")


async def test_enqueue_on_transaction_uses_tx_not_pool():
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("send")
    async def send(ctx):
        pass

    tx = FakeConnection(fetchval_result=7)
    job_id = await runner.enqueue("send", tx=tx)
    assert job_id == 7
    assert db.acquired == 0  # rode the caller's transaction
    assert any("pg_notify" in sql for sql, _ in tx.calls)


async def test_deduplicated_launch_returns_the_surviving_handle():
    """The ordinary dedup case: the row the unique index kept is what you watch."""
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("import_herd")
    async def import_herd(ctx):
        pass

    # Insert conflicted (no id returned); the surviving row is job 17.
    db.connection.fetchval_script = [None, 17]
    handle = await runner.launch("import_herd", key="nightly")

    assert handle.task_id == "17"
    assert handle.state == "running"


async def test_launch_never_hands_back_a_stringified_none():
    """The row vanished between the conflict and the read -- a retention sweep of
    completed jobs will do that. There is no id to watch, and `str(None)` would
    hand the client the four-character task id "None": a status endpoint that
    404s and an SSE stream that never terminates."""
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("import_herd")
    async def import_herd(ctx):
        pass

    db.connection.fetchval_script = [None, None]  # conflict, then no surviving row
    with pytest.raises(JobVanished) as raised:
        await runner.launch("import_herd", key="nightly")

    message = str(raised.value)
    assert "nightly" in message and "import_herd" in message


async def test_a_deduplicated_launch_seeds_progress_when_this_worker_has_none():
    """The other route to an unwatchable handle.

    Progress fan-out is at-most-once with no replay, so a worker that started
    after the original `launch` has no registry entry for it. Handing back a
    `running` handle for a task this worker knows nothing about gives a client
    something that 404s on status and hangs on the SSE stream -- the same
    failure as a stringified `None`, arrived at differently.
    """
    from wreath.progress import ProgressRegistry

    registry = ProgressRegistry()
    db = FakeDatabase()
    runner = _runner(db, progress=registry)

    @runner.task("import_herd")
    async def import_herd(ctx):
        pass

    db.connection.fetchval_script = [None, 17]
    handle = await runner.launch("import_herd", key="nightly")

    seeded = registry.get(handle.task_id)
    assert seeded is not None, "the handle points at a task the registry cannot describe"
    assert seeded.state == "running"


async def test_a_deduplicated_launch_never_rewinds_real_progress():
    """...but a worker that *does* know is not reset to zero.

    The original may be at 70% on this very worker; re-seeding unconditionally
    would tell every watching client the import had started over.
    """
    from wreath.progress import ProgressRegistry

    registry = ProgressRegistry()
    registry.report("17", 70.0, "halfway through the herd", state="running")
    db = FakeDatabase()
    runner = _runner(db, progress=registry)

    @runner.task("import_herd")
    async def import_herd(ctx):
        pass

    db.connection.fetchval_script = [None, 17]
    handle = await runner.launch("import_herd", key="nightly")

    live = registry.get(handle.task_id)
    assert live is not None
    assert live.percent == 70.0, "a deduplicated launch rewound the original's progress"
    assert live.message == "halfway through the herd"


async def test_a_pass_that_cannot_be_started_is_counted_not_swallowed():
    """A pass that is never driven does nothing at all, silently.

    `_start_passes` wrapped the whole per-pass body in `suppress(Exception)`, so
    a database that would not answer at startup left the pass unqueued with
    nothing anywhere to say so -- the "nothing is driving this pass" state the
    CLI can print but nothing set.
    """

    class UnreachableDatabase:
        async def acquire(self, workload):
            raise ConnectionError("database is down")

        async def release(self, workload, connection):
            pass

    class Walk:
        name = "purge_replays"
        tenant = ""
        shift = 5.0
        recurring = True
        # A real ChunkedPass has one, and `_record_drive_failure` reads it. The
        # stub omitted it and `suppress(Exception)` swallowed the AttributeError,
        # so this test passed because a programming error was being hidden.
        workload = "write"

        async def status(self, db):
            return None

    runner = _runner(UnreachableDatabase())
    runner.drive(Walk(), cron="*/5 * * * *")

    assert runner.pass_drive_errors == 0
    await runner._start_passes()
    assert runner.pass_drive_errors == 1, "a pass that never started was not counted"


async def test_one_unstartable_pass_does_not_strand_the_others():
    """Isolation is per pass, not around the loop."""
    db = FakeDatabase()
    runner = _runner(db)

    class Ledger:
        """Enough of a real ledger for `_record_drive_failure` to reach."""

        def __init__(self):
            self.marked = []

        async def mark_driven(self, connection, *, error=None):
            self.marked.append(error)

    class Walk:
        def __init__(self, name, ok):
            self.name = name
            self.tenant = ""
            self.workload = "write"
            self.shift = 5.0
            self.recurring = True
            self.ledger = Ledger()
            self._ok = ok

        async def status(self, db):
            if not self._ok:
                raise ConnectionError("this pass's ledger read failed")
            return None

    runner.drive(Walk("broken", ok=False), cron="*/5 * * * *")
    runner.drive(Walk("healthy", ok=True), cron="*/5 * * * *")

    await runner._start_passes()

    assert runner.pass_drive_errors == 1
    enqueued = [sql for sql, _ in db.connection.calls if "INSERT INTO" in sql]
    assert enqueued, "the healthy pass was stranded by the broken one"


def test_schema_sql_has_table_and_indexes():
    sql = _runner(FakeDatabase()).schema_sql()
    assert "CREATE TABLE IF NOT EXISTS" in sql and ".jobs" in sql
    assert "jobs_claim_idx" in sql
    assert "jobs_lease_idx" in sql
    assert "jobs_dedup_idx" in sql
    assert "FOR UPDATE SKIP LOCKED" not in sql  # DDL only


def test_schedule_registration_and_validation():
    runner = _runner(FakeDatabase())

    @runner.task("rollup")
    async def rollup(ctx):
        pass

    runner.schedule("rollup", cron="0 3 * * *")
    with pytest.raises(ValueError):
        runner.schedule("rollup", cron="0 3 * * *", misfire="fire")


# --- a job enqueued by an older release (design 22 item 10) ------------------


def _stale_job(**kw):
    from wreath.jobs import _Claimed

    defaults = dict(
        id=7, task="send_receipt", args=["o-1", "plain"], tenant=None,
        attempts=0, max_attempts=3, fence=1, key=None,
    )
    return _Claimed(**{**defaults, **kw})


async def test_args_that_no_longer_bind_dead_letter_instead_of_escaping():
    """A signature change between releases must not kill the worker silently.

    Binding used to happen inside the call that builds the coroutine, which sat
    *outside* `_run`'s try -- so the TypeError escaped `_run` entirely: no
    `_fail`, no `last_error`, no backoff. The job stayed leased until the
    sweeper reclaimed it and eventually recorded "lease expired before
    completion", which points at a hung handler rather than a stale enqueue.
    """
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("send_receipt")
    async def send_receipt(ctx, order_id, template, locale):   # three, not two
        pass

    await runner._run(_stale_job())

    statements = [sql for sql, _ in db.connection.calls]
    assert statements, "the job was never marked failed, retried, or dead-lettered"
    assert "state='dead'" in statements[0]
    assert runner.dead_lettered == 1


async def test_a_stale_enqueue_names_the_arity_and_does_not_retry():
    db = FakeDatabase()
    runner = _runner(db, )

    @runner.task("send_receipt")
    async def send_receipt(ctx, order_id, template, locale):
        pass

    await runner._run(_stale_job())

    _, args = db.connection.calls[0]
    message = args[-1]
    assert "send_receipt" in message
    assert "2 argument(s)" in message
    assert "locale" in message
    assert "retrying cannot fix it" in message
    # Attempt 1 of 3, yet dead: a binding failure will not bind on the fourth try.
    assert args[2] == 1


async def test_a_handler_that_raises_still_retries_normally():
    """The permanent path must not swallow ordinary failures."""
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("send_receipt")
    async def send_receipt(ctx, order_id, template):
        raise RuntimeError("smtp down")

    await runner._run(_stale_job())

    statements = [sql for sql, _ in db.connection.calls]
    assert "state='ready'" in statements[0], "a transient failure must retry"
    assert runner.dead_lettered == 0


# --- `drive`: the shift handler that keeps an online pass moving ---------------
#
# `jobs.drive(pass)` is how the purge and rewrite passes behind `session_store`,
# `webhooks`, `middleware/idempotency` and `middleware/ratelimit` make progress,
# and the closure it registers had never been executed by a test: `_start_passes`
# was covered, but nothing ever ran the shift it enqueues. Every branch that
# decides whether a pass continues, halts or fails was `unreached` -- a pass that
# silently stopped after one chunk would have looked exactly like a passing suite.


class _Result:
    """A `run_shift` result, duck-typed as `ChunkedPass.run_shift` returns one."""

    def __init__(self, *, rows=10, chunks=1, error=None, stopped=None,
                 should_continue=False, complete=True):
        self.rows = rows
        self.chunks = chunks
        self.error = error
        self.stopped = stopped
        self.should_continue = should_continue
        self.complete = complete


class _Ledger:
    def __init__(self):
        self.marked = []

    async def mark_driven(self, connection, *, error=None):
        self.marked.append(error)


class _DrivenWalk:
    """Enough of a `ChunkedPass` for `drive` to register and run a shift."""

    def __init__(self, result, *, name="purge_replays", tenant="", recurring=False,
                 shift=5.0):
        self.name = name
        self.tenant = tenant
        self.workload = "write"
        self.shift = shift
        self.recurring = recurring
        self.ledger = _Ledger()
        self._result = result
        self.stopping_seen = []

    async def status(self, db):
        return None

    async def run_shift(self, db, *, stopping=None):
        self.stopping_seen.append(stopping)
        return self._result


def _shift_handler(runner, walk, cron=None):
    """Register `walk` and hand back the closure `drive` built for it."""
    task = runner.drive(walk, cron=cron)
    return task, runner._tasks[task].func


def _ctx(task):
    from wreath.jobs import JobContext

    return JobContext(job_id=1, task=task, attempt=1, fence=1, tenant="", key=None)


async def test_a_shift_with_more_to_do_enqueues_the_next_one():
    db = FakeDatabase()
    runner = _runner(db)
    walk = _DrivenWalk(_Result(chunks=3, complete=False, should_continue=True))
    task, handler = _shift_handler(runner, walk)

    await handler(_ctx(task))

    inserts = [args for sql, args in db.connection.calls if "INSERT INTO" in sql]
    assert len(inserts) == 1, "the next shift was not queued; the pass would stop here"
    assert walk.ledger.marked == [None], walk.ledger.marked  # stamped before the work


async def test_a_shift_that_made_chunks_but_is_not_complete_continues_too():
    """`result.chunks and not result.complete`, the arm `should_continue` does not cover."""
    db = FakeDatabase()
    runner = _runner(db)
    walk = _DrivenWalk(_Result(chunks=2, complete=False, should_continue=False))
    task, handler = _shift_handler(runner, walk)

    await handler(_ctx(task))

    assert [sql for sql, _ in db.connection.calls if "INSERT INTO" in sql]


async def test_a_finished_shift_queues_nothing_further():
    db = FakeDatabase()
    runner = _runner(db)
    walk = _DrivenWalk(_Result(chunks=1, complete=True, should_continue=False))
    task, handler = _shift_handler(runner, walk)

    await handler(_ctx(task))

    assert not [sql for sql, _ in db.connection.calls if "INSERT INTO" in sql], \
        "a complete pass re-enqueued itself, which is a loop with no end"


async def test_a_shift_that_made_no_chunks_queues_nothing_further():
    """Zero chunks and incomplete is a pass with nothing to do, not one to spin on."""
    db = FakeDatabase()
    runner = _runner(db)
    walk = _DrivenWalk(_Result(chunks=0, complete=False, should_continue=False))
    task, handler = _shift_handler(runner, walk)

    await handler(_ctx(task))

    assert not [sql for sql, _ in db.connection.calls if "INSERT INTO" in sql]


async def test_a_blocked_shift_returns_without_re_enqueueing():
    """Re-enqueuing a halted pass turns `halt` back into the retry loop it refuses."""
    db = FakeDatabase()
    runner = _runner(db)
    walk = _DrivenWalk(_Result(
        chunks=2, complete=False, should_continue=True, stopped="blocked",
    ))
    task, handler = _shift_handler(runner, walk)

    await handler(_ctx(task))  # not an error: halting is a decision, not a failure

    assert not [sql for sql, _ in db.connection.calls if "INSERT INTO" in sql], \
        "a blocked pass was re-enqueued, so halting would retry forever"


async def test_a_failed_chunk_raises_so_the_retry_machinery_sees_it():
    db = FakeDatabase()
    runner = _runner(db)
    walk = _DrivenWalk(_Result(error=RuntimeError("deadlock"), stopped="failed"))
    task, handler = _shift_handler(runner, walk)

    with pytest.raises(RuntimeError, match="purge_replays.*chunk failed"):
        await handler(_ctx(task))


async def test_a_chunk_error_without_a_failed_verdict_is_not_raised():
    """Both halves are required: an error the pass did not stop for is not a failure.

    A pass that recorded an error and carried on (a retried chunk, say) reports
    `error` without `stopped == "failed"`, and raising there would dead-letter a
    shift that is still making progress.
    """
    db = FakeDatabase()
    runner = _runner(db)
    walk = _DrivenWalk(_Result(
        error=RuntimeError("transient"), stopped=None, chunks=1, complete=False,
    ))
    task, handler = _shift_handler(runner, walk)

    await handler(_ctx(task))  # no raise

    assert [sql for sql, _ in db.connection.calls if "INSERT INTO" in sql]


async def test_a_shift_passes_the_supervisors_stopping_flag_to_the_pass():
    """A shift has to notice a shutdown mid-chunk; without a supervisor it gets None."""
    import asyncio as _asyncio

    class Supervisor:
        def __init__(self):
            self.stopping = _asyncio.Event()

    runner = _runner(FakeDatabase())
    walk = _DrivenWalk(_Result())
    task, handler = _shift_handler(runner, walk)

    await handler(_ctx(task))
    assert walk.stopping_seen == [None]  # never started, so no supervisor

    supervisor = Supervisor()
    runner._supervisor = supervisor
    await handler(_ctx(task))
    assert walk.stopping_seen[-1] is supervisor.stopping


async def test_a_recurring_pass_must_be_given_a_schedule():
    """Nothing else would start the next cycle, and stopping quietly is worse."""
    runner = _runner(FakeDatabase())
    with pytest.raises(ValueError, match="re-derived frontier"):
        runner.drive(_DrivenWalk(_Result(), name="cycles", recurring=True))

    # ... and a pass that is *not* recurring needs no schedule.
    runner.drive(_DrivenWalk(_Result(), name="one_shot", recurring=False))


async def test_a_pass_whose_shift_could_outlast_the_lease_is_refused():
    runner = _runner(FakeDatabase(), lease=30.0)
    with pytest.raises(ValueError, match="statement_timeout < within < shift < lease"):
        runner.drive(_DrivenWalk(_Result(), name="slow", shift=30.0))


async def test_the_same_pass_cannot_be_driven_twice_by_one_runner():
    runner = _runner(FakeDatabase())
    runner.drive(_DrivenWalk(_Result(), name="purge"))
    with pytest.raises(ValueError, match="already driven"):
        runner.drive(_DrivenWalk(_Result(), name="purge"))


def test_a_pass_task_name_keeps_the_tenant_that_scopes_it():
    """Two tenants' copies of one pass are two tasks; one name would collide."""
    from wreath.jobs import _pass_task_name

    assert _pass_task_name("purge replays", "acme") == "pass_purge_replays_acme"
    assert _pass_task_name("purge replays", "") == "pass_purge_replays"


# --- declaration refusals, none of which had ever been triggered --------------

def test_the_runner_refuses_a_batch_below_one():
    with pytest.raises(ValueError, match="batch must be >= 1"):
        _runner(FakeDatabase(), batch=0)


def test_the_runner_names_which_of_the_two_intervals_it_refuses():
    """`lease` and `poll_interval` share a guard, so each must be shown to reach it."""
    for kw in ({"lease": 0}, {"poll_interval": 0}, {"poll_interval": -1}):
        with pytest.raises(ValueError, match="lease and poll_interval must be positive"):
            _runner(FakeDatabase(), **kw)


def test_a_task_refuses_negative_retries_and_a_non_positive_timeout():
    runner = _runner(FakeDatabase())
    with pytest.raises(ValueError, match="retries cannot be negative"):
        runner.task("a", retries=-1)
    with pytest.raises(ValueError, match="timeout must be positive"):
        runner.task("b", timeout=0)
    with pytest.raises(ValueError, match="timeout must be positive"):
        runner.task("c", timeout=-5)


def test_a_task_name_that_is_not_an_identifier_is_refused_at_declaration():
    """The name is interpolated into SQL, so it is checked where it is declared."""
    runner = _runner(FakeDatabase())
    with pytest.raises(ValueError, match="task name"):
        runner.task("send email")
    with pytest.raises(ValueError, match="task name"):
        runner.task('send"; drop table jobs --')


async def test_enqueueing_an_unregistered_task_says_which_task_and_how_to_fix_it():
    """A bare `KeyError` from the dict lookup below reads the same to `except`, not to a person."""
    runner = _runner(FakeDatabase())
    with pytest.raises(KeyError, match="unknown task"):
        await runner.enqueue("nope")
    with pytest.raises(KeyError, match="register with @runner.task"):
        await runner.enqueue("nope")


async def test_an_explicit_max_attempts_overrides_the_tasks_own():
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("send", retries=4)
    async def send(ctx):
        pass

    await runner.enqueue("send")
    await runner.enqueue("send", max_attempts=1)
    inserts = [args for sql, args in db.connection.calls if "INSERT INTO" in sql]
    # Index 5 is max_attempts; see the note on front-indexing above.
    assert [args[5] for args in inserts] == [5, 1]  # retries + 1, then the override


async def test_a_failed_verdict_without_an_error_is_not_a_failure():
    """Both clauses are required, and `stopped == "failed"` alone is not enough.

    A pass reports the verdict and the error separately; raising on the verdict
    alone would dead-letter a shift whose error is `None`, and the message would
    have nothing to name.
    """
    db = FakeDatabase()
    runner = _runner(db)
    walk = _DrivenWalk(_Result(error=None, stopped="failed", chunks=1, complete=True))
    task, handler = _shift_handler(runner, walk)

    await handler(_ctx(task))  # no raise


async def test_a_pass_asking_to_continue_is_believed_even_with_nothing_to_show():
    """`should_continue` alone, with no chunks and complete: the first shift of a
    pass whose frontier is empty this cycle still has to hand off to the next."""
    db = FakeDatabase()
    runner = _runner(db)
    walk = _DrivenWalk(_Result(chunks=0, complete=True, should_continue=True))
    task, handler = _shift_handler(runner, walk)

    await handler(_ctx(task))

    assert [sql for sql, _ in db.connection.calls if "INSERT INTO" in sql], \
        "a pass that asked to continue was dropped"


async def test_driving_with_a_cron_registers_the_schedule_that_starts_each_cycle():
    runner = _runner(FakeDatabase())
    assert not runner._schedules
    task = runner.drive(_DrivenWalk(_Result(), name="cycles", recurring=True),
                        cron="*/5 * * * *")
    assert [s.task for s in runner._schedules] == [task], \
        "nothing would start the next cycle"


async def test_a_drive_failure_is_recorded_on_the_ledger_as_its_repr():
    """The ledger is the only place that survives the process, and `wreath passes
    status` is where somebody looks -- so the error has to arrive as text, and a
    successful drive has to arrive as nothing rather than as the string "None"."""
    runner = _runner(FakeDatabase())
    walk = _DrivenWalk(_Result())

    await runner._record_drive_failure(walk, RuntimeError("ledger read failed"))
    await runner._record_drive_failure(walk, None)

    assert walk.ledger.marked == ["RuntimeError('ledger read failed')", None]


async def test_the_scheduler_enqueues_only_the_minute_its_cron_matches():
    """`_tick_schedules` had never run: a schedule that never fires and one that
    fires every tick are the same passing suite without this."""
    from datetime import datetime

    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("nightly")
    async def nightly(ctx):
        pass

    runner.schedule("nightly", cron="30 2 * * *")

    db.connection.fetchval_script = [datetime(2026, 7, 30, 2, 30)]
    await runner._tick_schedules()
    inserts = [args for sql, args in db.connection.calls if "INSERT INTO" in sql]
    assert len(inserts) == 1, "the schedule did not fire on its own minute"

    db.connection.calls.clear()
    db.connection.fetchval_script = [datetime(2026, 7, 30, 3, 30)]
    await runner._tick_schedules()
    assert not [sql for sql, _ in db.connection.calls if "INSERT INTO" in sql], \
        "the schedule fired on a minute its cron does not match"


# --- the worker loop, the claim bookkeeping, and the failure path --------------

class _Progress:
    """Enough of a `ProgressRegistry` for `_report_terminal`."""

    def __init__(self, percent=None):
        self.reports = []
        self._percent = percent

    def report(self, key, percent, message="", *, state=None, error=None):
        self.reports.append((key, percent, state, error))

    def get(self, key):
        if self._percent is None:
            return None
        return type("Snapshot", (), {"percent": self._percent})()


async def test_a_dead_lettered_job_reports_where_it_stopped_not_a_hundred():
    """Only `done` is 100%. Reporting a failure at 100 tells a watching client the
    work finished, which is the opposite of what happened."""
    progress = _Progress(percent=42.0)
    runner = _runner(FakeDatabase(), progress=progress)

    await runner._fail(_stale_job(), "smtp down", permanent=True)
    assert progress.reports == [("7", 42.0, "failed", "smtp down")]

    progress.reports.clear()
    await runner._complete(_stale_job())
    assert progress.reports == [("7", 100, "done", None)]


async def test_a_job_that_reported_nothing_is_dead_lettered_at_zero():
    progress = _Progress(percent=None)  # nothing ever reported
    runner = _runner(FakeDatabase(), progress=progress)
    await runner._fail(_stale_job(), "boom", permanent=True)
    assert progress.reports == [("7", 0.0, "failed", "boom")]


async def test_cancelling_reports_terminal_progress_when_configured(monkeypatch):
    progress = _Progress(percent=42.0)
    runner = _runner(FakeDatabase(), progress=progress)

    async def cancelled_rows(*args, **kwargs):
        return [{"id": 7}]

    monkeypatch.setattr(runner, "_fetch", cancelled_rows)

    assert await runner.cancel(7, reason="operator stopped it") is True
    assert progress.reports == [("7", 42.0, "failed", "operator stopped it")]


async def test_cancelling_without_progress_still_succeeds(monkeypatch):
    runner = _runner(FakeDatabase())

    async def cancelled_rows(*args, **kwargs):
        return [{"id": 7}]

    monkeypatch.setattr(runner, "_fetch", cancelled_rows)

    assert await runner.cancel(7) is True
    assert runner.cancelled == 1


async def test_a_failure_with_no_handler_falls_back_to_the_rows_own_budget():
    """`_fail` is reached with `handler=None` when the task is not registered here
    -- another release's task, or one removed since the row was enqueued."""
    db = FakeDatabase()
    runner = _runner(db)

    # attempts=2 of the row's own max_attempts=3: one more to go, so a retry.
    await runner._fail(_stale_job(attempts=1, max_attempts=3), "transient")
    assert "state='ready'" in db.connection.calls[0][0]

    db.connection.calls.clear()
    # attempts=3 of 3: the row's budget is spent, so dead rather than retried.
    await runner._fail(_stale_job(attempts=2, max_attempts=3), "transient")
    assert "state='dead'" in db.connection.calls[0][0]


async def test_a_retry_with_no_handler_waits_no_backoff_it_cannot_compute():
    """The backoff shape lives on the handler; with none there is nothing to read,
    and inventing one would delay a job by a policy nobody declared."""
    db = FakeDatabase()
    runner = _runner(db)

    await runner._fail(_stale_job(attempts=0, max_attempts=3), "transient")
    delay = db.connection.calls[0][1][-1]
    assert delay == "0.000", delay

    db.connection.calls.clear()
    runner_with = _runner(FakeDatabase())

    @runner_with.task("send_receipt", backoff_base=2.0, backoff_jitter=0.0)
    async def send_receipt(ctx, order_id, template):
        pass

    handler = runner_with._tasks["send_receipt"]
    await runner._fail(_stale_job(attempts=0, max_attempts=3), "transient", handler)
    assert float(db.connection.calls[0][1][-1]) > 0.0, "the declared backoff was not applied"


async def test_a_handlers_attempt_budget_wins_over_the_rows():
    """A row enqueued before `retries` was lowered must respect the current handler."""
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("send_receipt", retries=0)  # max_attempts == 1
    async def send_receipt(ctx, order_id, template):
        pass

    handler = runner._tasks["send_receipt"]
    await runner._fail(_stale_job(attempts=0, max_attempts=99), "smtp down", handler)
    assert "state='dead'" in db.connection.calls[0][0], \
        "the row's stale budget was used instead of the handler's"


def test_discarding_a_claim_removes_that_job_and_not_merely_the_first():
    """`_release_unstarted` hands back whatever is still listed, so a wrong removal
    hands back a job that is already running -- two workers, one job."""
    runner = _runner(FakeDatabase())
    first, second = _stale_job(id=1), _stale_job(id=2)
    runner._claimed_not_started.extend([first, second])

    runner._discard_claim(second)
    assert runner._claimed_not_started == [first]

    runner._discard_claim(first)
    assert runner._claimed_not_started == []


def test_a_claim_row_with_no_args_becomes_an_empty_argument_list():
    """`args` is JSONB and comes back `None` for a job enqueued with none."""
    from wreath.jobs import JobRunner

    claim = JobRunner._row_to_claim({
        "id": 3, "task": "t", "args": None, "tenant": None,
        "attempts": 0, "max_attempts": 3, "fence": 1, "dedup_key": None,
    })
    assert claim.args == []

    text = JobRunner._row_to_claim({
        "id": 3, "task": "t", "args": '["a", 1]', "tenant": None,
        "attempts": 0, "max_attempts": 3, "fence": 1, "dedup_key": None,
    })
    assert text.args == ["a", 1]  # a driver that hands back JSON as text


async def test_a_worker_with_nothing_to_claim_waits_instead_of_re_querying():
    """The park is the difference between polling and hammering the database.

    Without it the loop re-issues the claim as fast as the event loop will let
    it -- which is not a hang a test can see, so this bounds the number of
    queries rather than the time.
    """
    import asyncio as _asyncio

    class EmptyDatabase(FakeDatabase):
        def __init__(self, stopping):
            super().__init__()
            self.fetches = 0
            self.hammered = False
            self._stopping = stopping

        async def acquire(self, workload):
            self.fetches += 1
            if self.fetches > 30:
                self.hammered = True
                self._stopping.set()
            return self.connection

    stopping = _asyncio.Event()
    db = EmptyDatabase(stopping)
    runner = _runner(db, poll_interval=30.0)  # long enough that a park cannot expire
    wake = runner._new_waiter()

    async def stop_soon():
        await _asyncio.sleep(0.05)
        stopping.set()
        wake.set()  # ring the doorbell so the park returns at once

    await _asyncio.gather(runner._work(stopping, wake), stop_soon())

    assert not db.hammered, f"the idle worker issued {db.fetches} claims in 50ms"


async def test_a_worker_asked_to_stop_does_not_start_the_rest_of_its_batch():
    """A batch is claimed together and run one at a time; a stop between two jobs
    must leave the rest for `_release_unstarted` rather than running them anyway.

    The claim is served once and then bounded: a worker that never reaches the
    handler never sets `stopping` either, and this loop would run until the
    mutation deadline instead of failing. The bound is on the number of claims
    rather than on time, so it decides the same way on a slow machine.
    """
    import asyncio as _asyncio

    stopping = _asyncio.Event()
    ran = []

    class BatchDatabase(FakeDatabase):
        def __init__(self):
            super().__init__()
            self.served = False
            self.claims = 0

        async def acquire(self, workload):
            return self.connection

    db = BatchDatabase()
    rows = [
        {"id": i, "task": "send", "args": [], "tenant": None,
         "attempts": 0, "max_attempts": 3, "fence": 1, "dedup_key": None}
        for i in (1, 2, 3)
    ]

    async def fetch(sql, *args):
        db.connection.calls.append((sql, args))
        if "claimable" not in sql:
            return []
        db.claims += 1
        if db.claims > 10:
            stopping.set()  # the handler never got there; stop anyway
            return []
        if not db.served:
            db.served = True
            return rows
        return []

    db.connection.fetch = fetch
    # A short poll so the bound above is reached promptly when the batch is
    # never run; the passing path never parks at all.
    runner = _runner(db, batch=3, poll_interval=0.01)
    wake = runner._new_waiter()

    @runner.task("send")
    async def send(ctx):
        ran.append(ctx.job_id)
        stopping.set()  # a shutdown arrives while the first job is running
        wake.set()

    await runner._work(stopping, wake)

    assert ran == [1], f"ran {ran} after a stop was requested"
    assert len(runner._claimed_not_started) == 2, "the rest of the batch was not left claimed"


async def test_draining_gives_up_at_its_deadline_rather_than_waiting_out_a_hung_handler():
    """`drain` is called with a deadline by the supervisor; a handler that never
    returns must not hold the shutdown open past it."""
    import asyncio as _asyncio

    runner = _runner(FakeDatabase())
    loop = _asyncio.get_running_loop()
    forever = loop.create_task(_asyncio.Event().wait())
    runner._inflight.add(forever)
    try:
        # A deadline already in the past: one pass through the loop, then out.
        await _asyncio.wait_for(runner.drain(loop.time() - 1.0), timeout=1.0)
    finally:
        forever.cancel()


# --- priority and coalescing ----------------------------------------------------------


async def test_priority_rides_the_row_and_orders_the_claim():
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("send")
    async def send(ctx):
        pass

    await runner.enqueue("send", priority=7)
    insert = next(args for sql, args in db.connection.calls if "INSERT INTO" in sql)
    assert insert[7] == 7


async def test_the_claim_is_ordered_by_priority_then_run_at():
    db = FakeDatabase()
    runner = _runner(db)
    await runner._claim(1)
    claim = next(sql for sql, _ in db.connection.calls if "FOR UPDATE SKIP LOCKED" in sql)
    assert "ORDER BY priority DESC, run_at" in claim


async def test_the_default_priority_is_zero_so_nothing_reorders_itself():
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("send")
    async def send(ctx):
        pass

    await runner.enqueue("send")
    insert = next(args for sql, args in db.connection.calls if "INSERT INTO" in sql)
    assert insert[7] == 0


async def test_a_repeated_key_drops_by_default():
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("send")
    async def send(ctx):
        pass

    await runner.enqueue("send", key="k")
    sql = next(sql for sql, _ in db.connection.calls if "INSERT INTO" in sql)
    assert "DO NOTHING" in sql


async def test_coalesce_merges_instead_of_dropping():
    # The second call carries something the first did not: newest arguments,
    # earliest run time, highest priority -- all in the one statement, because
    # a read-then-write lets two callers interleave and lose an update.
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("send")
    async def send(ctx):
        pass

    await runner.enqueue("send", key="k", coalesce=True)
    sql = next(sql for sql, _ in db.connection.calls if "INSERT INTO" in sql)
    assert "DO UPDATE SET args = excluded.args" in sql
    assert "run_at = LEAST(j.run_at, excluded.run_at)" in sql
    assert "priority = GREATEST(j.priority, excluded.priority)" in sql


async def test_coalesce_only_touches_a_row_still_waiting():
    # A leased job is being worked; rewriting its arguments underneath the
    # worker would hand it inputs it never read.
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("send")
    async def send(ctx):
        pass

    await runner.enqueue("send", key="k", coalesce=True)
    sql = next(sql for sql, _ in db.connection.calls if "INSERT INTO" in sql)
    assert "WHERE j.state = 'ready'" in sql


async def test_coalesce_without_a_key_is_refused():
    # There is nothing to coalesce onto, and a silent no-op would read as a
    # merge that happened.
    db = FakeDatabase()
    runner = _runner(db)

    @runner.task("send")
    async def send(ctx):
        pass

    with pytest.raises(ValueError, match="coalesce= needs a key"):
        await runner.enqueue("send", coalesce=True)


def test_the_claim_index_leads_with_priority():
    # An index on (queue, run_at) cannot serve an ORDER BY that leads with
    # priority, so the step replaces it rather than adding beside it.
    db = FakeDatabase()
    statements = " ".join(
        statement for step in _runner(db).component().steps for statement in step.statements
    )
    assert "(queue, priority DESC, run_at) WHERE state = 'ready'" in statements
    assert "DROP INDEX IF EXISTS" in statements


# --- worker identity ------------------------------------------------------------------


def test_two_runners_on_one_queue_have_distinct_owners():
    """`owner` used to be the queue name, so every worker on a queue shared it.

    Correctness never depended on it -- the fence is what stops a superseded
    worker's bookkeeping landing -- but it meant `owner` answered "which queue",
    a question the `queue` column already answers, and an incident could not ask
    which *process* held a job. Nothing read it back, which is why that went
    unnoticed for as long as it did.
    """
    first = _runner(FakeDatabase())
    second = _runner(FakeDatabase())
    assert first._worker_id != second._worker_id


def test_a_worker_id_still_names_its_queue():
    # A human reading a leased row should see which queue it belongs to without
    # a join, which is why the identity is prefixed rather than opaque.
    runner = _runner(FakeDatabase())
    assert runner._worker_id.startswith("work:")


async def test_the_claim_records_the_worker_that_took_it():
    db = FakeDatabase()
    runner = _runner(db)
    await runner._claim(1)
    claim = next(
        (sql, args) for sql, args in db.connection.calls if "FOR UPDATE SKIP LOCKED" in sql
    )
    assert runner._worker_id in claim[1]
