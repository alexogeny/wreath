from __future__ import annotations

import json
from typing import Any

from _pgfidelity import check_for

from wreath.messaging import Message, MessageBus


class FakeConn:
    def __init__(self, *, fetchrow: Any = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._fetchrow = fetchrow

    async def execute(self, sql: str, *args: Any) -> str:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return "OK"

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return self._fetchrow

    async def fetchval(self, sql: str, *args: Any) -> Any:
        """The version-2 `trace_context` column probe, and nothing else.

        `None` -- a real `SELECT true ... WHERE` returns *no rows* when the
        column is absent, which the driver reads as None, so that is the shape
        of the negative answer rather than `False`. This double's rows carry no
        `trace_context`, so answering yes would model a database whose catalog
        and whose rows disagree. The traced world is modelled in
        `tests/messaging/test_trace.py`; between them both schema versions run.
        """
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return None

    def sqls(self) -> list[str]:
        return [sql for sql, _ in self.calls]


class FakeDB:
    name = "main"

    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    async def acquire(self, workload: str) -> FakeConn:
        return self.conn

    async def release(self, workload: str, connection: FakeConn) -> None:
        pass


def _bus(conn: FakeConn) -> MessageBus:
    return MessageBus(FakeDB(conn), name="events")


def _msg(**kw: Any) -> Message:
    base = dict(channel="orders", group="g", tenant="", payload={}, id=3, fence=1)
    base.update(kw)
    return Message(**base)  # type: ignore[arg-type]


async def test_publish_ephemeral_emits_pg_notify_with_json_body() -> None:
    conn = FakeConn()
    await _bus(conn).publish("orders", {"id": 1})
    sql, args = conn.calls[-1]
    assert "pg_notify" in sql
    assert args[1] == json.dumps({"id": 1})


async def test_publish_durable_inserts_one_row_per_group() -> None:
    conn = FakeConn()
    bus = _bus(conn)

    @bus.subscribe("orders", group="fulfil", durable=True)
    async def h(m: Message) -> None:
        pass

    await bus.publish("orders", {"id": 1}, durable=True)
    inserts = [s for s in conn.sqls() if "INSERT INTO" in s]
    assert inserts and "messages" in inserts[0]
    assert any("pg_notify" in s for s in conn.sqls())


async def test_publish_durable_with_no_groups_is_a_noop() -> None:
    conn = FakeConn()
    await _bus(conn).publish("orders", {"id": 1}, durable=True)
    assert not any("INSERT INTO" in s for s in conn.sqls())


async def test_claim_hydrates_message_or_returns_none() -> None:
    # `attempts` rides the claim so a retry's backoff can grow with it.
    row = {"id": 7, "payload": json.dumps({"x": 1}), "tenant": "", "fence": 2, "attempts": 0}
    conn = FakeConn(fetchrow=row)
    bus = _bus(conn)

    @bus.subscribe("orders", group="g", durable=True)
    async def h(m: Message) -> None:
        pass

    message = await bus._claim(bus._subs[0])
    assert message is not None
    assert (message.id, message.payload, message.fence) == (7, {"x": 1}, 2)

    empty = _bus(FakeConn(fetchrow=None))

    @empty.subscribe("orders", group="g", durable=True)
    async def h2(m: Message) -> None:
        pass

    assert await empty._claim(empty._subs[0]) is None


async def test_deliver_ack_completes() -> None:
    conn = FakeConn()
    bus = _bus(conn)

    @bus.subscribe("orders", group="g", durable=True)
    async def h(m: Message) -> None:
        m.ack()

    await bus._deliver(bus._subs[0], _msg())
    assert "state='done'" in conn.calls[-1][0]


async def test_deliver_reject_dead_letters() -> None:
    conn = FakeConn()
    bus = _bus(conn)

    @bus.subscribe("orders", group="g", durable=True)
    async def h(m: Message) -> None:
        m.reject()

    await bus._deliver(bus._subs[0], _msg())
    assert "state='dead'" in conn.calls[-1][0]


async def test_deliver_handler_error_retries_with_attempt_growth() -> None:
    conn = FakeConn()
    bus = _bus(conn)

    @bus.subscribe("orders", group="g", durable=True)
    async def h(m: Message) -> None:
        raise RuntimeError("x")

    await bus._deliver(bus._subs[0], _msg())
    sql = conn.calls[-1][0]
    assert "attempts = attempts + 1" in sql
    assert "WHERE id=$1 AND fence=$2" in sql
