"""Injection seams, stuck keys, and per-handler scoping (report 23: R-02, R-03,
B-01, B-02, G-05, G-09, G-12, G-14, G-23, B-08)."""

from __future__ import annotations

import pytest


class TestStoreExpressionSeam:
    """R-02/R-03: `upsert` validates the column *keys* and interpolates their
    expressions verbatim, and `window()` takes a raw string. Nothing in the
    signature says which arguments are SQL."""

    def test_a_bare_string_expression_is_refused(self):
        from wreath.store import Keyed, PostgresStore

        store = PostgresStore(object(), Keyed(table="t", key="k"))
        with pytest.raises(TypeError, match="Sql|placeholder"):
            store.upsert(values={"k": "$1"}, update={"k": "'; DROP TABLE t --"})

    def test_a_placeholder_is_accepted(self):
        from wreath.store import Keyed, PostgresStore

        store = PostgresStore(object(), Keyed(table="t", key="k"))
        sql = store.upsert(values={"k": "$1"}, update={"k": "$2"})
        assert "$2" in sql

    def test_an_explicitly_marked_expression_is_accepted(self):
        from wreath.store import Keyed, PostgresStore, Sql

        store = PostgresStore(object(), Keyed(table="t", key="k"))
        sql = store.upsert(values={"k": "$1"}, update={"k": Sql("clock_timestamp()")})
        assert "clock_timestamp()" in sql

    def test_a_window_string_must_be_marked_too(self):
        from wreath.store import Keyed, PostgresStore

        store = PostgresStore(object(), Keyed(table="t", ttl=60.0))
        assert "$1" in store.window("$1")          # a placeholder is fine
        with pytest.raises(TypeError):
            store.window("(SELECT 1)")


class TestIdempotencyOperability:
    """B-02: a key claimed by a process that then died stays claimed for the
    whole TTL, answering 409 with no operator lever. G-05: `after` reaches into
    `request._state`."""

    async def test_a_stuck_key_can_be_released(self):
        from wreath.middleware.idempotency import (
            IdempotencyMiddleware,
            MemoryIdempotencyStore,
        )

        store = MemoryIdempotencyStore()
        middleware = IdempotencyMiddleware(store=store)
        await store.reserve("k")                  # claimed, never completed
        assert (await store.reserve("k"))[0] == "in_flight"

        await middleware.release("k")
        assert (await store.reserve("k"))[0] == "fresh"

    async def test_in_flight_conflicts_are_counted(self):
        from wreath.auth import Identity
        from wreath.middleware.idempotency import (
            IdempotencyMiddleware,
            MemoryIdempotencyStore,
        )

        store = MemoryIdempotencyStore()
        middleware = IdempotencyMiddleware(store=store)

        class _Request:
            method = "POST"
            path = "/orders"
            identity = Identity(id="u1")

            def header(self, name, default=None):
                return "abc" if name == "idempotency-key" else default

        await store.reserve(middleware._key(_Request()))
        response = await middleware.action(_Request())
        assert response.status == 409
        assert middleware.conflicts == 1

    async def test_state_is_reached_through_the_public_api(self):
        import inspect

        from wreath.middleware import idempotency

        source = inspect.getsource(idempotency.IdempotencyMiddleware.after)
        assert "request._state" not in source, "the middleware still reaches into Request internals"


class TestSubscriberRegistrationRace:
    """G-14: `_has_write_subscribers()` is read at flush, so a subscriber that
    registers during an open transaction misses that transaction's names."""

    async def test_a_subscriber_that_registers_mid_transaction_still_hears(self):
        from wreath import _orm_events
        from wreath.orm import Mapped, Model, column
        from wreath.orm.registry import Registry
        from wreath.orm.session import Session
        from wreath.orm.types import Int64, Text

        class Note(Model, table="rgb_notes"):
            id: Mapped[int] = column(Int64, primary_key=True)
            body: Mapped[str] = column(Text)

        class _Connection:
            async def execute(self, sql, *args):
                return "INSERT 0 1"

            async def fetchrow(self, sql, *args):
                return None

        class _Database:
            name = "app"

            async def acquire(self, workload):
                return _Connection()

            async def release(self, workload, connection):
                pass

        session = Session(Registry(_Database(), [Note], validate_schema="off"), "write")
        heard: list = []

        async with session.begin():
            note = Note(id=1, body="hello")
            session.add(note)
            await session.flush()
            # Registered *after* the flush but before the commit -- a `@cached`
            # handler decorated at startup, a broadcast attached by a later hook.
            _orm_events.subscribe_writes(heard.append)

        try:
            assert heard == [frozenset({"Note"})], (
                "the transaction's writes were never announced to a subscriber "
                "that arrived before the commit"
            )
        finally:
            _orm_events.unsubscribe_writes(heard.append)


