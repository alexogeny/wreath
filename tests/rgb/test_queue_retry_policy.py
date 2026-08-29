from __future__ import annotations

import pytest
from _doubles import RecordingConnection

from wreath.jobs import JobRunner
from wreath.messaging import MessageBus


class FakeDatabase:
    def __init__(self):
        self.connection = RecordingConnection()

    async def acquire(self, workload):
        return self.connection

    async def release(self, workload, connection):
        pass

    def calls_matching(self, needle: str):
        return [call for call in self.connection.calls if needle in call[0]]


def _sweep_sql(statements) -> tuple[str, tuple]:
    return next(call for call in statements if "lease_expiry <" in call[0] and "UPDATE" in call[0])


class TestReclaimCountsAnAttempt:
    """R-17 / R-23: an expired lease returns the row to `ready` without
    incrementing `attempts`, so a job or message that kills its worker is
    redelivered forever and never dead-letters."""

    async def test_jobs_sweeper_increments_attempts(self):
        db = FakeDatabase()
        runner = JobRunner(db, name="work")
        await runner._reclaim_expired()
        sql, _args = _sweep_sql(db.connection.calls)
        assert "attempts = attempts + 1" in sql

    async def test_jobs_sweeper_dead_letters_past_max_attempts(self):
        db = FakeDatabase()
        runner = JobRunner(db, name="work")
        await runner._reclaim_expired()
        sql, _args = _sweep_sql(db.connection.calls)
        assert "'dead'" in sql and "max_attempts" in sql

    async def test_messaging_sweeper_increments_attempts(self):
        db = FakeDatabase()
        bus = MessageBus(db, name="events")

        @bus.subscribe("thing", group="g", durable=True)
        async def handler(message):  # pragma: no cover - never delivered here
            pass

        await bus._reclaim_expired(bus._subs[0])
        sql, _args = _sweep_sql(db.connection.calls)
        assert "attempts = attempts + 1" in sql
        assert "'dead'" in sql and "max_attempts" in sql


class TestMessagingBackoff:
    """R-24: `_retry` computes its delay from a hardcoded attempt 1, so every
    retry waits the same ~1s no matter how many have already failed."""

    def _bus_with_sub(self, retries: int = 5):
        db = FakeDatabase()
        bus = MessageBus(db, name="events")

        @bus.subscribe("thing", group="g", durable=True, retries=retries)
        async def handler(message):  # pragma: no cover
            pass

        return db, bus, bus._subs[0]

    async def _delay_for(self, attempts: int) -> float:
        from wreath.messaging import Message

        db, bus, sub = self._bus_with_sub()
        message = Message(
            channel="thing",
            group="g",
            tenant="",
            payload={},
            id=7,
            fence=1,
            attempts=attempts,
        )
        await bus._retry(sub, message, "boom")
        sql, args = next(
            call for call in db.connection.calls if "attempts = attempts + 1" in call[0]
        )
        return float(next(a for a in args if isinstance(a, str) and a.replace(".", "").isdigit()))

    async def test_delay_grows_with_attempts(self):
        first = await self._delay_for(1)
        later = await self._delay_for(5)
        assert later > first * 2, (first, later)

    async def test_configured_retries_bound_the_dead_letter_threshold(self):
        from wreath.messaging import Message

        db, bus, sub = self._bus_with_sub(retries=2)
        message = Message(
            channel="thing",
            group="g",
            tenant="",
            payload={},
            id=7,
            fence=1,
            attempts=2,
        )
        await bus._retry(sub, message, "boom")
        sql, args = next(
            call for call in db.connection.calls if "attempts = attempts + 1" in call[0]
        )
        # The consumer's configured budget has to reach the statement somehow;
        # a hardcoded 6 in the INSERT cannot express `retries=2`.
        assert sub.retries + 1 in args, args


@pytest.mark.skip(
    reason="needs a bigger refactor in the source: renewing a lease means a "
    "heartbeat task per in-flight item, cancelled on completion and fenced "
    "against the sweeper. See report 23 R-16/R-22."
)
class TestLeaseRenewal:
    """R-16 / R-22: a handler slower than `lease` has its work reclaimed and
    re-run while the first copy is still executing."""

    async def test_slow_job_handler_is_not_reclaimed_while_running(self):
        raise AssertionError("unimplemented")

    async def test_slow_message_handler_is_not_reclaimed_while_running(self):
        raise AssertionError("unimplemented")
