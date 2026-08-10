"""Durable subscriber groups are discovered fleet-wide, not per process.

A durable publish fans out one copy per subscriber *group*. Discovering those
groups from the subscriptions registered in the publishing process works right
up until the consumer lives somewhere else -- a service deployed later, or a
different service entirely against the same bus. Then the publisher enqueues
nothing for it: no error, no dead letter, the message simply never existed for
that group. That is data loss wearing a limitation's clothes.

These tests pin the fix and, just as importantly, pin that it cannot regress a
deployment that has not applied the new DDL: local registrations are *unioned*
with the persisted ones, so the worst case is exactly today's behaviour.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from _pgfidelity import check_for

from wreath.messaging import Message, MessageBus, NoSubscriberGroup


class FakeConnection:
    """Records every statement and replays scripted registry rows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        #: Rows `SELECT ... FROM ...message_groups` returns.
        self.group_rows: list[dict[str, str]] = []
        #: When set, `fetch` raises it -- the "DDL was never applied" case.
        self.fetch_error: Exception | None = None
        #: When set, `execute` raises it -- registration against a missing table.
        self.execute_error: Exception | None = None
        self.fetches = 0

    async def execute(self, sql: str, *args: Any) -> str:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        if self.execute_error is not None:
            raise self.execute_error
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        self.fetches += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        return list(self.group_rows)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        """The version-2 `trace_context` column probe, and nothing else.

        Answering `True` models a database the schema component has been applied
        to. A real `SELECT true ... WHERE` returns *no rows* when the column is
        absent, which the driver reads as `None` -- so that is the shape of the
        negative answer, not `False`.
        """
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return True

    def sqls(self) -> list[str]:
        return [sql for sql, _ in self.calls]

    def inserted_groups(self) -> list[str]:
        """The group each durable message row was enqueued for, in order.

        One statement carries every group, so the parameters are
        ``(channel, payload, tenant, group, dedup, group, dedup, ..., trace)``.
        The trailing traceparent is one bind for the whole fan-out -- every
        group's row is the same publish -- and it is sliced off rather than
        stepped over, because a pairwise walk to the end of the tuple would
        read it as a group.
        """
        groups: list[str] = []
        for sql, args in self.calls:
            if "INSERT INTO" in sql and ".messages" in sql:
                pairs = args[3:-1] if "trace_context" in sql else args[3:]
                groups.extend(pairs[::2])
        return groups

    def registrations(self) -> list[tuple[Any, ...]]:
        return [
            args
            for sql, args in self.calls
            if "INSERT INTO" in sql and "message_groups" in sql
        ]


class FakeDatabase:
    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.acquired = 0

    async def acquire(self, workload: str) -> FakeConnection:
        self.acquired += 1
        return self.connection

    async def release(self, workload: str, connection: FakeConnection) -> None:
        return None


class FakeSupervisor:
    """Records spawned tasks without running them.

    The refresher loop is driven directly in these tests, so its coroutine is
    closed rather than scheduled -- an un-awaited coroutine would warn.
    """

    def __init__(self) -> None:
        self.stopping = asyncio.Event()
        self.spawned: list[str] = []

    def spawn(self, name: str, coro: Any) -> None:
        self.spawned.append(name)
        coro.close()


def _bus(database: FakeDatabase, **kwargs: Any) -> MessageBus:
    return MessageBus(database, name="events", **kwargs)


def _durable(bus: MessageBus, channel: str, group: str) -> None:
    @bus.subscribe(channel, group=group, durable=True)
    async def handler(message: Message) -> None:
        pass


# --- the registry itself ------------------------------------------------------


def test_the_schema_declares_the_group_registry() -> None:
    sql = _bus(FakeDatabase()).schema_sql()
    assert "message_groups" in sql
    assert 'PRIMARY KEY (channel, "group")' in sql
    # Never auto-applied, like every other table this module owns.
    assert "CREATE TABLE IF NOT EXISTS" in sql


async def test_starting_registers_every_durable_group() -> None:
    database = FakeDatabase()
    bus = _bus(database)
    _durable(bus, "order_placed", "billing")
    _durable(bus, "order_placed", "fulfilment")

    await bus.start(FakeSupervisor())

    registered = sorted(args[:2] for args in database.connection.registrations())
    assert registered == [("order_placed", "billing"), ("order_placed", "fulfilment")]


