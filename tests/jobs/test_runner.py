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
    # Params: (queue, task, payload, tenant, run_at, max_attempts, dedup_key)
    assert insert[-1] == dedup_key("work", "user-7")


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
