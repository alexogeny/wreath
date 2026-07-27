"""PostgreSQL driver facade and application-owned workload pools."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from time import monotonic_ns as _monotonic_ns
from types import ModuleType
from typing import Any, Literal, cast

from ._flight_markers import (
    CAP_DB_PARAM as _CAP_DB_PARAM,
)
from ._flight_markers import (
    CAP_DB_ROW as _CAP_DB_ROW,
)
from ._flight_markers import (
    COV_EXTERNAL as _COV_EXTERNAL,
)
from ._flight_markers import (
    COV_PYTHON as _COV_PYTHON,
)
from ._flight_markers import (
    PH_DB_POOL_WAIT as _PH_DB_POOL_WAIT,
)
from ._flight_markers import (
    PH_DB_QUERY as _PH_DB_QUERY,
)
from ._flight_markers import (
    capture_marker as _capture_marker,
)
from ._flight_markers import (
    phase_marker as _phase_marker,
)
from ._locks import AdvisoryLock, AdvisoryTryLock, SingletonRunner

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
_DEFAULT_CONNECTOR = connect

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
    #: Approximate retained bytes for automatic plans on each connection.
    statement_cache_bytes: int = 4 * 1024 * 1024

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
        if self.statement_cache_bytes < 1:
            raise ValueError("pool statement_cache_bytes must be positive")


@dataclass(frozen=True, slots=True)
class FromDatabase:
    name: str | None = None
    workload: Workload = "read"

    def __post_init__(self) -> None:
        if self.workload not in _WORKLOADS:
            raise ValueError(f"unknown PostgreSQL workload: {self.workload}")


@dataclass(frozen=True, slots=True)
class PoolSnapshot:
    """What the pool looks like right now, for a status page or a pacer.

    `queue_high_water` is the only field that is not instantaneous: it is the
    deepest the wait queue has ever been, because the interesting question --
    "did anything ever wait?" -- has no answer a sampler can be relied on to
    catch. A pass that paces against pool pressure needs to know the queue was
    ten deep for fifty milliseconds, and polling `waiters` will usually miss it.
    """

    borrowed: int
    available: int
    waiters: int
    max_size: int
    queue_high_water: int


class Pool:
    """A bounded, exclusive-lease pool for one database workload."""

    __slots__ = (
        "_available", "_borrowed", "_condition", "_config", "_connections",
        "_connector", "_dsn", "_high_water", "_read_only", "_started",
        "_statements", "_stopping", "_waiters",
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
        self._high_water = 0
        self._started = False
        self._stopping = False

    @property
    def borrowed(self) -> int:
        return len(self._borrowed)

    def snapshot(self) -> PoolSnapshot:
        """Mirrors `HTTPClient.snapshot()`, so both pools read the same way."""
        return PoolSnapshot(
            borrowed=len(self._borrowed),
            available=len(self._available),
            waiters=self._waiters,
            max_size=self._config.max_size,
            queue_high_water=self._high_water,
        )

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
                        # Recorded on the way in: a sampler polling `waiters`
                        # will usually miss a queue that formed and drained
                        # between two polls, and that queue is the whole signal.
                        self._high_water = max(self._high_water, self._waiters)
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


def _encode_db_params(params: Any) -> bytes:
    """A deterministic byte encoding of query parameters for forensic capture.

    Only reached on the Forensic-armed, dependency-permitting path. `repr` is
    stable for a given argument set and handles every parameter type; the native
    capture then redacts it (hash/mask/bounded-raw) per the arm's dependency
    disposition, so raw values only ever persist when the policy allowed it.
    """
    return repr(params).encode("utf-8", "replace")


def _encode_db_rows(rows: Any) -> bytes:
    """A deterministic byte encoding of a query's result for forensic capture.

    Only reached on the Forensic-armed, dependency-permitting path (rows are
    dependency data, redacted by the arm's dependency disposition). `repr` is
    stable and handles rows, a single row, a scalar, or `None`; the native
    capture bounds/redacts it, so raw values persist only when policy allowed it.
    """
    return repr(rows).encode("utf-8", "replace")


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
            marker = _phase_marker.get(None)
            if marker is None:
                return await getattr(connection, method)(self.sql, *args)
            # Forensic dependency capture rides inside the phase gate: only a
            # Detailed-armed request has a phase marker at all, and only a
            # Forensic arm that permits dependencies binds the capturer. Params
            # are captured before the call so a failing statement still records
            # what it was asked to run; result rows are captured after it returns.
            capture = _capture_marker.get(None)
            if capture is not None and args:
                capture(_CAP_DB_PARAM, _encode_db_params(args))
            start = _monotonic_ns()
            try:
                result = await getattr(connection, method)(self.sql, *args)
                if capture is not None and method != "execute":
                    capture(_CAP_DB_ROW, _encode_db_rows(result))
                return result
            finally:
                # Recorded in a finally so a failed statement still shows the
                # time it spent at the database before raising.
                marker(_PH_DB_QUERY, self.database._flight_dep_id,
                       _COV_EXTERNAL, _monotonic_ns() - start)
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
        distinct `Sync`-delimited operation (duplicates are not coalesced or
        deduplicated), and results are returned in input order.
        """
        connection = await self.database.acquire(self.workload)
        try:
            marker = _phase_marker.get(None)
            if marker is None:
                return await connection.map(
                    method, self.sql, argument_sets, max_in_flight=max_in_flight
                )
            # Capture the fan-out's argument sets only when they are already
            # materialized -- never drain a generator, which would change what
            # the query runs (same rule as request-body capture).
            capture = _capture_marker.get(None)
            if (
                capture is not None
                and isinstance(argument_sets, (list, tuple))
                and argument_sets
            ):
                capture(_CAP_DB_PARAM, _encode_db_params(argument_sets))
            start = _monotonic_ns()
            try:
                results = await connection.map(
                    method, self.sql, argument_sets, max_in_flight=max_in_flight
                )
                if capture is not None:
                    capture(_CAP_DB_ROW, _encode_db_rows(results))
                return results
            finally:
                # One DB_QUERY for the whole fan-out: it is one acquisition and
                # one Sync-delimited pipeline from the request's point of view.
                marker(_PH_DB_QUERY, self.database._flight_dep_id,
                       _COV_EXTERNAL, _monotonic_ns() - start)
        finally:
            await self.database.release(self.workload, connection)


class Database:
    """One logical database with independently bounded workload pools."""

    __slots__ = (
        "_configs", "_connector", "_dsn", "_flight_dep_id", "_name", "_pools",
        "_register_lock", "_statements", "_workload_dsns", "shutdown_timeout",
        "started",
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
        # Guards the duplicate check in `statement`, which is check-then-act and
        # therefore not a guard at all across threads -- see the comment there.
        self._register_lock = threading.Lock()
        # Metadata-image ID for phase attribution; stamped by the app when the
        # flight recorder joins live objects to the image (0 = unattributed).
        self._flight_dep_id = 0
        self.shutdown_timeout = shutdown_timeout
        self.started = False

    @property
    def name(self) -> str:
        return self._name

    def statement(self, name: str, sql: str, *, workload: Workload = "read") -> Statement:
        """Register one prepared statement, refusing a name already claimed.

        The duplicate check and the assignment run under a lock because
        separately they are not a check at all: two threads registering the same
        name both passed `name in self._statements` before either assigned, so
        *both* succeeded and the loser was left holding a `Statement` that no
        pool ever prepares -- `_for_workload` only ever sees the survivor. A
        guard that exists to catch two subsystems claiming one name silently
        became a lost write instead of the refusal it was written to be.

        The window spans a `Statement` construction, so it does not need a
        free-threaded interpreter to be real; free-threading merely removes the
        GIL that makes it rare. Registration happens at startup and on a store's
        first use, never per request, so a plain lock costs nothing worth
        measuring. `store.py`'s `_prepare_lock` guards the tier above this one.
        """
        workload = _workload(workload)
        if not name or not sql.strip():
            raise ValueError("statement name and SQL are required")
        with self._register_lock:
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
        # Snapshot under the registration lock rather than iterating the live
        # dict. `statement()` inserts a workload the first time one is named, and
        # it holds `_register_lock` while doing so -- but this loop awaits a
        # connection per workload, so it stays open for milliseconds and a
        # concurrent registration raises `dictionary changed size during
        # iteration`. Measured: 400 of 400 trials when the two align.
        #
        # Snapshotting rather than holding the lock across the loop is
        # deliberate: `_register_lock` is a `threading.Lock`, so holding it
        # across an `await` would block every other thread for the whole of
        # connection setup. A workload registered after the snapshot is not
        # started here, which is the pre-existing behaviour -- `pool()` raises
        # for it, as it already did.
        with self._register_lock:
            configs = tuple(self._configs.items())
        for workload, config in configs:
            connector = self._connector or connect
            if connector is _DEFAULT_CONNECTOR:
                connector = partial(
                    connector,
                    statement_cache_size=config.statement_cache_size,
                    statement_cache_bytes=config.statement_cache_bytes,
                )
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
        # Armed-request pool-wait phase; every other request pays exactly the
        # ContextVar read. This is the one acquisition seam, so Statement,
        # Statement.map, and direct acquire callers are all covered.
        marker = _phase_marker.get(None)
        if marker is None:
            return await self.pool(workload).acquire()
        start = _monotonic_ns()
        connection = await self.pool(workload).acquire()
        marker(_PH_DB_POOL_WAIT, self._flight_dep_id, _COV_PYTHON,
               _monotonic_ns() - start)
        return connection

    async def release(self, workload: Workload, connection: Any) -> None:
        await self.pool(workload).release(connection)

    # -- distributed advisory locks ----------------------------------------
    # Cluster-global mutexes built on PostgreSQL advisory locks. See
    # `wreath._locks` for the connection-affinity contract; xact-scoped locks
    # live on `wreath.orm.session.Session.lock`.

    def lock(
        self,
        key: str,
        *,
        namespace: str | None = None,
        mode: str = "exclusive",
        workload: Workload = "write",
    ) -> AdvisoryLock:
        """A blocking, session-scoped advisory lock held across an `async with`.

        The lock pins one connection from *workload* (the primary by default) for
        the block's duration. Prefer `Session.lock(scope="xact")` for
        request-path exclusion; use this for long-lived fleet locks.
        """
        return AdvisoryLock(self, key, namespace=namespace, mode=mode, workload=workload)

    def try_lock(
        self,
        key: str,
        *,
        timeout: float | None = None,
        namespace: str | None = None,
        mode: str = "exclusive",
        workload: Workload = "write",
    ) -> AdvisoryTryLock:
        """A non-blocking advisory lock: `async with db.try_lock(k) as held:`.

        *held* is the handle when acquired or `None` otherwise. With *timeout*
        set, acquisition blocks up to that many seconds via `lock_timeout`.
        """
        return AdvisoryTryLock(
            self, key, timeout=timeout, namespace=namespace, mode=mode, workload=workload
        )

    def run_singleton(
        self,
        key: str,
        work: Callable[[], Awaitable[Any]],
        *,
        namespace: str | None = None,
        workload: Workload = "write",
        poll_interval: float = 5.0,
    ) -> SingletonRunner:
        """Run *work* once across the fleet, guarded by an advisory lock.

        *work* is a zero-argument callable returning a fresh awaitable. Returns a
        handle with `await handle.stop()`; wire it through `on_startup` /
        `on_shutdown`. The guarded critical section must be idempotent.
        """
        return SingletonRunner(
            self, key, work, namespace=namespace, workload=workload,
            poll_interval=poll_interval,
        )


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
    "AdvisoryLock", "AdvisoryTryLock", "Connection", "Database", "FromDatabase",
    "InterfaceError", "OperationalError", "PipelineFullError", "Pool", "PoolConfig",
    "PoolSnapshot", "PostgresError", "ProtocolError", "Record", "SingletonRunner",
    "Statement",
    "Workload", "connect",
]