async def test_an_ephemeral_subscription_registers_nothing() -> None:
    """Ephemeral fan-out has no groups; a row for one would be a lie."""
    database = FakeDatabase()
    bus = _bus(database)

    @bus.subscribe("order_placed")
    async def handler(message: Message) -> None:
        pass

    await bus.start(FakeSupervisor())
    assert database.connection.registrations() == []


async def test_registration_is_an_upsert_so_a_restart_is_a_heartbeat() -> None:
    """Idempotent across restarts, and the primary key serialises racing workers."""
    database = FakeDatabase()
    bus = _bus(database)
    _durable(bus, "order_placed", "billing")

    await bus.start(FakeSupervisor())

    sql = next(s for s in database.connection.sqls() if "message_groups" in s)
    assert 'ON CONFLICT (channel, "group") DO UPDATE' in sql
    assert "seen_at" in sql


async def test_a_registry_that_cannot_be_written_does_not_fail_startup() -> None:
    """The DDL is never auto-applied, so a missing table must not stop the bus."""
    database = FakeDatabase()
    database.connection.execute_error = RuntimeError('relation "message_groups"')
    bus = _bus(database)
    _durable(bus, "order_placed", "billing")

    await bus.start(FakeSupervisor())          # must not raise

    assert bus.group_registry_errors >= 1


# --- discovery ----------------------------------------------------------------


async def test_a_group_registered_by_another_process_receives_a_copy() -> None:
    """The whole point: this process never declared `analytics`."""
    database = FakeDatabase()
    database.connection.group_rows = [
        {"channel": "order_placed", "group": "analytics"},
    ]
    bus = _bus(database)

    await bus.start(FakeSupervisor())
    await bus.publish("order_placed", {"id": 1}, durable=True)

    assert database.connection.inserted_groups() == ["analytics"]


async def test_local_and_remote_groups_are_unioned_without_duplicates() -> None:
    database = FakeDatabase()
    database.connection.group_rows = [
        {"channel": "order_placed", "group": "analytics"},
        {"channel": "order_placed", "group": "billing"},     # also registered here
    ]
    bus = _bus(database)
    _durable(bus, "order_placed", "billing")

    await bus.start(FakeSupervisor())
    await bus.publish("order_placed", {"id": 1}, durable=True)

    assert database.connection.inserted_groups() == ["analytics", "billing"]


async def test_groups_on_another_channel_are_not_fanned_out_to() -> None:
    database = FakeDatabase()
    database.connection.group_rows = [
        {"channel": "trek_started", "group": "analytics"},
    ]
    bus = _bus(database)
    _durable(bus, "order_placed", "billing")

    await bus.start(FakeSupervisor())
    await bus.publish("order_placed", {"id": 1}, durable=True)

    assert database.connection.inserted_groups() == ["billing"]


async def test_a_local_group_still_receives_a_copy_with_an_empty_registry() -> None:
    """A deployment that has not applied the new DDL must behave as it did."""
    database = FakeDatabase()
    bus = _bus(database)
    _durable(bus, "order_placed", "billing")

    await bus.start(FakeSupervisor())
    await bus.publish("order_placed", {"id": 1}, durable=True)

    assert database.connection.inserted_groups() == ["billing"]


async def test_a_local_group_survives_an_unreachable_registry() -> None:
    """A lost copy is worse than a duplicate one, so the fallback is local."""
    database = FakeDatabase()
    database.connection.fetch_error = RuntimeError("the database is down")
    bus = _bus(database)
    _durable(bus, "order_placed", "billing")

    await bus.start(FakeSupervisor())          # must not raise
    await bus.publish("order_placed", {"id": 1}, durable=True)

    assert database.connection.inserted_groups() == ["billing"]
    assert bus.group_registry_errors >= 1


async def test_a_publish_without_start_falls_back_to_local_groups() -> None:
    """Scripts and tests publish on a bus the supervisor never started."""
    database = FakeDatabase()
    bus = _bus(database)
    _durable(bus, "order_placed", "billing")

    await bus.publish("order_placed", {"id": 1}, durable=True)

    assert database.connection.inserted_groups() == ["billing"]


# --- cost and freshness ---------------------------------------------------------


