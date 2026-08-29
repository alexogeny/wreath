from __future__ import annotations

import asyncio

import pytest
from _doubles import RecordingConnection

from wreath.jobs import JobRunner
from wreath.messaging import MessageBus


class _FakeDatabase:
    def __init__(self):
        self.connection = RecordingConnection()

    async def acquire(self, workload):
        return self.connection

    async def release(self, workload, connection):
        pass

    def matching(self, needle):
        return [call for call in self.connection.calls if needle in call[0]]


class TestDoorbellWakesEveryParkedWorker:
    """R-19: every worker parks on one shared `asyncio.Event`, so one worker's
    `clear()` racing another's `set()` loses the wake -- the parked worker then
    sleeps the whole poll interval with work waiting."""

    async def test_a_doorbell_wakes_all_parked_job_workers(self):
        runner = JobRunner(_FakeDatabase(), name="work", poll_interval=30.0, concurrency=3)
        waiters = [runner._new_waiter() for _ in range(3)]
        parked = [asyncio.create_task(runner._park(w)) for w in waiters]
        await asyncio.sleep(0)  # let them all reach the wait

        runner._wake_workers()

        async with asyncio.timeout(1):
            await asyncio.gather(*parked)

    async def test_one_workers_clear_does_not_eat_anothers_wake(self):
        runner = JobRunner(_FakeDatabase(), name="work", poll_interval=30.0, concurrency=2)
        first, second = runner._new_waiter(), runner._new_waiter()

        # The doorbell rings while the first worker is between claims...
        runner._wake_workers()
        # ...and that worker starts its next round, clearing *its own* waiter.
        first.clear()

        # The second worker was already parked. Its wake must have survived.
        async with asyncio.timeout(1):
            await runner._park(second)

    async def test_a_wake_during_a_claim_is_remembered(self):
        runner = JobRunner(_FakeDatabase(), name="work", poll_interval=30.0)
        wake = runner._new_waiter()
        wake.clear()  # the worker is about to claim
        runner._wake_workers()  # a NOTIFY lands mid-claim
        async with asyncio.timeout(1):  # the park must not sleep through it
            await runner._park(wake)

    async def test_a_doorbell_wakes_all_parked_consumers(self):
        bus = MessageBus(_FakeDatabase(), name="events", poll_interval=30.0)
        waiters = [bus._new_waiter() for _ in range(3)]
        parked = [asyncio.create_task(bus._park(w)) for w in waiters]
        await asyncio.sleep(0)

        bus._wake_consumers()

        async with asyncio.timeout(1):
            await asyncio.gather(*parked)


class TestRetention:
    """G-16 / G-22: `JobVanished`'s docstring describes 'a retention sweep over
    completed jobs' and none exists, so `done` rows accumulate forever. The
    messages table has the same gap."""

    async def test_a_job_runner_can_purge_completed_rows(self):
        database = _FakeDatabase()
        runner = JobRunner(database, name="work")
        await runner.purge(older_than=86_400)
        sql, args = database.matching("DELETE")[0]
        assert "state IN ('done', 'dead')" in sql or "state = ANY" in sql
        assert 86_400 in args or "86400.000" in args

    async def test_a_bus_can_purge_completed_rows(self):
        database = _FakeDatabase()
        bus = MessageBus(database, name="events")
        await bus.purge(older_than=86_400)
        assert database.matching("DELETE")

    async def test_purging_refuses_a_non_positive_age(self):
        runner = JobRunner(_FakeDatabase(), name="work")
        with pytest.raises(ValueError):
            await runner.purge(older_than=0)


class TestGroupRegistryUpkeep:
    """G-21: groups are registered and never deregistered, so a decommissioned
    consumer keeps receiving one durable copy of everything into a queue nobody
    drains."""

    async def test_a_bus_can_forget_its_groups(self):
        database = _FakeDatabase()
        bus = MessageBus(database, name="events")

        @bus.subscribe("thing", group="g", durable=True)
        async def handler(message):  # pragma: no cover
            pass

        await bus._deregister_groups()
        assert database.matching("DELETE FROM"), "nothing removed the registration"

    async def test_stale_groups_can_be_pruned_by_age(self):
        database = _FakeDatabase()
        bus = MessageBus(database, name="events")
        await bus.prune_groups(unseen_for=30 * 86_400)
        sql, _args = database.matching("DELETE FROM")[0]
        assert "seen_at" in sql


class TestLifecycleValidatorIsUsed:
    """G-15: `_jobcore` says 'every transition a worker performs is checked
    against this table', and `check_transition` is called from nowhere."""

    def test_the_documented_claim_matches_the_code(self):
        import inspect

        from wreath import _jobcore

        source = inspect.getsource(_jobcore)
        claims_checking = "Every transition a worker performs is checked" in source
        if not claims_checking:
            return  # the docstring was corrected instead
        import wreath.jobs
        import wreath.messaging

        used = any(
            "check_transition" in inspect.getsource(module)
            for module in (wreath.jobs, wreath.messaging)
        )
        assert used, "the module documents a check that nothing performs"

    def test_the_transition_table_still_describes_the_lifecycle(self):
        from wreath._jobcore import valid_transition

        assert valid_transition("ready", "leased")
        assert valid_transition("leased", "done")
        assert not valid_transition("done", "ready")
