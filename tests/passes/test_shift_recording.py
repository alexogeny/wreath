"""A pass shift is a job attempt, so it records like one.

`JobRunner.drive` registers the shift as an ordinary task -- that is the whole
of the pass/queue seam -- so a shift that fails is captured by exactly the
arming that captures any other failed attempt. No second vocabulary, and no
second recorder: what makes this worth a test is that it would be easy to build
one by accident.
"""

from __future__ import annotations

import datetime

import pytest

from wreath.jobs import JobRunner, _Claimed
from wreath.passes import ChunkedPass, Key, Purge, Rows, Sealed, Table
from wreath.postgres import PostgresError
from wreath.recording import (
    AttemptOutcome,
    AttemptPolicy,
    AttemptRecorder,
    AttemptTrigger,
    AttemptTriggerKind,
    read_attempt_recording,
)

from .fakes import FakeDatabase, World

EXPIRES = Key("expires", "timestamptz", indexed=True)
KEY = Key("key", "text", unique=True)


def _walk():
    return ChunkedPass(
        "purge_replays",
        over=Table("replays"),
        units=Rows(key=(EXPIRES, KEY), limit=100, within="2s"),
        frontier=Sealed(),
        work=Purge(),
        shift="10s",
    )


class _QueueConnection:
    """The pass fake's connection, plus the queue's own bookkeeping.

    The passes' `World` interprets the chunked-pass grammar and refuses
    anything outside it, which is what makes it worth testing against -- but
    the job runner writes its own `UPDATE "wreath".jobs` through the same
    database, and that is not pass grammar. Routing it here keeps the pass
    statements under the strict interpreter instead of loosening it.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner) -> None:
        self._inner = inner

    async def execute(self, sql, *args):
        if '"wreath".jobs' in sql:
            return "OK"
        return await self._inner.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _QueueDatabase(FakeDatabase):
    name = "main"

    async def acquire(self, workload: str = "write"):
        return _QueueConnection(await super().acquire(workload))


@pytest.fixture
def armed(tmp_path):
    database = _QueueDatabase(
        World(
            "replays",
            [{"key": "k", "expires": datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)}],
        )
    )
    runner = JobRunner(
        database,
        name="work",
        attempts=AttemptRecorder(
            AttemptPolicy(triggers=(AttemptTrigger(AttemptTriggerKind.FAILURE),)),
            directory=str(tmp_path),
        ),
    )
    return runner, database, tmp_path


def _shift_claim(task: str, *, fence: int = 3) -> _Claimed:
    return _Claimed(
        id=7, task=task, args=[], tenant="", attempts=0, max_attempts=5,
        fence=fence, key=None,
    )


async def test_a_failing_shift_is_recorded_as_the_job_attempt_it_is(armed):
    runner, database, directory = armed
    task = runner.drive(_walk(), cron="*/5 * * * *")

    def refuse_the_ledger(sql, args):
        if " ".join(sql.split()).startswith("SELECT"):
            raise PostgresError("permission denied for table passes")

    database.world.before = refuse_the_ledger

    await runner._run(_shift_claim(task))

    written = [p.name for p in directory.iterdir()]
    assert written == ["work-7-1.wfr1"]
    record = read_attempt_recording((directory / written[0]).read_bytes())
    assert record.outcome == AttemptOutcome.RAISED
    assert record.error_type == "PostgresError"
    assert "permission denied" in record.error_message
    assert record.task == task
    assert record.fence == 3
    assert record.argument_count == 0
    # The shift crossed the database it was driving, and the trace records those
    # crossings at the seams and coordinates a replay would double.
    assert record.boundaries
    assert {event.seam for event in record.boundaries} <= {0, 1, 2, 5}


async def test_a_shift_that_succeeds_is_not_recorded_by_a_failure_arm(armed):
    runner, _database, directory = armed
    task = runner.drive(_walk(), cron="*/5 * * * *")

    await runner._run(_shift_claim(task, fence=1))

    assert list(directory.iterdir()) == []
