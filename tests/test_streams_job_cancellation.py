from __future__ import annotations

import pytest
from _pgfidelity import check_statement

from wreath._jobcore import dedup_key
from wreath.jobs import JobRunner
from wreath.progress import ProgressRegistry


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        #: Rows successive `fetch` calls answer with.
        self.fetch_script: list[list] = []

    async def execute(self, sql, *args):
        check_statement(sql, args)
        self.calls.append((sql, args))
        return "OK"

    async def fetchval(self, sql, *args):
        check_statement(sql, args)
        self.calls.append((sql, args))
        return 1

    async def fetch(self, sql, *args):
        check_statement(sql, args)
        self.calls.append((sql, args))
        return self.fetch_script.pop(0) if self.fetch_script else []

    async def fetchrow(self, sql, *args):
        check_statement(sql, args)
        self.calls.append((sql, args))
        return None


class _Database:
    def __init__(self) -> None:
        self.connection = _Connection()
        self.released = 0

    async def acquire(self, workload):
        return self.connection

    async def release(self, workload, connection):
        self.released += 1


def _runner(**options) -> tuple[JobRunner, _Database]:
    database = _Database()
    return JobRunner(database, name="work", **options), database


async def test_cancel_takes_exactly_one_of_id_and_key() -> None:
    runner, _database = _runner()
    for arguments in ({}, {"job_id": 7, "key": "k"}):
        with pytest.raises(ValueError) as raised:
            await runner.cancel(**arguments)
        assert "exactly one of job_id= and key=" in str(raised.value)
        assert "would cancel the whole queue" in str(raised.value)


async def test_cancel_by_id_selects_on_the_id() -> None:
    runner, database = _runner()
    database.connection.fetch_script = [[{"id": 7}]]
    assert await runner.cancel(7) is True
    sql, args = database.connection.calls[-1]
    assert "id=$3" in sql and "dedup_key=$3" not in sql
    assert args[1] == ""
    assert args[2] == 7


async def test_cancel_by_key_hashes_the_key_the_way_enqueue_did() -> None:
    runner, database = _runner()
    database.connection.fetch_script = [[{"id": 7}]]
    assert await runner.cancel(key="stream:conversation-7") is True
    sql, args = database.connection.calls[-1]
    assert "dedup_key=$3" in sql and "id=$3" not in sql
    assert args[1] == ""
    assert args[2] == dedup_key("work", "stream:conversation-7")
    assert args[2] != "stream:conversation-7"


async def test_cancel_bumps_the_fence_because_that_is_the_whole_mechanism() -> None:
    runner, database = _runner()
    database.connection.fetch_script = [[{"id": 7}]]
    await runner.cancel(7)
    sql, _args = database.connection.calls[-1]
    assert "fence=fence+1" in sql
    assert "state='dead'" in sql
    # Only a row still in play: a job that finished is not cancellable.
    assert "state IN ('ready', 'leased')" in sql


async def test_cancelling_a_row_that_moved_reports_false_and_counts_nothing() -> None:
    runner, database = _runner()
    database.connection.fetch_script = [[]]
    assert await runner.cancel(7) is False
    assert runner.cancelled == 0
    assert runner.stats()["cancelled"] == 0


async def test_a_cancelled_job_is_counted_apart_from_a_dead_letter() -> None:
    runner, database = _runner()
    database.connection.fetch_script = [[{"id": 7}]]
    assert await runner.cancel(7) is True
    assert runner.cancelled == 1
    assert runner.dead_lettered == 0
    assert runner.stats()["cancelled"] == 1


async def test_cancelling_closes_out_a_watching_client() -> None:
    progress = ProgressRegistry()
    database = _Database()
    runner = JobRunner(database, name="work", progress=progress)
    progress.report("7", 60, "generating")
    database.connection.fetch_script = [[{"id": 7}]]
    await runner.cancel(7, reason="the user closed the tab")
    current = progress.get("7")
    assert current is not None
    assert current.state == "failed"
    assert current.error == "the user closed the tab"
    assert current.percent == 60, "the last percentage seen, not a jump back to zero"


async def test_cancelling_without_a_registry_is_still_fine() -> None:
    runner, database = _runner()
    database.connection.fetch_script = [[{"id": 7}]]
    assert await runner.cancel(7) is True
    assert runner.progress is None


async def test_the_reason_is_clamped_to_what_the_row_can_hold() -> None:
    runner, database = _runner()
    database.connection.fetch_script = [[{"id": 7}]]
    await runner.cancel(7, reason="x" * 5000)
    _sql, args = database.connection.calls[-1]
    assert len(args[3]) == 2000


async def test_the_connection_is_released_even_though_nothing_was_claimed() -> None:
    runner, database = _runner()
    database.connection.fetch_script = [[{"id": 7}]]
    await runner.cancel(7)
    assert database.released == 1
