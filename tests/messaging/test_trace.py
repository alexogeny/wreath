"""Plan 01 stage 3, the bus half: a durable message names the publish that caused it.

The plan's own rule -- "context rides the row, never the NOTIFY" -- was written
for `wreath.jobs`, where `pg_notify($1, '')` is deliberately empty and the row
is the message. **It does not transfer literally to this module**, because the
two tiers are opposites:

* **Durable publish** writes one row per subscriber group and rings an empty
  doorbell. That is the direct analogue of a job, and it carries the context on
  the row. That is what these tests pin.
* **Ephemeral publish** has no row at all: `pg_notify($1, $2)` carries the
  user's payload *as* the message. There is nowhere to put a traceparent that
  is not inside that payload, so propagating it would mean wrapping every
  ephemeral message in an envelope -- a breaking change to a live wire format
  between processes. That is deferred, deliberately and in writing; see
  `docs/reference/roadmap.md` and `MessageBus.publish`. The test below pins the
  wire format *unchanged*, so the deferral is a decision the suite defends
  rather than an omission nobody noticed.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from wreath import _pytest_plugin, telemetry
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
        self.calls.append((sql, args))
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        return self.row

    async def fetchval(self, sql: str, *args: Any) -> Any:
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
        """An empty string is a *value*, and would match `IS NOT NULL`.

        The same distinction `wreath.jobs.JobRunner.enqueue` had to make: a
        forensic lookup filtering on the column being present would otherwise
        find every message ever published.
        """
        conn = FakeConn()
        await _bus(conn).publish("orders", {"id": 1}, durable=True)
        sql, args = _inserts(conn)[0]
        assert "trace_context" in sql
        # Positional rather than `None in args`: the tenant is `''` and the
        # dedup key is `None`, so a membership test would pass whatever the
        # trace bind held. One bind for the whole fan-out, last.
        assert args[-1] is None

    async def test_a_consumer_runs_the_handler_under_the_publishers_trace(self):
        """The other half of the seam, and the reason the column exists.

        The consumer is a different process on a different day. Delivered with
        an unrelated context bound, so a handler that merely inherited the
        ambient value would fail this.
        """
        conn = FakeConn(
            row={
                "id": 7, "payload": json.dumps({"id": 1}), "tenant": "",
                "fence": 1, "attempts": 0, "trace_context": PARENT,
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
        """The staleness rule every other seam in this plan had to adopt.

        A consumer running thousands of messages must not hand message N+1 the
        context of message N, and a message published untraced must not inherit
        whatever the worker happens to hold.
        """
        conn = FakeConn(
            row={
                "id": 7, "payload": json.dumps({"id": 1}), "tenant": "",
                "fence": 1, "attempts": 0, "trace_context": None,
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
                "id": 7, "payload": json.dumps({}), "tenant": "",
                "fence": 1, "attempts": 0, "trace_context": PARENT,
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
        """A handler that raises drives retry, and must not leave a binding behind."""
        conn = FakeConn(
            row={
                "id": 7, "payload": json.dumps({}), "tenant": "",
                "fence": 1, "attempts": 0, "trace_context": PARENT,
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
        """The outbox guarantee: nothing goes out behind the caller's back.

        Including the column probe, which is why `_carries_trace` takes the
        executor the statement will use rather than opening a connection.
        """
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
        """Losing the trace is a degradation; losing the message is not."""
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
                "id": 7, "payload": json.dumps({}), "tenant": "",
                "fence": 1, "attempts": 0,
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


class TestEphemeralFanOutIsDeliberatelyUnchanged:
    async def test_the_ephemeral_wire_format_is_the_payload_and_nothing_else(self):
        """The deferral, pinned.

        Ephemeral fan-out has only the NOTIFY, so carrying a traceparent means
        wrapping the user's payload in an envelope -- a breaking change to a
        live wire format between processes, which needs a versioned envelope an
        old-build subscriber can still read through a rolling deploy. The
        8000-byte NOTIFY bound is not the obstacle; a traceparent is 55 bytes.

        This test exists so that when someone does build the envelope, they
        have to come here and say so, rather than discovering during a deploy
        that half the fleet cannot read the other half's messages.
        """
        conn = FakeConn()
        bus = MessageBus(FakeDB(conn), name="events")
        token = _bound(PARENT)
        try:
            await bus.publish("orders", {"id": 1})
        finally:
            telemetry.outbound_context.reset(token)
        sql, args = conn.calls[-1]
        assert sql == "SELECT pg_notify($1, $2)"
        assert json.loads(args[1]) == {"id": 1}, (
            "the ephemeral payload is the user's message verbatim; wrapping it "
            "in an envelope is a wire-format break and needs a versioned one"
        )
        assert PARENT not in args[1]


@pytest.mark.database
@pytest.mark.skipif(not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)")
async def test_a_durable_message_carries_its_trace_across_processes() -> None:
    """Publisher and consumer as two buses, against a real server.

    The consumer is built after the publisher is gone and delivers with a
    different context bound, which is the closest a test gets to "another
    service picked this up".
    """
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
