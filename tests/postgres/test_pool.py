from __future__ import annotations

import asyncio
from typing import Any

import pytest

from wreath.postgres import Database, PoolConfig


class FakeConnection:
    def __init__(self, label: int) -> None:
        self.label = label
        self.closed = False
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.prepared: list[str] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return "OK"

    async def fetchrow(self, sql: str, *args: object) -> dict[str, object]:
        self.calls.append((sql, args))
        return {"connection": self.label, "args": args}

    async def fetchval(self, sql: str, *args: object) -> object:
        self.calls.append((sql, args))
        return args[0] if args else 1

    async def prepare(self, sql: str) -> None:
        self.prepared.append(sql)

    async def close(self) -> None:
        self.closed = True


class Connector:
    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self.dsns: list[str] = []

    async def __call__(self, dsn: str) -> FakeConnection:
        self.dsns.append(dsn)
        connection = FakeConnection(len(self.connections))
        self.connections.append(connection)
        return connection


@pytest.mark.asyncio
async def test_workloads_use_isolated_pools_with_one_dsn() -> None:
    connector = Connector()
    db = Database(
        "main",
        "postgresql://primary/app",
        pools={
            "security_read": PoolConfig(min_size=1, max_size=1),
            "read": PoolConfig(min_size=1, max_size=1),
            "write": PoolConfig(min_size=1, max_size=1),
        },
        connector=connector,
    )

    await db.start()
    try:
        assert connector.dsns == ["postgresql://primary/app"] * 3
        assert db.pool("security_read") is not db.pool("read")
        assert db.pool("read") is not db.pool("write")
        assert connector.connections[0].calls == [("SET default_transaction_read_only = on", ())]
        assert connector.connections[1].calls == [("SET default_transaction_read_only = on", ())]
        assert connector.connections[2].calls == []
    finally:
        await db.stop()

    assert all(connection.closed for connection in connector.connections)


@pytest.mark.asyncio
async def test_saturated_read_pool_does_not_starve_security_pool() -> None:
    connector = Connector()
    db = Database(
        "main",
        "postgresql://primary/app",
        pools={
            "security_read": PoolConfig(min_size=1, max_size=1),
            "read": PoolConfig(min_size=1, max_size=1),
        },
        connector=connector,
    )
    await db.start()
    try:
        read = await db.acquire("read")
        security = await asyncio.wait_for(db.acquire("security_read"), timeout=0.05)
        assert security is not read
        await db.release("security_read", security)
        await db.release("read", read)
    finally:
        await db.stop()


@pytest.mark.asyncio
async def test_registered_statement_prepares_and_routes_to_its_workload() -> None:
    connector = Connector()
    db = Database(
        "main",
        "postgresql://primary/app",
        pools={"security_read": PoolConfig(min_size=1, max_size=1)},
        connector=connector,
    )
    statement = db.statement(
        "security.resolve_session",
        "select user_id from sessions where token_hash = $1",
        workload="security_read",
    )

    await db.start()
    try:
        connection = connector.connections[0]
        assert connection.prepared == [statement.sql]
        assert await statement.fetchrow("digest") == {
            "connection": 0,
            "args": ("digest",),
        }
        assert connection.calls[-1] == (statement.sql, ("digest",))
        assert db.pool("security_read").borrowed == 0
    finally:
        await db.stop()


def test_unused_security_pool_is_not_configured_implicitly() -> None:
    db = Database(
        "main",
        "postgresql://primary/app",
        pools={"read": PoolConfig()},
        connector=Connector(),
    )

    with pytest.raises(KeyError, match="security_read"):
        db.pool("security_read")


@pytest.mark.asyncio
async def test_invalid_registered_statement_fails_startup_and_closes_connection() -> None:
    connector = Connector()

    async def reject(sql: str) -> None:
        raise RuntimeError(f"invalid SQL: {sql}")

    original_call = connector.__call__

    async def connect(dsn: str) -> FakeConnection:
        connection = await original_call(dsn)
        connection.prepare = reject  # type: ignore[method-assign]
        return connection

    db = Database(
        "main",
        "postgresql://primary/app",
        pools={"read": PoolConfig(min_size=1)},
        connector=connect,
    )
    db.statement("broken", "not valid sql", workload="read")

    with pytest.raises(RuntimeError, match="invalid SQL"):
        await db.start()

    assert connector.connections[-1].closed
    assert not db.started
