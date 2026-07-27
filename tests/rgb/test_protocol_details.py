"""Protocol and helper details (report 23: R-01, R-29, R-41, R-77, G-01, G-20,
G-48, G-50, G-81)."""

from __future__ import annotations

import asyncio

import pytest

from wreath import Wreath
from wreath.testing import TestClient


class TestNegotiationQValues:
    """R-77: `q=0` means *not acceptable* (RFC 9110 §12.5.1), but it is honoured
    only for the exact range it appears on."""

    def test_an_excluded_type_is_not_served_via_a_wildcard(self):
        from wreath.negotiation import JSON, MSGPACK, negotiate

        chosen = negotiate("application/json;q=0, */*", (JSON, MSGPACK))
        assert chosen is not None
        assert chosen.media_type != "application/json"

    def test_an_excluded_subtype_wildcard_still_excludes(self):
        from wreath.negotiation import JSON, MSGPACK, negotiate

        assert negotiate("application/*;q=0", (JSON, MSGPACK)) is None

    def test_ordinary_preferences_are_unchanged(self):
        from wreath.negotiation import JSON, MSGPACK, negotiate

        assert negotiate("application/msgpack", (JSON, MSGPACK)).media_type == (
            "application/msgpack"
        )
        assert negotiate("*/*", (JSON, MSGPACK)).media_type == "application/json"
        assert negotiate(None, (JSON, MSGPACK)).media_type == "application/json"


class TestCronDayOfWeek:
    """G-20: crontab accepts 7 for Sunday; this parser rejects it."""

    def test_seven_is_sunday(self):
        from wreath._jobcore import CronSchedule

        schedule = CronSchedule("0 0 * * 7")
        # 2026-07-26 is a Sunday; Python weekday() puts Sunday at 6.
        assert schedule.matches(minute=0, hour=0, day=26, month=7, weekday=6)

    def test_a_range_through_seven_is_accepted(self):
        from wreath._jobcore import CronSchedule

        schedule = CronSchedule("0 0 * * 5-7")
        assert schedule.matches(minute=0, hour=0, day=26, month=7, weekday=6)  # Sun
        assert schedule.matches(minute=0, hour=0, day=24, month=7, weekday=4)  # Fri

    def test_out_of_range_is_still_refused(self):
        from wreath._jobcore import CronSchedule

        with pytest.raises(ValueError):
            CronSchedule("0 0 * * 8")


class TestStoreStatementNames:
    """R-01: the prepared-statement name is `{prefix}_{name}_{table}`, which
    PostgreSQL truncates at 63 bytes -- so two stores can collide on one
    statement, which is what `prefix` exists to prevent."""

    def _database(self):
        class _FakeDatabase:
            def __init__(self):
                self.names: list[str] = []

            def statement(self, name, sql, workload="write"):
                self.names.append(name)
                return object()

        return _FakeDatabase()

    def test_a_long_table_name_is_refused_rather_than_truncated(self):
        from wreath.store import Column, Keyed, PostgresStore

        database = self._database()
        long_table = "a" * 60
        declaration = Keyed(table=long_table, columns=(Column("v", "text"),))
        # Refused while the store is being described, not on the first request.
        with pytest.raises(ValueError, match="63"):
            PostgresStore(database, declaration)

    def test_ordinary_names_still_prepare(self):
        from wreath.store import Column, Keyed, PostgresStore

        database = self._database()
        store = PostgresStore(
            database, Keyed(table="wreath_session", columns=(Column("v", "text"),))
        )
        store.statement("read")
        assert database.names == ["wreath_store_read_wreath_session"]