class TestReapThreadSafety:
    """G-12: `_reap` mutates the subscriber list from a weakref callback, which
    can run on any thread."""

    def test_reaping_takes_a_lock(self):
        import inspect

        from wreath import _orm_events

        source = inspect.getsource(_orm_events)
        assert "_lock" in source, "list mutation from a GC callback is unguarded"

    def test_an_owned_subscription_still_reaps(self):
        from wreath import _orm_events

        class _Owner:
            pass

        owner = _Owner()
        seen: list = []
        _orm_events.subscribe_writes(seen.append, owner=owner)
        assert _orm_events.has_subscribers()
        del owner
        import gc

        gc.collect()
        _orm_events.publish_write(frozenset({"User"}))
        assert seen == [], "a collected owner's subscription still fired"


class TestDurableFanOut:
    """G-23: durable fan-out issues one INSERT per group, serially, inside the
    caller's transaction."""

    async def test_one_statement_covers_every_group(self):
        from wreath.messaging import MessageBus

        statements: list[str] = []

        class _Connection:
            async def execute(self, sql, *args):
                statements.append(sql)
                return "OK"

            async def fetchval(self, sql, *args):
                # The version-2 `trace_context` column probe, and nothing else.
                # `None` models a schema still on version 1, which is what this
                # double's INSERT assertion is about -- the fan-out shape, not
                # the trace column.
                return None

        class _Database:
            async def acquire(self, workload):
                return _Connection()

            async def release(self, workload, connection):
                pass

        bus = MessageBus(_Database(), name="events")
        bus._remote_groups = {"orders": frozenset({"a", "b", "c"})}
        await bus.publish("orders", {"id": 1}, durable=True)

        inserts = [sql for sql in statements if sql.startswith("INSERT")]
        assert len(inserts) == 1, f"{len(inserts)} statements for three groups"


class TestBusStats:
    """B-08: the counters are on the object with no way to read them as a set,
    so nothing can export them without knowing each name."""

    def test_the_counters_are_readable_as_a_mapping(self):
        from wreath.messaging import MessageBus

        class _Database:
            pass

        stats = MessageBus(_Database(), name="events").stats()
        for name in (
            "unrouted_publishes", "group_registry_errors", "doorbell_reconnects",
            "handler_errors", "delivery_errors",
        ):
            assert name in stats

    def test_a_job_runner_reports_its_counters_too(self):
        from wreath.jobs import JobRunner

        class _Database:
            pass

        stats = JobRunner(_Database(), name="work").stats()
        for name in ("run_errors", "sweep_errors", "schedule_errors", "dead_lettered"):
            assert name in stats


class TestStorePurgeReporting:
    """B-01: `purge` returns the driver's status string, so a caller cannot tell
    how much it removed without parsing it."""

    async def test_purge_reports_how_many_rows_went(self):
        from wreath.store import Keyed, PostgresStore

        class _Statement:
            async def execute(self, *args):
                return "DELETE 17"

        class _Database:
            def statement(self, name, sql, workload="write"):
                return _Statement()

        store = PostgresStore(_Database(), Keyed(table="t"))
        assert await store.purge_count() == 17

    async def test_an_unparseable_status_reports_nothing(self):
        from wreath.store import Keyed, PostgresStore

        class _Statement:
            async def execute(self, *args):
                return "OK"

        class _Database:
            def statement(self, name, sql, workload="write"):
                return _Statement()

        store = PostgresStore(_Database(), Keyed(table="t"))
        assert await store.purge_count() is None
