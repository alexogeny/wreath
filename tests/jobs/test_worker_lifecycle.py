"""Worker state-machine coverage (claim/complete/fail/run) over a fake driver.

These exercise the fencing + retry/dead-letter branches a real DB would
otherwise be needed for, by driving the JobRunner methods directly against a
connection that records SQL and returns canned RETURNING rows. The end-to-end
integration (real SKIP-LOCKED contention, lease expiry) stays DSN-gated.
"""

from __future__ import annotations

import json
from typing import Any

from _pgfidelity import check_for

from wreath.jobs import JobRunner, _Claimed


class FakeConn:
    def __init__(self, *, fetch: list[Any] | None = None, fetchval: Any = 1) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False
        self._fetch = list(fetch) if fetch is not None else []
        self._fetchval = fetchval

    async def execute(self, sql: str, *args: Any) -> str:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return self._fetch

    async def fetchval(self, sql: str, *args: Any) -> Any:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return self._fetchval

    def sqls(self) -> list[str]:
        return [sql for sql, _ in self.calls]


class FakeDB:
    name = "main"

    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self.acquired = 0
        self.released = 0

    async def acquire(self, workload: str) -> FakeConn:
        self.acquired += 1
        return self.conn

    async def release(self, workload: str, connection: FakeConn) -> None:
        self.released += 1


def _runner(conn: FakeConn, **kw: Any) -> JobRunner:
    return JobRunner(FakeDB(conn), name="work", **kw)


def _job(**kw: Any) -> _Claimed:
    base = dict(id=5, task="t", args=[], tenant="", attempts=0, max_attempts=6,
                fence=1, key=None)
    base.update(kw)
    return _Claimed(**base)  # type: ignore[arg-type]


async def test_claim_parses_rows_and_emits_skip_locked_with_fence_bump() -> None:
    row = {"id": 5, "task": "send", "args": json.dumps(["o1"]), "tenant": "",
           "attempts": 0, "max_attempts": 6, "fence": 3, "dedup_key": None}
    conn = FakeConn(fetch=[row])
    claimed = await _runner(conn)._claim(1)
    assert len(claimed) == 1
    c = claimed[0]
    assert (c.id, c.task, c.args, c.fence) == (5, "send", ["o1"], 3)
    claim_sql = conn.sqls()[0]
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "fence = j.fence + 1" in claim_sql


async def test_complete_is_fenced() -> None:
    conn = FakeConn()
    await _runner(conn)._complete(_job(id=5, fence=3))
    sql, args = conn.calls[-1]
    assert "state='done'" in sql and "WHERE id=$1 AND fence=$2" in sql
    assert args == (5, 3)


async def test_fail_retries_with_backoff_before_the_cap() -> None:
    conn = FakeConn()
    runner = _runner(conn)

    @runner.task("t", retries=5)
    async def t(ctx: Any) -> None:
        pass

    await runner._fail(_job(max_attempts=6, fence=1), "boom", runner._tasks["t"])
    sql, args = conn.calls[-1]
    assert "state='ready'" in sql and "seconds')::interval" in sql
    assert args[0] == 5 and args[1] == 1 and args[2] == 1  # id, fence, attempts


async def test_fail_dead_letters_when_attempts_exhausted() -> None:
    conn = FakeConn()
    runner = _runner(conn)

    @runner.task("t", retries=0)  # max_attempts == 1
    async def t(ctx: Any) -> None:
        pass

    await runner._fail(_job(max_attempts=1, fence=1), "boom", runner._tasks["t"])
    assert "state='dead'" in conn.calls[-1][0]


async def test_run_success_completes_the_job() -> None:
    conn = FakeConn()
    runner = _runner(conn)
    seen: list[int] = []

    @runner.task("t")
    async def t(ctx: Any, x: int) -> None:
        seen.append(x)

    await runner._run(_job(task="t", args=[9], fence=0))
    assert seen == [9]
    assert "state='done'" in conn.calls[-1][0]


async def test_run_handler_error_retries_and_never_completes() -> None:
    conn = FakeConn()
    runner = _runner(conn)

    @runner.task("t", retries=5)
    async def t(ctx: Any) -> None:
        raise RuntimeError("nope")

    await runner._run(_job(task="t", max_attempts=6, fence=0))
    assert "state='ready'" in conn.calls[-1][0]
    assert all("state='done'" not in s for s in conn.sqls())


async def test_run_unregistered_task_dead_letters() -> None:
    conn = FakeConn()
    await _runner(conn)._run(_job(task="ghost", max_attempts=1))
    assert "state='dead'" in conn.calls[-1][0]


# --- runtime bounds ----------------------------------------------------------
# A handler that outruns its lease is reclaimed by the sweeper *while it is still
# running*, so a second worker starts a concurrent copy and the first one's
# fenced completion lands on nothing. `drive()` already refuses a pass whose
# shift could outlast the lease; an ordinary task had no such bound at all.


async def test_a_handler_that_outruns_its_deadline_is_cancelled_and_retried() -> None:
    import asyncio

    conn = FakeConn()
    runner = _runner(conn, lease=1.0)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    @runner.task("slow", timeout=0.05)
    async def slow(ctx: Any) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    await runner._run(_job(task="slow", max_attempts=6, fence=0))
    assert started.is_set()
    assert cancelled.is_set(), "the handler was left running after its deadline"
    # Retried like any other failure, with the reason legible in last_error.
    failure = [args for sql, args in conn.calls if "last_error" in sql]
    assert failure and "timed out" in str(failure[0])
    assert not any("state='done'" in sql for sql in conn.sqls())
    assert runner.stats()["run_timeouts"] == 1


async def test_a_handler_deadline_defaults_to_inside_the_lease() -> None:
    """A default that could exceed the lease would reintroduce the duplicate."""
    conn = FakeConn()
    runner = _runner(conn, lease=30.0)

    @runner.task("plain")
    async def plain(ctx: Any) -> None:
        return None

    assert 0 < runner.deadline_for("plain") < 30.0


def test_a_task_may_not_declare_a_deadline_beyond_the_lease() -> None:
    import pytest

    conn = FakeConn()
    runner = _runner(conn, lease=10.0)
    with pytest.raises(ValueError, match="lease"):

        @runner.task("too_slow", timeout=25.0)
        async def too_slow(ctx: Any) -> None:
            return None


async def test_a_finished_handler_frees_its_slot_even_when_it_times_out() -> None:
    import asyncio

    conn = FakeConn()
    runner = _runner(conn, lease=1.0)

    @runner.task("hang", timeout=0.05)
    async def hang(ctx: Any) -> None:
        await asyncio.sleep(30)

    await runner._run(_job(task="hang", max_attempts=6, fence=0))
    assert not runner._inflight, "a timed-out handler kept its concurrency slot"
