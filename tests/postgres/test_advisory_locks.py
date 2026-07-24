"""Advisory-lock facade: SQL emission, connection affinity, and pool accounting.

These exercise the real ``Database`` pool + ``Session`` code over a fake
connector that records emitted SQL, so no live PostgreSQL is required. The
integration behaviours that only a real server can prove (contention blocks,
``lock_timeout`` fires, xact locks auto-release on ROLLBACK, a killed connection
frees the lock) belong in a DSN-gated suite alongside the other postgres
integration tests.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from wreath.orm.errors import SessionError
from wreath.orm.session import Session, TenantContext
from wreath.postgres import Database, PoolConfig, PostgresError


class FakeConnection:
    def __init__(self, label: int) -> None:
        self.label = label
        self.closed = False
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return "OK"

    async def fetchval(self, sql: str, *args: object) -> object:
        self.calls.append((sql, args))
        # pg_try_advisory_lock(...) -> bool; the namespace arg is truthy, so the
        # default lease is "acquired". Blocking locks/unlocks return void (None).
        if "pg_try_advisory_lock" in sql:
            return True
        return None

    async def prepare(self, sql: str) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class Connector:
    def __init__(self, factory: Any = FakeConnection) -> None:
        self.factory = factory
        self.connections: list[Any] = []

    async def __call__(self, dsn: str) -> Any:
        connection = self.factory(len(self.connections))
        self.connections.append(connection)
        return connection


def _sqls(connection: FakeConnection) -> list[str]:
    return [sql for sql, _ in connection.calls]


async def _write_db(factory: Any = FakeConnection) -> tuple[Database, Connector]:
    connector = Connector(factory)
    database = Database(
        "main",
        "postgresql://primary/app",
        pools={"write": PoolConfig(min_size=1, max_size=1)},
        connector=connector,
    )
    await database.start()
    return database, connector


@pytest.mark.asyncio
async def test_lock_acquires_and_unlocks_on_the_same_connection() -> None:
    database, connector = await _write_db()
    pool = database.pool("write")
    try:
        assert pool.borrowed == 0
        async with database.lock("job:rebalance"):
            # The lock withholds exactly one connection from the pool.
            assert pool.borrowed == 1
            connection = connector.connections[0]
            assert (
                "SELECT pg_advisory_lock(hashtext($1::text), hashtext($2::text))",
                ("main", "job:rebalance"),
            ) in connection.calls
        # Released back to the pool, unlocked on the same backend.
        assert pool.borrowed == 0
        connection = connector.connections[0]
        assert connection is connector.connections[0]
        assert (
            "SELECT pg_advisory_unlock(hashtext($1::text), hashtext($2::text))",
            ("main", "job:rebalance"),
        ) in connection.calls
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_shared_mode_uses_shared_functions() -> None:
    database, connector = await _write_db()
    try:
        async with database.lock("catalog", mode="shared"):
            pass
        sqls = _sqls(connector.connections[0])
        assert any("pg_advisory_lock_shared(" in s for s in sqls)
        assert any("pg_advisory_unlock_shared(" in s for s in sqls)
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_namespace_defaults_to_database_name_and_can_override() -> None:
    database, connector = await _write_db()
    try:
        async with database.lock("k", namespace="tenant_acme"):
            pass
        _, args = next(
            call for call in connector.connections[0].calls
            if "pg_advisory_lock(" in call[0]
        )
        assert args == ("tenant_acme", "k")
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_try_lock_yields_handle_when_available() -> None:
    database, connector = await _write_db()
    pool = database.pool("write")
    try:
        async with database.try_lock("k") as held:
            assert held is not None
            assert pool.borrowed == 1
        assert pool.borrowed == 0
        assert any("pg_try_advisory_lock(" in s for s in _sqls(connector.connections[0]))
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_try_lock_yields_none_and_releases_when_unavailable() -> None:
    class Busy(FakeConnection):
        async def fetchval(self, sql: str, *args: object) -> object:
            self.calls.append((sql, args))
            if "pg_try_advisory_lock" in sql:
                return False
            return None

    database, connector = await _write_db(Busy)
    pool = database.pool("write")
    try:
        async with database.try_lock("k") as held:
            assert held is None
        # The connection is returned even though nothing was held.
        assert pool.borrowed == 0
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_try_lock_timeout_translates_lock_not_available_to_none() -> None:
    class TimesOut(FakeConnection):
        async def fetchval(self, sql: str, *args: object) -> object:
            self.calls.append((sql, args))
            if "pg_advisory_lock(" in sql:
                raise PostgresError("canceled on lock timeout", sqlstate="55P03")
            return None

    database, connector = await _write_db(TimesOut)
    try:
        async with database.try_lock("k", timeout=0.5) as held:
            assert held is None
        sqls = _sqls(connector.connections[0])
        # lock_timeout is set for the bounded wait and reset before release so it
        # never leaks onto the pooled connection.
        assert any(s == "SET lock_timeout = 500" for s in sqls)
        assert any(s == "SET lock_timeout = DEFAULT" for s in sqls)
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_try_lock_reraises_non_timeout_errors() -> None:
    class Boom(FakeConnection):
        async def fetchval(self, sql: str, *args: object) -> object:
            self.calls.append((sql, args))
            if "pg_advisory_lock(" in sql:
                raise PostgresError("deadlock detected", sqlstate="40P01")
            return None

    database, _ = await _write_db(Boom)
    try:
        with pytest.raises(PostgresError):
            async with database.try_lock("k", timeout=0.5):
                pass
    finally:
        await database.stop()


def test_read_only_workload_is_rejected() -> None:
    database = Database("main", "postgresql://primary/app")
    with pytest.raises(ValueError, match="primary"):
        database.lock("k", workload="read")
    with pytest.raises(ValueError, match="primary"):
        database.try_lock("k", workload="security_read")


def test_invalid_mode_is_rejected() -> None:
    database = Database("main", "postgresql://primary/app")
    with pytest.raises(ValueError, match="exclusive"):
        database.lock("k", mode="upgrade")


@pytest.mark.asyncio
async def test_run_singleton_leader_runs_work_and_releases() -> None:
    database, connector = await _write_db()
    ran = asyncio.Event()

    async def work() -> None:
        ran.set()

    try:
        runner = database.run_singleton("leader:reaper", work, poll_interval=0.01)
        await asyncio.wait_for(ran.wait(), 1.0)
        await runner.stop()
        connection = connector.connections[0]
        sqls = _sqls(connection)
        assert any("pg_try_advisory_lock(" in s for s in sqls)
        assert any("pg_advisory_unlock(" in s for s in sqls)
        # The dedicated connection is returned to the pool after leadership ends.
        assert database.pool("write").borrowed == 0
    finally:
        await database.stop()


# -- Session (xact-scoped) ---------------------------------------------------


class _SessionConn:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return "OK"

    async def fetchval(self, sql: str, *args: object) -> object:
        self.calls.append((sql, args))
        return None

    async def close(self) -> None:
        self.closed = True


class _FakeDatabase:
    name = "main"

    def __init__(self) -> None:
        self.connection = _SessionConn()

    async def acquire(self, workload: str) -> Any:
        return self.connection

    async def release(self, workload: str, connection: Any) -> None:
        pass


class _FakeRegistry:
    schema_mode = None

    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database


class _IsolatedMode:
    kind = "isolated"


class _IsolatedRegistry:
    schema_mode = _IsolatedMode()

    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database


@pytest.mark.asyncio
async def test_session_xact_lock_rides_the_pinned_connection() -> None:
    database = _FakeDatabase()
    session = Session(_FakeRegistry(database), "write")
    session._depth = 1  # simulate being inside `async with session.begin():`
    await session.lock("account:42")
    sql, args = database.connection.calls[-1]
    assert sql == "SELECT pg_advisory_xact_lock(hashtext($1::text), hashtext($2::text))"
    assert args == ("main", "account:42")


@pytest.mark.asyncio
async def test_session_xact_lock_requires_an_open_transaction() -> None:
    database = _FakeDatabase()
    session = Session(_FakeRegistry(database), "write")
    with pytest.raises(SessionError, match="open transaction"):
        await session.lock("account:42")


@pytest.mark.asyncio
async def test_session_scope_directs_callers_to_database_lock() -> None:
    database = _FakeDatabase()
    session = Session(_FakeRegistry(database), "write")
    session._depth = 1
    with pytest.raises(SessionError, match="database.lock"):
        await session.lock("k", scope="session")


@pytest.mark.asyncio
async def test_session_tenant_schema_is_folded_into_the_namespace() -> None:
    database = _FakeDatabase()
    registry = _IsolatedRegistry(database)
    session = Session(registry, "write", tenant=TenantContext(schema="tenant_acme"))
    session._depth = 1
    await session.lock("invoice:1")
    _, args = database.connection.calls[-1]
    # Advisory locks ignore search_path, so the tenant schema must be in the key.
    assert args == ("tenant_acme", "invoice:1")