class TestStoreIntervals:
    """G-01: a non-finite lifetime is rendered into SQL as `inf::float8`."""

    def test_a_non_finite_window_is_refused(self):
        from wreath.store import Keyed, PostgresStore

        store = PostgresStore(object(), Keyed(table="t"))
        with pytest.raises(ValueError):
            store.window(float("inf"))
        with pytest.raises(ValueError):
            store.window(float("nan"))

    def test_a_negative_window_is_refused(self):
        from wreath.store import Keyed, PostgresStore

        store = PostgresStore(object(), Keyed(table="t"))
        with pytest.raises(ValueError):
            store.window(-1.0)

    def test_a_placeholder_is_still_allowed(self):
        from wreath.store import Keyed, PostgresStore

        store = PostgresStore(object(), Keyed(table="t"))
        assert "$1" in store.window("$1")


class TestProgressStreamTermination:
    """R-29: a stream for a task that does not exist never ends, so an
    unauthenticated request can hold a connection open indefinitely."""

    async def test_an_unknown_task_ends_the_stream(self):
        from wreath.progress import ProgressRegistry

        registry = ProgressRegistry()
        events = []

        async def drain():
            async for item in registry.stream("nope", interval=0.01):
                events.append(item)

        async with asyncio.timeout(2):
            await drain()
        # The point of this test is that the stream *ends* rather than polling
        # forever; the timeout above is what proves it. It now ends with one
        # closing event naming the reason, so a client can tell "no such task"
        # apart from a dropped connection (design 22 item 11).
        assert [item.state for item in events] == ["unknown"]
        assert events[-1].ends_stream

    async def test_a_live_task_still_streams_to_its_terminal_state(self):
        from wreath.progress import ProgressRegistry

        registry = ProgressRegistry()
        registry.report("t1", 10, "working")

        seen = []

        async def drain():
            async for item in registry.stream("t1", interval=0.01):
                seen.append(item)

        async def finish():
            await asyncio.sleep(0.05)
            registry.report("t1", 100, "done", state="done")

        async with asyncio.timeout(2):
            await asyncio.gather(drain(), finish())
        assert seen[0].percent == 10
        assert seen[-1].state == "done"


class TestMissAndHead:
    async def test_a_registered_404_handler_sees_a_routing_miss(self):
        """G-50: a miss builds its own ProblemResponse, so `add_status_handler`
        never fires for the case people register it for."""
        app = Wreath()

        async def not_found(request, error):
            from wreath.response import JSONResponse

            return JSONResponse({"custom": True}, status=404)

        app.add_status_handler(404, not_found)

        @app.get("/known")
        async def known(request):
            return {}

        async with TestClient(app) as client:
            response = await client.get("/unknown")
        assert response.status == 404
        assert response.json() == {"custom": True}

    async def test_head_is_served_by_a_get_route(self):
        """G-48: RFC 9110 §9.3.2 -- HEAD is GET without a body."""
        app = Wreath()

        @app.get("/thing")
        async def thing(request):
            return {"a": 1}

        async with TestClient(app) as client:
            response = await client.head("/thing")
        assert response.status == 200
        assert response.body == b""


class TestBodyTruncation:
    """R-41: `body()` treats a disconnect as end-of-body and hands the handler
    the bytes that did arrive, as if the client had sent exactly those."""

    async def test_a_disconnect_mid_body_is_not_a_complete_body(self):
        from wreath.exceptions import ClientDisconnect
        from wreath.request import Request

        messages = [
            {"type": "http.request", "body": b'{"amount": 1', "more_body": True},
            {"type": "http.disconnect"},
        ]

        async def receive():
            return messages.pop(0)

        request = Request(
            {"type": "http", "method": "POST", "path": "/", "headers": []}, receive
        )
        with pytest.raises(ClientDisconnect):
            await request.body()

    async def test_a_complete_body_is_unaffected(self):
        from wreath.request import Request

        messages = [
            {"type": "http.request", "body": b'{"a": ', "more_body": True},
            {"type": "http.request", "body": b"1}", "more_body": False},
        ]

        async def receive():
            return messages.pop(0)

        request = Request(
            {"type": "http", "method": "POST", "path": "/", "headers": []}, receive
        )
        assert await request.body() == b'{"a": 1}'
