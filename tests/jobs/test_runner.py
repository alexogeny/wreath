"""JobRunner unit tests using a fake database (real enqueue/schema paths, no DB)."""

from __future__ import annotations

import pytest

from wreath._jobcore import dedup_key
from wreath.jobs import JobRunner


class FakeConnection:
    def __init__(self, fetchval_result=1):
        self.calls: list[tuple[str, tuple]] = []
        self._fetchval_result = fetchval_result

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "OK"

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetchval_result

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return []

    async def fetchrow(self, sql, *args):
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
