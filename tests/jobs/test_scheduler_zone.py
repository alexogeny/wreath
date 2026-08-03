"""A schedule fires on its own wall clock, not on UTC's.

`schedule(cron=...)` reads a five-field expression in UTC, which is what it has
always meant and what it still means. That is correct for "every fifteen
minutes" and quietly wrong for "03:00", because an operator who writes 03:00
means 03:00 where the depot is -- and half the year, UTC is not that.

`schedule(recurrence=...)` takes a `wreath.temporal.Recurrence`, which carries
its zone. These tests drive `_tick_schedules` directly against a fake clock, so
they assert what the scheduler *enqueues* rather than what the parser accepts:

* a zoned schedule fires at the depot's 03:00 and not at UTC's;
* the dedup key is the recurrence's local minute, so the hour that repeats on a
  fall-back day enqueues once rather than twice;
* a `cron=` schedule is unchanged, because moving every existing schedule on the
  first deploy after this landed would be the worst possible way to ship a
  correctness fix.
"""

from __future__ import annotations

import datetime

import pytest

from wreath.jobs import JobRunner
from wreath.temporal import Recurrence

SYDNEY = "Australia/Sydney"


class _RecordingRunner(JobRunner):
    """A runner whose clock is injected and whose enqueues are collected."""

    def __init__(self, moment: datetime.datetime) -> None:
        super().__init__(_NoDatabase(), name="work")
        self.moment = moment
        self.enqueued: list[tuple[str, str | None]] = []

    async def _now(self):  # type: ignore[override]
        # The real one reads `now() AT TIME ZONE 'UTC'` from the database, which
        # is naive. Matching that is the point: the scheduler is what places it
        # on the timeline.
        return self.moment

    async def enqueue(self, task, *args, key=None, **kw):  # type: ignore[override]
        self.enqueued.append((task, key))
        return 1


class _NoDatabase:
    async def acquire(self, workload):
        raise AssertionError("no statement should reach the database in these tests")

    async def release(self, workload, connection):  # pragma: no cover - unreachable
        raise AssertionError


def _at(text: str) -> datetime.datetime:
    """A naive UTC reading, the shape `_now` returns."""
    return datetime.datetime.fromisoformat(text)


async def _tick(moment: datetime.datetime, **schedule_kw) -> list[tuple[str, str | None]]:
    runner = _RecordingRunner(moment)
    # No `@runner.task` registration: `enqueue` is overridden above, so the
    # unknown-task check it would otherwise make is not the subject here.
    runner.schedule("rebalance", **schedule_kw)
    await runner._tick_schedules()
    return runner.enqueued


# --- the zone is the point -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_zoned_schedule_fires_at_the_depots_three_am() -> None:
    # 2026-08-02T17:00Z is 03:00 the next morning in Sydney (AEST, +10).
    fired = await _tick(
        _at("2026-08-02T17:00:00"),
        recurrence=Recurrence.cron("0 3 * * *", tz=SYDNEY),
    )
    assert [task for task, _ in fired] == ["rebalance"]


@pytest.mark.asyncio
async def test_a_zoned_schedule_does_not_fire_at_utc_three_am() -> None:
    fired = await _tick(
        _at("2026-08-03T03:00:00"),
        recurrence=Recurrence.cron("0 3 * * *", tz=SYDNEY),
    )
    assert fired == []


@pytest.mark.asyncio
async def test_the_same_expression_as_cron_fires_at_utc_three_am() -> None:
    # The `cron=` spelling is unchanged, which is what makes this safe to deploy.
    fired = await _tick(_at("2026-08-03T03:00:00"), cron="0 3 * * *")
    assert [task for task, _ in fired] == ["rebalance"]


@pytest.mark.asyncio
async def test_the_dedup_key_of_a_utc_schedule_is_the_string_it_always_was() -> None:
    fired = await _tick(_at("2026-08-03T03:00:00"), cron="0 3 * * *")
    assert fired == [("rebalance", "cron:rebalance:202608030300")]


# --- the fall-back hour --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_hour_that_happens_twice_enqueues_once() -> None:
    # 2026-04-05 in Sydney: 03:00 -> 02:00, so local 02:30 occurs at two
    # distinct UTC minutes. Both fire, and both carry the same dedup key, so the
    # unique index lands exactly one row -- which is what "02:30 daily" means.
    recurrence = Recurrence.cron("30 2 * * *", tz=SYDNEY)
    first = await _tick(_at("2026-04-04T15:30:00"), recurrence=recurrence)  # +11
    second = await _tick(_at("2026-04-04T16:30:00"), recurrence=recurrence)  # +10

    assert [task for task, _ in first] == ["rebalance"]
    assert [task for task, _ in second] == ["rebalance"]
    assert first[0][1] == second[0][1] == "cron:rebalance:202604050230"


@pytest.mark.asyncio
async def test_the_hour_that_never_happens_never_fires() -> None:
    # 2026-10-04 in Sydney: 02:00 -> 03:00, so local 02:30 does not exist. No
    # UTC minute reads as 02:30 local that day, so nothing is enqueued.
    recurrence = Recurrence.cron("30 2 * * *", tz=SYDNEY)
    minute = _at("2026-10-03T15:00:00")
    for _ in range(180):  # three hours either side of the transition
        assert await _tick(minute, recurrence=recurrence) == []
        minute += datetime.timedelta(minutes=1)


# --- the declaration -----------------------------------------------------------------


def test_schedule_refuses_both_spellings_at_once() -> None:
    runner = _RecordingRunner(_at("2026-08-03T00:00:00"))
    with pytest.raises(ValueError, match="both were given"):
        runner.schedule(
            "rebalance", cron="0 3 * * *", recurrence=Recurrence.cron("0 3 * * *")
        )


def test_schedule_refuses_neither() -> None:
    runner = _RecordingRunner(_at("2026-08-03T00:00:00"))
    with pytest.raises(ValueError, match="neither was given"):
        runner.schedule("rebalance")
