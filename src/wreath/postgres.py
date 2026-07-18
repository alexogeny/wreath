"""PostgreSQL driver facade and application-owned workload pools."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal, cast

Workload = Literal["security_read", "read", "write"]
_READ_ONLY = frozenset({"security_read", "read"})
_WORKLOADS = frozenset({"security_read", "read", "write"})


def _select_backend() -> ModuleType:
    if not os.environ.get("WREATH_PURE"):
        try:
            return importlib.import_module("wreath._native._postgres")
        except ImportError:
            pass

    from ._pure import postgres

    return postgres


_backend = _select_backend()
_implementation: str = _backend._implementation

Connection = _backend.Connection
InterfaceError = _backend.InterfaceError
OperationalError = _backend.OperationalError
PipelineFullError = _backend.PipelineFullError
PostgresError = _backend.PostgresError
ProtocolError = _backend.ProtocolError
Record = _backend.Record
connect = _backend.connect

Connector = Callable[[str], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class PoolConfig:
    min_size: int = 1
    max_size: int = 10
    max_queue: int = 256
    acquire_timeout: float = 5.0
    command_timeout: float = 30.0
    #: Automatic prepared plans each connection retains (LRU). A distinct SQL
    #: shape beyond this evicts the least-recently-used plan and closes its
    #: server-side statement, bounding per-connection and backend memory.
    statement_cache_size: int = 100

    def __post_init__(self) -> None:
        if self.min_size < 0:
            raise ValueError("pool min_size cannot be negative")
        if self.max_size < 1 or self.min_size > self.max_size:
            raise ValueError("pool max_size must be positive and at least min_size")
        if self.max_queue < 0:
            raise ValueError("pool max_queue cannot be negative")
        if self.acquire_timeout <= 0 or self.command_timeout <= 0:
            raise ValueError("pool timeouts must be positive")
        if self.statement_cache_size < 1:
            raise ValueError("pool statement_cache_size must be positive")


@dataclass(frozen=True, slots=True)
class FromDatabase:
    name: str | None = None
    workload: Workload = "read"

    def __post_init__(self) -> None:
        if self.workload not in _WORKLOADS:
            raise ValueError(f"unknown PostgreSQL workload: {self.workload}")


class Pool:
    """A bounded, exclusive-lease pool for one database workload."""

    __slots__ = (
        "_available", "_borrowed", "_condition", "_config", "_connections",
        "_connector", "_dsn", "_read_only", "_started", "_statements",
        "_stopping", "_waiters",
    )

    def __init__(
        self,
        dsn: str,
        config: PoolConfig,
        *,
        connector: Connector,
        read_only: bool,
        statements: Callable[[], tuple[Statement, ...]],
    ) -> None:
        self._dsn = dsn
        self._config = config
        self._connector = connector
        self._read_only = read_only
        self._statements = statements
        self._condition = asyncio.Condition()
        self._available: list[Any] = []
        self._connections: set[Any] = set()
        self._borrowed: set[Any] = set()
        self._waiters = 0
        self._started = False
        self._stopping = False

    @property
    def borrowed(self) -> int:
        return len(self._borrowed)

    @property
    def started(self) -> bool:
        return self._started

    async def _open(self) -> Any:
        connection = await self._connector(self._dsn)
        try:
            if self._read_only:
                await connection.execute("SET default_transaction_read_only = on")
            for statement in self._statements():
                await _prepare(connection, statement.sql)
        except BaseException:
            await connection.close()
            raise
        self._connections.add(connection)
        return connection

    async def start(self) -> None:
        if self._started:
            return
        self._stopping = False
        opened: list[Any] = []
        try:
            for _ in range(self._config.min_size):
                opened.append(await self._open())
        except BaseException:
            for connection in opened:
                await connection.close()
            self._connections.difference_update(opened)
            raise
        self._available.extend(opened)
        self._started = True

    async def acquire(self) -> Any:
        if not self._started or self._stopping:
            raise InterfaceError("PostgreSQL pool is not accepting acquisitions")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.acquire_timeout
        counted = False
        try:
            while True:
                async with self._condition:
                    if self._stopping:
                        raise InterfaceError("PostgreSQL pool is shutting down")
                    if self._available:
                        connection = self._available.pop()
                        self._borrowed.add(connection)
                        return connection
                    if len(self._connections) < self._config.max_size:
                        # Reserve capacity while opening outside the condition.
                        placeholder = object()
                        self._connections.add(placeholder)
                        break
                    if not counted:
                        if self._waiters >= self._config.max_queue:
                            raise InterfaceError("PostgreSQL pool queue is full")
                        self._waiters += 1
                        counted = True
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError("timed out acquiring PostgreSQL connection")
                    try:
                        async with asyncio.timeout(remaining):
                            await self._condition.wait()
                    except TimeoutError:
                        raise TimeoutError("timed out acquiring PostgreSQL connection") from None
            try:
                connection = await self._open()
            except BaseException:
                async with self._condition:
                    self._connections.discard(placeholder)
                    self._condition.notify()
                raise
            async with self._condition:
                self._connections.discard(placeholder)
                self._borrowed.add(connection)
            return connection
        finally:
            if counted:
                async with self._condition:
                    self._waiters -= 1

    async def release(self, connection: Any) -> None:
        async with self._condition:
            if connection not in self._borrowed:
                raise InterfaceError("connection was not borrowed from this pool")
            self._borrowed.remove(connection)
            if self._stopping or getattr(connection, "closed", False):
                self._connections.discard(connection)
                close = True
            else:
                self._available.append(connection)
                close = False
            self._condition.notify()
        if close and not getattr(connection, "closed", False):
            await connection.close()

    async def stop(self, grace_period: float) -> None:
        if not self._started:
            return
        self._stopping = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_period
        async with self._condition:
            self._condition.notify_all()
            while self._borrowed and loop.time() < deadline:
                try:
                    async with asyncio.timeout(deadline - loop.time()):
                        await self._condition.wait()
                except TimeoutError:
                    break
            idle = tuple(self._available)
            borrowed = tuple(self._borrowed)
            self._available.clear()
            self._borrowed.clear()
            self._connections.clear()
            self._started = False
            self._condition.notify_all()
        for connection in (*idle, *borrowed):
            if not getattr(connection, "closed", False):
                await connection.close()


class Statement:
    __slots__ = ("database", "name", "sql", "workload")

    def __init__(self, database: Database, name: str, sql: str, workload: Workload) -> None:
        self.database = database
        self.name = name
        self.sql = sql
        self.workload = workload

    async def _call(self, method: str, args: tuple[object, ...]) -> Any:
        connection = await self.database.acquire(self.workload)
        try:
            return await getattr(connection, method)(self.sql, *args)
        finally:
            await self.database.release(self.workload, connection)

    async def execute(self, *args: object) -> str:
        return cast(str, await self._call("execute", args))

    async def fetch(self, *args: object) -> list[Any]:
        return cast(list[Any], await self._call("fetch", args))

    async def fetchrow(self, *args: object) -> Any:
        return await self._call("fetchrow", args)

    async def fetchval(self, *args: object) -> Any:
        return await self._call("fetchval", args)

    async def map(
        self,
        method: str,
        argument_sets: Any,
        *,
        max_in_flight: int = 32,
    ) -> list[Any]:
        """Run this prepared statement once per argument set, in input order.

        Acquires one connection for the whole fan-out; each input becomes a
        distinct ``Sync``-delimited operation (duplicates are not coalesced or
        deduplicated), and results are returned in input order.
        """
        connection = await self.database.acquire(self.workload)
        try:
            return await connection.map(
                method, self.sql, argument_sets, max_in_flight=max_in_flight
            )
        finally:
            await self.database.release(self.workload, connection)


class Database:
    """One logical database with independently bounded workload pools."""

    __slots__ = (
        "_configs", "_connector", "_dsn", "_name", "_pools", "_statements",
        "_workload_dsns", "shutdown_timeout", "started",
    )

    def __init__(
        self,
        name: str,
        dsn: str,
        *,
        pools: Mapping[Workload, PoolConfig | Mapping[str, Any]] | None = None,
        workload_dsns: Mapping[Workload, str] | None = None,
        connector: Connector | None = None,
        shutdown_timeout: float = 10.0,
    ) -> None:
        if not name or not dsn:
            raise ValueError("database name and dsn are required")
        configured = pools or {"read": PoolConfig(), "write": PoolConfig(min_size=0)}
        self._configs = {_workload(key): _pool_config(value) for key, value in configured.items()}
        self._workload_dsns = {
            _workload(key): value for key, value in (workload_dsns or {}).items()
        }
        self._name = name
        self._dsn = dsn
        self._connector = connector
        self._pools: dict[Workload, Pool] = {}
        self._statements: dict[str, Statement] = {}
        self.shutdown_timeout = shutdown_timeout
        self.started = False

    @property
    def name(self) -> str:
        return self._name

    def statement(self, name: str, sql: str, *, workload: Workload = "read") -> Statement:
        workload = _workload(workload)
        if not name or not sql.strip():
            raise ValueError("statement name and SQL are required")
        if name in self._statements:
            raise ValueError(f"duplicate PostgreSQL statement: {name}")
        if workload not in self._configs:
            self._configs[workload] = PoolConfig()
        statement = Statement(self, name, sql, workload)
        self._statements[name] = statement
        return statement

    def _for_workload(self, workload: Workload) -> tuple[Statement, ...]:
        return tuple(item for item in self._statements.values() if item.workload == workload)

    async def start(self) -> None:
        if self.started:
            return
        connector = self._connector or connect
        for workload, config in self._configs.items():
            pool = Pool(
                self._workload_dsns.get(workload, self._dsn),
                config,
                connector=connector,
                read_only=workload in _READ_ONLY,
                statements=lambda workload=workload: self._for_workload(workload),
            )
            self._pools[workload] = pool
        started: list[Pool] = []
        try:
            for pool in self._pools.values():
                await pool.start()
                started.append(pool)
        except BaseException:
            for pool in reversed(started):
                await pool.stop(self.shutdown_timeout)
            self._pools.clear()
            raise
        self.started = True

    async def stop(self) -> None:
        if not self.started:
            return
        deadline = asyncio.get_running_loop().time() + self.shutdown_timeout
        for pool in reversed(tuple(self._pools.values())):
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await pool.stop(remaining)
        self.started = False

    def pool(self, workload: Workload) -> Pool:
        workload = _workload(workload)
        try:
            return self._pools[workload] if self.started else self._configured_pool(workload)
        except KeyError:
            raise KeyError(f"PostgreSQL workload is not configured: {workload}") from None

    def _configured_pool(self, workload: Workload) -> Pool:
        if workload not in self._configs:
            raise KeyError(workload)
        # Pool inspection before startup is intentionally unsupported except for
        # validating that a workload exists.
        raise InterfaceError("PostgreSQL pool has not started")

    async def acquire(self, workload: Workload = "read") -> Any:
        return await self.pool(workload).acquire()

    async def release(self, workload: Workload, connection: Any) -> None:
        await self.pool(workload).release(connection)


async def _prepare(connection: Any, sql: str) -> None:
    prepare = getattr(connection, "prepare", None)
    if prepare is not None:
        await prepare(sql)
        return
    # Current low-level backends do not yet expose a public prepare handle.
    # PREPARE still validates registered SQL at startup without executing it.
    fingerprint = hashlib.blake2s(sql.encode(), digest_size=8).hexdigest()
    await connection.execute(f"PREPARE wreath_{fingerprint} AS {sql}")


def _workload(value: str) -> Workload:
    if value not in _WORKLOADS:
        raise ValueError(f"unknown PostgreSQL workload: {value}")
    return cast(Workload, value)


def _pool_config(value: PoolConfig | Mapping[str, Any]) -> PoolConfig:
    return value if isinstance(value, PoolConfig) else PoolConfig(**value)


__all__ = [
    "Connection", "Database", "FromDatabase", "InterfaceError", "OperationalError",
    "PipelineFullError", "Pool", "PoolConfig", "PostgresError", "ProtocolError",
    "Record", "Statement", "Workload", "connect",
]
