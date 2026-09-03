from __future__ import annotations

import json
import os
from typing import Any

import pytest
from _pgfidelity import check_for

from wreath import _pytest_plugin, telemetry
from wreath import messaging as messaging_module
from wreath.messaging import MessageBus

PARENT = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
OTHER = "00-" + "c" * 32 + "-" + "d" * 16 + "-01"

_DSN = os.environ.get(_pytest_plugin.DSN_ENV)


class FakeConn:
    def __init__(self, *, trace_column: bool = True, row: Any = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.trace_column = trace_column
        self.row = row

    async def execute(self, sql: str, *args: Any) -> str:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> Any:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return self.row

    async def fetchval(self, sql: str, *args: Any) -> Any:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return True if self.trace_column else None

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


def _bus(conn: FakeConn) -> MessageBus:
    bus = MessageBus(FakeDB(conn), name="events")

    @bus.subscribe("orders", group="billing", durable=True)
    async def _handle(message):  # pragma: no cover - registration only
        return None

    return bus


def _bound(parent: str | None):
    return telemetry.outbound_context.set(None if parent is None else (parent, ""))


def _inserts(conn: FakeConn) -> list[tuple[str, tuple[Any, ...]]]:
    return [(sql, args) for sql, args in conn.calls if "INSERT INTO" in sql]


class TestADurableMessageCarriesTheContextOfItsPublish:
    async def test_a_traced_publish_writes_the_traceparent_on_every_row(self):
        conn = FakeConn()
        bus = _bus(conn)

        @bus.subscribe("orders", group="fulfilment", durable=True)
        async def _second(message):  # pragma: no cover - registration only
            return None

        token = _bound(PARENT)
        try:
            await bus.publish("orders", {"id": 1}, durable=True)
        finally:
            telemetry.outbound_context.reset(token)

        sql, args = _inserts(conn)[0]
        assert "trace_context" in sql
        # One traceparent shared by every group's row: they are one publish.
        assert args.count(PARENT) == 1
        assert args[-1] == PARENT
        assert len(_inserts(conn)) == 1, "one statement for the whole fan-out"
        # Every `VALUES` tuple names the shared bind, and every one names the
        # *same* one. Asserted on the statement rather than only on the column
        # list: a fake accepts an INSERT whose column count and value count
        # disagree, and a server answers `INSERT has more target columns than
        # expressions` -- which is what a mutant sweep found unguarded here.
        mark = f"${2 * 2 + 4}"  # two groups: channel, body, tenant, then 2 pairs
        assert sql.count(f", {mark})") == 2

    async def test_an_untraced_publish_writes_null_not_an_empty_string(self):
        conn = FakeConn()
        await _bus(conn).publish("orders", {"id": 1}, durable=True)
        sql, args = _inserts(conn)[0]
        assert "trace_context" in sql
        # Positional rather than `None in args`: the tenant is `''` and the
        # dedup key is `None`, so a membership test would pass whatever the
        # trace bind held. One bind for the whole fan-out, last.
        assert args[-1] is None

    async def test_a_consumer_runs_the_handler_under_the_publishers_trace(self):
        conn = FakeConn(
            row={
                "id": 7,
                "payload": json.dumps({"id": 1}),
                "tenant": "",
                "fence": 1,
                "attempts": 0,
                "trace_context": PARENT,
            }
        )
        bus = _bus(conn)
        seen: list[object] = []

        sub = next(s for s in bus._subs if s.durable)
        claimed = None
        other = _bound(OTHER)
        try:
            claimed = await bus._claim(sub)

            async def handler(message):
                seen.append(telemetry.outbound_context.get())

            object.__setattr__(sub, "handler", handler)
            await bus._deliver(sub, claimed)
        finally:
            telemetry.outbound_context.reset(other)

        assert claimed is not None
        assert claimed.trace_context == PARENT
        assert seen == [(PARENT, "")], (
            "the handler ran under the consumer's own context, so a message "
            "published by a request and handled in another service is two traces"
        )

    async def test_an_untraced_message_binds_none_rather_than_leaking(self):
        conn = FakeConn(
            row={
                "id": 7,
                "payload": json.dumps({"id": 1}),
                "tenant": "",
                "fence": 1,
                "attempts": 0,
                "trace_context": None,
            }
        )
        bus = _bus(conn)
        seen: list[object] = []
        sub = next(s for s in bus._subs if s.durable)

        other = _bound(OTHER)
        try:
            claimed = await bus._claim(sub)

            async def handler(message):
                seen.append(telemetry.outbound_context.get())

            object.__setattr__(sub, "handler", handler)
            await bus._deliver(sub, claimed)
        finally:
            telemetry.outbound_context.reset(other)

        assert seen == [None]

    async def test_the_context_is_reset_after_delivery(self):
        conn = FakeConn(
            row={
                "id": 7,
                "payload": json.dumps({}),
                "tenant": "",
                "fence": 1,
                "attempts": 0,
                "trace_context": PARENT,
            }
        )
        bus = _bus(conn)
        sub = next(s for s in bus._subs if s.durable)

        async def handler(message):
            return None

        object.__setattr__(sub, "handler", handler)
        await bus._deliver(sub, await bus._claim(sub))
        assert telemetry.outbound_context.get() is None

    async def test_the_context_is_reset_even_when_the_handler_raises(self):
        conn = FakeConn(
            row={
                "id": 7,
                "payload": json.dumps({}),
                "tenant": "",
                "fence": 1,
                "attempts": 0,
                "trace_context": PARENT,
            }
        )
        bus = _bus(conn)
        sub = next(s for s in bus._subs if s.durable)

        async def handler(message):
            raise RuntimeError("downstream down")

        object.__setattr__(sub, "handler", handler)
        await bus._deliver(sub, await bus._claim(sub))
        assert telemetry.outbound_context.get() is None
        assert any("attempts = attempts + 1" in sql for sql in conn.sqls())


class TestTheDurablePublishAccountsForItsConnection:
    async def test_a_publish_of_its_own_releases_what_it_acquired(self):
        conn = FakeConn()
        bus = MessageBus(FakeDB(conn), name="events")

        @bus.subscribe("orders", group="billing", durable=True)
        async def _handle(message):  # pragma: no cover - registration only
            return None

        await bus.publish("orders", {"id": 1}, durable=True)
        assert bus._db.acquired == 1
        assert bus._db.released == 1

    async def test_a_publish_inside_a_transaction_acquires_nothing(self):
        conn = FakeConn()
        bus = MessageBus(FakeDB(conn), name="events")

        @bus.subscribe("orders", group="billing", durable=True)
        async def _handle(message):  # pragma: no cover - registration only
            return None

        await bus.publish("orders", {"id": 1}, tx=conn, durable=True)
        assert bus._db.acquired == 0
        assert bus._db.released == 0


class TestTheSchemaMayBeOlderThanTheBuild:
    async def test_a_build_meeting_a_version_one_schema_still_publishes(self):
        conn = FakeConn(trace_column=False)
        await _bus(conn).publish("orders", {"id": 1}, durable=True)
        sql, args = _inserts(conn)[0]
        assert "trace_context" not in sql
        # The column list and the `VALUES` tuple are built from the same answer
        # and have to agree. A fake accepts an INSERT naming more placeholders
        # than it binds; PostgreSQL answers `bind message supplies 5 parameters,
        # but prepared statement requires 6`, which is a broken publish.
        assert sql.count("$") == len(args) + sql.count("$1") - 1
        assert f"${len(args) + 1}" not in sql

    async def test_a_build_meeting_a_version_one_schema_still_claims(self):
        conn = FakeConn(
            trace_column=False,
            row={
                "id": 7,
                "payload": json.dumps({}),
                "tenant": "",
                "fence": 1,
                "attempts": 0,
            },
        )
        bus = _bus(conn)
        sub = next(s for s in bus._subs if s.durable)
        claimed = await bus._claim(sub)
        assert claimed is not None
        assert claimed.trace_context is None
        select = next(sql for sql in conn.sqls() if "WITH claimable" in sql)
        assert "trace_context" not in select

    def test_version_one_of_the_component_has_no_trace_column(self):
        conn = FakeConn()
        component = _bus(conn).component()
        first = next(step for step in component.steps if step.version == 1)
        assert not any("trace_context" in s for s in first.statements)
        assert component.target_version == 2
        second = next(step for step in component.steps if step.version == 2)
        assert any("trace_context" in s for s in second.statements)

    async def test_the_catalog_is_asked_once_per_bus(self):
        conn = FakeConn()
        bus = _bus(conn)
        for _ in range(3):
            await bus.publish("orders", {"id": 1}, durable=True)
        probes = [sql for sql in conn.sqls() if "pg_attribute" in sql]
        assert len(probes) == 1, f"probed the catalog {len(probes)} times"


class TestEphemeralFanOutCarriesTenantContext:
    async def test_the_ephemeral_wire_format_wraps_the_payload_without_trace_context(self):
        conn = FakeConn()
        bus = MessageBus(FakeDB(conn), name="events")
        token = _bound(PARENT)
        try:
            await bus.publish("orders", {"id": 1})
        finally:
            telemetry.outbound_context.reset(token)
        sql, args = conn.calls[-1]
        assert sql == "SELECT pg_notify($1, $2)"
        envelope = json.loads(args[1].removeprefix(messaging_module._EPHEMERAL_PREFIX))
        assert envelope == {"tenant": "", "payload": {"id": 1}}
        assert PARENT not in args[1]


@pytest.mark.database
@pytest.mark.skipif(not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)")
async def test_a_durable_message_carries_its_trace_across_processes() -> None:
    from wreath.postgres import Database

    worker = os.environ.get("PYTEST_XDIST_WORKER", "solo")
    schema = f"wreath_trace_bus_{worker}"

    database = Database("test", _DSN, pools={"write": {"min_size": 1, "max_size": 2}})
    await database.start()
    try:
        publisher = MessageBus(database, name="publisher", schema=schema)

        @publisher.subscribe("orders", group="billing", durable=True)
        async def _registration(message):  # pragma: no cover - registration only
            return None

        connection = await database.acquire("write")
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await connection.execute(f'CREATE SCHEMA "{schema}"')
            for statement in publisher.component().statements():
                await connection.execute(statement)
        finally:
            await database.release("write", connection)

        token = _bound(PARENT)
        try:
            await publisher.publish("orders", {"id": 1}, durable=True)
        finally:
            telemetry.outbound_context.reset(token)

        consumer = MessageBus(database, name="consumer", schema=schema)
        seen: list[object] = []

        @consumer.subscribe("orders", group="billing", durable=True)
        async def _handle(message):
            seen.append(telemetry.outbound_context.get())

        sub = next(s for s in consumer._subs if s.durable)
        other = _bound(OTHER)
        try:
            claimed = await consumer._claim(sub)
            assert claimed is not None
            assert claimed.trace_context == PARENT
            await consumer._deliver(sub, claimed)
        finally:
            telemetry.outbound_context.reset(other)

        assert seen == [(PARENT, "")]
    finally:
        await database.stop()
