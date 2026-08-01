"""Worker survival, channel naming, and bus trust (report 23: R-13, R-14, R-18,
R-20, R-26, R-27)."""

from __future__ import annotations

import asyncio

import pytest

from wreath.jobs import JobRunner
from wreath.messaging import MessageBus


class _Supervisor:
    def __init__(self) -> None:
        self.stopping = asyncio.Event()
        self.spawned: list[str] = []

    def spawn(self, name, coro):
        self.spawned.append(name)
        coro.close()


class _FailingCompletion:
    """A database that claims work and then refuses to record the outcome."""

    def __init__(self, row):
        self.row = row
        self.claims = 0

    async def acquire(self, workload):
        return self

    async def release(self, workload, connection):
        pass

    async def fetch(self, sql, *args):
        if "state='leased'" in sql and "claimable" in sql:
            self.claims += 1
            return [self.row] if self.claims == 1 else []
        return []

    async def fetchrow(self, sql, *args):
        if "claimable" in sql:
            self.claims += 1
            return self.row if self.claims == 1 else None
        return None

    async def execute(self, sql, *args):
        if "state='done'" in sql:
            raise RuntimeError("connection reset while recording completion")
        return "OK"

    async def fetchval(self, sql, *args):
        # The version-2 `trace_context` column probe, and nothing else. `None`
        # models a schema still on version 1 -- this double is about surviving a
        # failed outcome write, and it holds rows with no trace column, so
        # answering yes would model a catalog and rows that disagree.
        return None


class TestWorkerSurvivesAnOutcomeFailure:
    """R-18 / R-27: the try/except covers the *claim*, not the run, so a
    transient database error while recording an outcome kills that worker (or
    consumer) for the life of the process."""

    async def test_a_job_worker_survives(self):
        row = {
            "id": 1, "task": "t", "args": [], "tenant": "", "attempts": 0,
            "max_attempts": 3, "fence": 1, "dedup_key": None,
        }
        database = _FailingCompletion(row)
        runner = JobRunner(database, name="work", poll_interval=0.01)
        supervisor = _Supervisor()
        runner._supervisor = supervisor

        ran = 0

        @runner.task("t")
        async def handler(ctx):
            nonlocal ran
            ran += 1
            supervisor.stopping.set()

        async with asyncio.timeout(2):
            await runner._worker()      # must return, not raise
        assert ran == 1

    async def test_a_message_consumer_survives(self):
        row = {"id": 1, "payload": {}, "tenant": "", "fence": 1, "attempts": 0}
        database = _FailingCompletion(row)
        bus = MessageBus(database, name="events", poll_interval=0.01)
        supervisor = _Supervisor()
        bus._supervisor = supervisor

        ran = 0

        @bus.subscribe("thing", group="g", durable=True)
        async def handler(message):
            nonlocal ran
            ran += 1
            supervisor.stopping.set()

        async with asyncio.timeout(2):
            await bus._consumer(bus._subs[0])
        assert ran == 1


class TestChannelNamesDoNotCollide:
    """R-20 / R-26: the wire channel is truncated to 63 bytes, so two long
    names share one doorbell -- jobs wake each other's workers, and ephemeral
    payloads reach the wrong subscribers."""

    def test_a_job_queue_name_that_would_truncate_is_refused(self):
        class _Database:
            async def acquire(self, workload):  # pragma: no cover
                raise AssertionError

        with pytest.raises(ValueError, match="63"):
            JobRunner(_Database(), name="q" * 60, schema="wreath")

    def test_a_channel_that_would_truncate_is_refused(self):
        class _Database:
            async def acquire(self, workload):  # pragma: no cover
                raise AssertionError

        bus = MessageBus(_Database(), name="events")
        with pytest.raises(ValueError, match="63"):
            bus._channel_wire("c" * 60)

    def test_ordinary_names_are_unchanged(self):
        class _Database:
            async def acquire(self, workload):  # pragma: no cover
                raise AssertionError

        bus = MessageBus(_Database(), name="events")
        assert bus._channel_wire("orders") == "wm_wreath_orders"


class TestBusBridgeTrust:
    """R-13: an untagged payload is applied as foreign, so anything able to
    NOTIFY on the channel can drive cache invalidation, room broadcasts, and
    progress writes. R-14: the deferred-publish set is unbounded."""

    def _bridge(self, applied):
        from wreath._busbridge import BusBridge

        class _Bus:
            def subscribe(self, channel):
                def register(fn):
                    return fn

                return register

            async def publish(self, channel, payload):
                await asyncio.sleep(0.05)

        async def apply(payload):
            applied.append(payload)

        return BusBridge(_Bus(), channel="c", apply=apply)

    async def test_an_untagged_payload_is_delivered_but_counted(self):
        """R-13 is a *deliberate* trade-off, not a defect: delivering an
        untagged payload is the shipped decision (see
        `tests/test_busbridge.py::test_a_payload_with_no_origin_is_treated_as_foreign`).
        What was missing is that a publisher which is not a bridge -- anything
        with NOTIFY rights on the database -- drove invalidations, broadcasts,
        and progress writes invisibly. It is now counted."""
        applied: list = []
        bridge = self._bridge(applied)

        class _Message:
            payload = {"models": ["User"]}      # no origin tag

        await bridge._receive(_Message())
        assert applied == [{"models": ["User"]}]
        assert bridge.untagged_applied == 1

    async def test_a_foreign_tagged_payload_is_applied(self):
        applied: list = []
        bridge = self._bridge(applied)

        class _Message:
            payload = {"models": ["User"], "origin": "somebody-else"}

        await bridge._receive(_Message())
        assert applied == [{"models": ["User"], "origin": "somebody-else"}]

    async def test_deferred_publishes_are_bounded(self):
        bridge = self._bridge([])
        for index in range(10_000):
            bridge.publish_soon({"n": index})
        assert bridge.inflight <= 1024
        assert bridge.dropped_publishes > 0
        # Let the parked publishes finish so the test leaves no pending tasks.
        pending = tuple(bridge._inflight)
        if pending:
            await asyncio.wait(pending)