async def test_the_registry_is_not_read_once_per_publish() -> None:
    """Discovery is a timer, not a query on the write path."""
    database = FakeDatabase()
    database.connection.group_rows = [
        {"channel": "order_placed", "group": "analytics"},
    ]
    bus = _bus(database)

    await bus.start(FakeSupervisor())
    reads_after_start = database.connection.fetches

    for _ in range(10):
        await bus.publish("order_placed", {"id": 1}, durable=True)

    assert reads_after_start == 1
    assert database.connection.fetches == 1


async def test_a_group_registered_later_becomes_visible_on_the_next_refresh() -> None:
    database = FakeDatabase()
    bus = _bus(database)
    _durable(bus, "order_placed", "billing")

    await bus.start(FakeSupervisor())
    assert bus.known_groups("order_placed") == frozenset({"billing"})

    # Another service deploys and registers its consumer.
    database.connection.group_rows = [
        {"channel": "order_placed", "group": "analytics"},
    ]
    await bus._refresh_groups()

    assert bus.known_groups("order_placed") == frozenset({"analytics", "billing"})


async def test_the_refresher_is_supervised() -> None:
    database = FakeDatabase()
    supervisor = FakeSupervisor()
    await _bus(database).start(supervisor)
    assert any(name.endswith(":groups") for name in supervisor.spawned)


# --- the silent case, made observable -------------------------------------------


async def test_a_publish_with_no_group_anywhere_is_counted() -> None:
    """Still a no-op -- publishing before any consumer exists is legitimate --
    but no longer invisible."""
    database = FakeDatabase()
    bus = _bus(database)

    await bus.start(FakeSupervisor())
    await bus.publish("order_placed", {"id": 1}, durable=True)

    assert database.connection.inserted_groups() == []
    assert bus.unrouted_publishes == 1


async def test_a_caller_can_insist_that_someone_is_listening() -> None:
    database = FakeDatabase()
    bus = _bus(database)
    await bus.start(FakeSupervisor())

    with pytest.raises(NoSubscriberGroup, match="order_placed"):
        await bus.publish(
            "order_placed", {"id": 1}, durable=True, require_group=True
        )


async def test_requiring_a_group_passes_when_one_is_known() -> None:
    database = FakeDatabase()
    bus = _bus(database)
    _durable(bus, "order_placed", "billing")
    await bus.start(FakeSupervisor())

    await bus.publish("order_placed", {"id": 1}, durable=True, require_group=True)
    assert database.connection.inserted_groups() == ["billing"]


async def test_requiring_a_group_is_meaningless_for_an_ephemeral_publish() -> None:
    bus = _bus(FakeDatabase())
    with pytest.raises(ValueError, match="durable"):
        await bus.publish("order_placed", {"id": 1}, require_group=True)


async def test_known_groups_answers_the_deploy_time_question() -> None:
    """"Will anything receive what I publish?" -- checkable before shipping."""
    database = FakeDatabase()
    database.connection.group_rows = [
        {"channel": "order_placed", "group": "analytics"},
    ]
    bus = _bus(database)
    _durable(bus, "order_placed", "billing")

    await bus.start(FakeSupervisor())

    assert bus.known_groups("order_placed") == frozenset({"analytics", "billing"})
    assert bus.known_groups("trek_started") == frozenset()


# --- fan-out shape ---------------------------------------------------------------


async def test_one_doorbell_serves_the_whole_fan_out() -> None:
    """The doorbell only sets a wake event, so one NOTIFY covers every group.

    It mattered less when groups came from local registrations; with fleet-wide
    discovery a busy channel can have many, and one redundant NOTIFY per group
    is round trips spent saying the same thing.
    """
    database = FakeDatabase()
    database.connection.group_rows = [
        {"channel": "order_placed", "group": "analytics"},
        {"channel": "order_placed", "group": "billing"},
        {"channel": "order_placed", "group": "search"},
    ]
    bus = _bus(database)

    await bus.start(FakeSupervisor())
    before = sum(1 for sql in database.connection.sqls() if "pg_notify" in sql)
    await bus.publish("order_placed", {"id": 1}, durable=True)
    after = sum(1 for sql in database.connection.sqls() if "pg_notify" in sql)

    assert len(database.connection.inserted_groups()) == 3
    assert after - before == 1
