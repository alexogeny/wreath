"""MessageBus unit tests using a fake database (real publish/schema paths)."""

from __future__ import annotations

import json

import pytest
from _pgfidelity import check_statement

from wreath._jobcore import PayloadTooLarge
from wreath.messaging import MessageBus


class FakeConnection:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql, *args):
        check_statement(sql, args)
        self.calls.append((sql, args))
        return "OK"


class FakeDatabase:
    def __init__(self):
        self.connection = FakeConnection()
        self.acquired = 0

    async def acquire(self, workload):
        self.acquired += 1
        return self.connection

    async def release(self, workload, connection):
        pass


def _bus(db):
    return MessageBus(db, name="events")


async def test_publish_ephemeral_notifies():
    db = FakeDatabase()
    bus = _bus(db)
    await bus.publish("booking_created", {"id": 1})
    call = next(c for c in db.connection.calls if "pg_notify" in c[0])
    wire, body = call[1]
    assert wire.startswith("wm_")
    assert json.loads(body) == {"id": 1}


async def test_publish_ephemeral_oversized_rejected():
    bus = _bus(FakeDatabase())
    with pytest.raises(PayloadTooLarge):
        await bus.publish("booking_created", {"blob": "x" * 8000})


def test_durable_subscription_requires_group():
    bus = _bus(FakeDatabase())
    with pytest.raises(ValueError):
        @bus.subscribe("booking_created", durable=True)
        async def handler(message):
            pass


async def test_publish_durable_fans_out_per_group():
    db = FakeDatabase()
    bus = _bus(db)

    @bus.subscribe("booking_created", group="billing", durable=True)
    async def to_billing(message):
        pass

    @bus.subscribe("booking_created", group="fulfilment", durable=True)
    async def to_fulfilment(message):
        pass

    await bus.publish("booking_created", {"id": 9}, durable=True)
    inserts = [args for sql, args in db.connection.calls if "INSERT INTO" in sql]
    # One statement, every group: (channel, payload, tenant, group, dedup, ...)
    assert len(inserts) == 1
    groups = sorted(inserts[0][3::2])
    assert groups == ["billing", "fulfilment"]


def test_schema_sql_has_messages_table():
    sql = _bus(FakeDatabase()).schema_sql()
    assert "CREATE TABLE IF NOT EXISTS" in sql and ".messages" in sql
    assert "messages_claim_idx" in sql
    assert "messages_dedup_idx" in sql
