from __future__ import annotations

import asyncio

import pytest

from wreath import Wreath


class TestWriteAnnouncementsAreCounted:
    """B-05: `publish_write` swallows subscriber *and* bridge exceptions, so a
    cache listener that has been raising since deploy looks exactly like one
    that works."""

    def test_a_raising_subscriber_is_counted(self):
        from wreath import _orm_events

        def broken(names):
            raise RuntimeError("nope")

        _orm_events.subscribe_writes(broken)
        try:
            before = _orm_events.subscriber_errors()
            _orm_events.publish_write(frozenset({"User"}))
            assert _orm_events.subscriber_errors() == before + 1
        finally:
            _orm_events.unsubscribe_writes(broken)

    def test_a_raising_bridge_is_counted(self):
        from wreath import _orm_events

        def broken(names):
            raise RuntimeError("nope")

        _orm_events.register_bridge(broken)
        try:
            before = _orm_events.bridge_errors()
            _orm_events.publish_write(frozenset({"User"}))
            assert _orm_events.bridge_errors() == before + 1
        finally:
            _orm_events.unregister_bridge(broken)

    def test_a_healthy_subscriber_counts_nothing(self):
        from wreath import _orm_events

        seen: list = []
        _orm_events.subscribe_writes(seen.append)
        try:
            before = _orm_events.subscriber_errors()
            _orm_events.publish_write(frozenset({"User"}))
            assert seen == [frozenset({"User"})]
            assert _orm_events.subscriber_errors() == before
        finally:
            _orm_events.unsubscribe_writes(seen.append)


class TestBusPublishFailuresAreCounted:
    """B-04: `_publish_quietly` suppresses every exception with no counter, so a
    bus that has been failing all week is invisible."""

    async def test_a_failing_publish_is_counted(self):
        from wreath._busbridge import BusBridge

        class _Bus:
            def subscribe(self, channel):
                return lambda fn: fn

            async def publish(self, channel, payload):
                raise RuntimeError("the bus is down")

        async def apply(payload):  # pragma: no cover
            pass

        bridge = BusBridge(_Bus(), channel="c", apply=apply)
        bridge.publish_soon({"n": 1})
        pending = tuple(bridge._inflight)
        if pending:
            await asyncio.wait(pending)
        assert bridge.publish_errors == 1


class TestJobLoopFailuresAreCounted:
    """B-06: the scheduler and sweeper wrap their whole body in
    `contextlib.suppress(Exception)`, so one that has never fired is
    indistinguishable from one with nothing to do. B-07: dead-lettering emits
    no signal at all."""

    def _runner(self, database):
        from wreath.jobs import JobRunner

        return JobRunner(database, name="work")

    async def test_a_failing_sweep_is_counted(self):
        class _Database:
            async def acquire(self, workload):
                return self

            async def release(self, workload, connection):
                pass

            async def execute(self, sql, *args):
                raise RuntimeError("no such table")

        runner = self._runner(_Database())

        class _Supervisor:
            stopping = asyncio.Event()

        supervisor = _Supervisor()
        runner._supervisor = supervisor

        # The sweep raises, and stops the loop on its way out, so exactly one
        # pass runs.
        original = runner._reclaim_expired

        async def failing_sweep():
            supervisor.stopping.set()
            await original()

        runner._reclaim_expired = failing_sweep
        async with asyncio.timeout(2):
            await runner._sweeper()
        assert runner.sweep_errors >= 1

    async def test_a_dead_letter_is_counted(self):
        calls: list = []

        class _Database:
            async def acquire(self, workload):
                return self

            async def release(self, workload, connection):
                pass

            async def execute(self, sql, *args):
                calls.append(sql)
                return "OK"

        runner = self._runner(_Database())

        @runner.task("t", retries=0)
        async def handler(ctx):  # pragma: no cover
            pass

        from wreath.jobs import _Claimed

        job = _Claimed(
            id=1,
            task="t",
            args=[],
            tenant="",
            attempts=0,
            max_attempts=1,
            fence=1,
            key=None,
        )
        await runner._fail(job, "boom", runner._tasks["t"])
        assert runner.dead_lettered == 1


class TestBackgroundTaskFailures:
    """B-10: `response.background` runs after the response is sent with no
    exception handling anywhere, so a failure surfaces in the server's request
    task with nothing to catch it."""

    async def test_a_failing_background_task_is_counted(self):
        from wreath.background import BackgroundTask
        from wreath.testing import TestClient

        app = Wreath()

        async def explode():
            raise RuntimeError("after the response")

        @app.get("/go")
        async def go(request):
            from wreath.response import JSONResponse

            return JSONResponse({"ok": True}, background=BackgroundTask(explode))

        async with TestClient(app) as client:
            with pytest.raises(RuntimeError, match="after the response"):
                await client.get("/go")
        assert app.background_errors == 1


class TestWriteBroadcastClose:
    """G-11: `close()` unregisters the bridge but leaves the bus subscription
    live, so a closed broadcast still applies remote writes into local
    subscribers."""

    async def test_a_closed_broadcast_applies_nothing(self):
        from wreath._orm_events import WriteBroadcast

        received: list = []

        class _Bus:
            def __init__(self):
                self.handler = None

            def subscribe(self, channel):
                def register(fn):
                    self.handler = fn
                    return fn

                return register

            async def publish(self, channel, payload):  # pragma: no cover
                pass

        bus = _Bus()
        broadcast = WriteBroadcast(bus)
        from wreath import _orm_events

        _orm_events.subscribe_writes(received.append)
        try:
            broadcast.close()

            class _Message:
                payload = {"models": ["User"], "origin": "elsewhere"}

            await bus.handler(_Message())
            assert received == [], "a closed broadcast still delivered"
        finally:
            _orm_events.unsubscribe_writes(received.append)


class TestSpecCacheKey:
    """G-51: the OpenAPI spec and docs HTML are cached on `len(self._routes)`,
    so an equal-count route change serves a stale spec."""

    async def test_a_same_count_route_change_refreshes_the_spec(self, monkeypatch):
        monkeypatch.setenv("WREATH_ENV", "dev")
        from wreath.testing import TestClient

        app = Wreath()

        @app.get("/first")
        async def first(request):
            return {}

        app.enable_docs()

        async with TestClient(app) as client:
            before = (await client.get("/openapi.json")).json()
        assert "/first" in before["paths"]

        # Swap one route for another: the count is identical.
        app._routes[0] = app._routes[0].__class__(
            "/second",
            ("GET",),
            first,
            (),
            (),
            None,
            (),
            app._routes[0].requirement,
            None,
        )
        app._dirty = True

        async with TestClient(app) as client:
            after = (await client.get("/openapi.json")).json()
        assert "/second" in after["paths"], "a stale spec was served"


class TestNotFoundAllocation:
    """G-53: `_NOT_FOUND` is built once at import and never used; every miss
    allocates a fresh 404."""

    def test_the_module_has_no_unused_prebuilt_response(self):
        import inspect

        from wreath import app as app_module

        source = inspect.getsource(app_module)
        if "_NOT_FOUND" not in source:
            return  # removed outright, which is also fine
        assert source.count("_NOT_FOUND") > 1, "defined and never used"
