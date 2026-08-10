"""PostgreSQL driver facade and application-owned workload pools."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections import deque
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
from ._native import _core
from ._native import extension as _extension
from ._sparsevec import MAX_SPARSEVEC_DIM, MAX_SPARSEVEC_NNZ, SparseVector

Workload = Literal["security_read", "read", "write"]
_READ_ONLY = frozenset({"security_read", "read"})
_WORKLOADS = frozenset({"security_read", "read", "write"})


def _select_backend() -> ModuleType:
    backend = _extension("_postgres")
    if backend is not None:
        return cast(ModuleType, backend)

    from . import _pgdriver as postgres

    return postgres


_backend = _select_backend()
_implementation: str = _backend._implementation
_NATIVE_STATEMENT_SUBMIT = _implementation == "native"
_NATIVE_STATEMENT_AWAIT = _NATIVE_STATEMENT_SUBMIT and hasattr(
    _backend, "_statement_call"
)

if _NATIVE_STATEMENT_SUBMIT:
    _core.template_record_configure(_backend._RECORD_C_API)

Connection = _backend.Connection
InterfaceError = _backend.InterfaceError
OperationalError = _backend.OperationalError
PipelineFullError = _backend.PipelineFullError
PostgresError = _backend.PostgresError
ProtocolError = _backend.ProtocolError
Record = _backend.Record
RecordBatch = _backend.RecordBatch
connect = _backend.connect
_DEFAULT_CONNECTOR = connect


def _driver_infer_oid() -> Callable[[object], int]:
    """The parameter-OID inference the driver actually performs.

    There is no C `_infer_oid`. `_native/postgres/pipeline.c` reads this very
    function out of `wreath._pgdriver` at module init and calls it per parameter
    of a cold operation, so the Python one *is* what the driver runs. The
    `getattr` comes first anyway: the day a C `_infer_oid` lands, every derived
    caller picks it up without an edit.
    """
    inference = getattr(_backend, "_infer_oid", None)
    if inference is not None:
        return cast(Callable[[object], int], inference)

    from ._pgdriver import _infer_oid

    return _infer_oid


#: What the shipped driver does, resolved once against whichever backend loaded.
#: Doubles and probes derive their behaviour from these rather than restating a
#: table, so a codec landing changes them with nobody editing them.
#:
#: **Ask here, never `wreath._pgdriver`.** That module is the driver's Python
#: base class, and the extension table `register_extension_codec` writes into
#: belongs to the backend that owns it -- so a `vector` column decoded through
#: the base class comes back as raw bytes in the same process that registered
#: it.
_decode_value: Callable[[int, int, bytes | None], Any] = _backend._decode_value
_is_transaction_sql: Callable[[str], bool] = _backend._is_transaction_sql
_infer_oid = _driver_infer_oid()

Connector = Callable[[str], Awaitable[Any]]

#: `Statement._call` reaches the driver's result methods with
#: `getattr(connection, method)`, which allocates a bound method per query.
#: Resolving the four unbound functions once and calling them with the instance
#: was tried, behind an exact-type check (a connection is not always a
#: `Connection` -- the pool suites lease duck-typed fakes). It measured 3.48us
#: for the Statement+pool facade before and 3.48us after: one bound-method
#: allocation is not resolvable against a 35us query, and the branch it needed
#: made `_call` worse to read. Left as it was, so the next reader does not
#: re-derive it.


def register_extension_codec(name: str, oid: int, kind: int) -> None:
    """Teach the active codec that `oid` carries an extension wire format.

    Every built-in OID is a compile-time constant the codec dispatches on
    directly. An extension type's OID is assigned by `CREATE EXTENSION`, so it
    cannot be one -- the codec instead keeps a small fixed table, consulted only
    after every built-in OID has already missed, and this is what writes into it.

    Called once per type per process, from
    `wreath.orm.introspection.resolve_extension_types`, before any statement
    binds a value of that type. The table is read-only afterwards; re-registering
    the same name with the same OID is a no-op, and re-registering it with a
    different one raises, because a codec table that changed under a running
    connection would decode rows with the wrong rules.

    Args:
        name: The catalog type name, such as `"vector"`.
        oid: The OID this database assigned it.
        kind: Which wire format to frame it with -- one of the `EXT_KIND_*`
            constants in `wreath.orm.types`.
    """
    _backend._register_extension_type(name, oid, kind)


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
    #: Concurrent `Statement` operations allowed to share one connection.
    #:
    #: The driver has always been able to hold several operations in flight on
    #: a connection; the pool is what decided nobody could reach one at the
    #: same time as anybody else. Raising this lets concurrent statements batch
    #: into the same flight -- one write and one backend wakeup for several
    #: queries instead of one each, which is what every other driver on the
    #: Fortunes board has been doing.
    #:
    #: **4 because it was measured.** Swept on the Fortunes board, six workers
    #: on three physical cores, 512 concurrent connections against a pool of 56:
    #:
    #:     depth  1   49,289 req/s      (exclusive leasing)
    #:     depth  2   53,993 req/s      +9.5%
    #:     depth  4   59,318 req/s      +20.3%   <- peak
    #:     depth  8   57,804 req/s      +17.3%
    #:
    #: It turns over past 4: a deeper share queues more operations on one
    #: connection than a single flight can carry, which is latency without
    #: throughput to show for it.
    #:
    #: `1` restores exclusive leasing exactly, and is what to set when a caller
    #: needs a connection to itself for reasons the pool cannot see.
    #: `Database.acquire()` is exclusive whatever this says, because a caller
    #: holding an explicit lease may open a transaction and the driver refuses
    #: concurrent operations once it has -- so `Statement.map`, `_Transaction`
    #: and every direct `acquire` are unaffected by this setting.
    pipeline_depth: int = 4

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
        if self.pipeline_depth < 1:
            raise ValueError("pool pipeline_depth must be at least 1")


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
    """A bounded lease pool for one database workload.

    A lease is exclusive by default. `PoolConfig.pipeline_depth` above 1 lets
    concurrent *statements* share a connection so their operations batch into
    one flight -- see `acquire`, and the note on that field for the measurement
    that chose the depth. Anything holding an explicit lease still has the
    connection to itself, because a shared one cannot carry a transaction.
    """

    __slots__ = (
        "_available", "_borrowed", "_config", "_connections",
        "_connector", "_drained", "_dsn", "_high_water", "_read_only",
        "_shared", "_started", "_statements", "_stopping", "_waiters",
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
        self._available: list[Any] = []
        self._connections: set[Any] = set()
        self._borrowed: set[Any] = set()
        # One future per queued caller, oldest first. This replaces an
        # `asyncio.Condition`, and the replacement is not a refactor: the
        # Condition was taken on *every* acquire and release, including the
        # uncontended path where the work it guards is a `list.pop` and a
        # `set.add` with no await between them. On a single-threaded loop that
        # sequence cannot interleave, so the lock protected nothing and cost
        # two lock round-trips per database request on the path whose whole
        # design goal is eliding frames.
        self._waiters: deque[asyncio.Future[Any]] = deque()
        self._drained: asyncio.Future[None] | None = None
        #: Share counts for connections lent out for batching, by identity.
        #: A connection appears here only while it has shared borrowers; an
        #: exclusive lease never enters it, which is what keeps the two kinds
        #: of loan from being mistaken for each other on release.
        self._shared: dict[int, tuple[Any, int]] = {}
        self._high_water = 0
        self._started = False
        self._stopping = False

    @property
    def borrowed(self) -> int:
        """How many connections are leased out right now.

        A lease is exclusive, so this is also how many are unavailable to the
        next `acquire`. `snapshot()` reports it alongside the idle count, the
        queue depth and the high-water mark, and is the one to reach for when
        more than one of them is wanted at a consistent moment.
        """
        return len(self._borrowed)

    def counters(self) -> Any:
        """This pool's counters, for `wreath.metrics.collect`.

        `queue_high_water` is the one worth scraping: it is the only field that
        is not instantaneous, so it answers "did anything ever wait?" — which a
        sampler polling `waiters` will usually miss entirely.
        """
        from .metrics import Counters

        reading = self.snapshot()
        return Counters(
            subsystem="pool",
            instance=getattr(self, "_name", "") or "default",
            values={
                "borrowed": reading.borrowed,
                "available": reading.available,
                "waiters": reading.waiters,
                "max_size": reading.max_size,
                "queue_high_water": reading.queue_high_water,
            },
        )

    def snapshot(self) -> PoolSnapshot:
        """Mirrors `HTTPClient.snapshot()`, so both pools read the same way."""
        return PoolSnapshot(
            borrowed=len(self._borrowed),
            available=len(self._available),
            waiters=len(self._waiters),
            max_size=self._config.max_size,
            queue_high_water=self._high_water,
        )

    @property
    def started(self) -> bool:
        """Whether `start()` has completed and `acquire()` will be answered.

        False before the first `start()`, and false again after `stop()`
        returns. It does not go false while a stop is draining — use it to ask
        "has this pool been brought up", not "is it still healthy".
        """
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
        """Open `min_size` connections and begin accepting acquisitions.

        Each connection is opened, set read-only when this pool serves a
        read workload, and has every statement registered for that workload
        prepared on it before it joins the idle set — so a `Statement` never
        pays a prepare on the request path.

        All-or-nothing: if any of them fails, the ones already opened are closed
        and the exception propagates with the pool still unstarted, rather than
        leaving a half-sized pool that works until it doesn't. Calling it on an
        already-started pool does nothing.
        """
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

    async def acquire(self, *, shared: bool = False) -> Any:
        """Lease one connection. Always pair it with `release`.

        `shared=True` allows up to `pipeline_depth` concurrent holders, so their
        operations batch into one flight; the default is an exclusive lease.

        Three outcomes, tried in this order: an idle connection is handed over
        immediately; otherwise a new one is opened if the pool is below
        `max_size`; otherwise the caller queues until somebody releases one.

        Every way of failing is an exception rather than a `None`, and each says
        which limit was hit:

        Raises:
            InterfaceError: the pool has not started, or is shutting down, or
                the wait queue already holds `max_queue` callers. A full queue
                refuses immediately rather than growing without bound.
            TimeoutError: `acquire_timeout` seconds elapsed while queued. The
                deadline covers the wait only — a connection being opened for
                this caller is not interrupted by it.
        """
        if shared and self._config.pipeline_depth > 1:
            connection = self.try_acquire_shared()
            if connection is not None:
                return connection
            return await self._acquire_shared()
        if not self._started or self._stopping:
            raise InterfaceError("PostgreSQL pool is not accepting acquisitions")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.acquire_timeout
        while True:
            # Fast path. Nothing awaits between the test and the take, so on a
            # single-threaded loop this cannot interleave with another caller.
            if self._available:
                connection = self._available.pop()
                self._borrowed.add(connection)
                return connection

            if len(self._connections) < self._config.max_size:
                # Reserve capacity while opening, so two callers cannot both
                # decide there is room for the last connection.
                placeholder = object()
                self._connections.add(placeholder)
                try:
                    connection = await self._open()
                except BaseException:
                    self._connections.discard(placeholder)
                    self._wake_one()
                    raise
                self._connections.discard(placeholder)
                self._borrowed.add(connection)
                return connection

            if len(self._waiters) >= self._config.max_queue:
                raise InterfaceError("PostgreSQL pool queue is full")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("timed out acquiring PostgreSQL connection")

            # The deadline is a timer on this caller's own future rather than
            # `asyncio.timeout`, which requires `asyncio.current_task()` and so
            # raised `RuntimeError` outright on the server's elided-call fast
            # path -- under load only, because an uncontended acquire returns
            # above without ever reaching here.
            waiter: asyncio.Future[Any] = loop.create_future()
            self._waiters.append(waiter)
            # Recorded on the way in: a sampler polling `waiters` will usually
            # miss a queue that formed and drained between two polls, and that
            # queue is the whole signal.
            self._high_water = max(self._high_water, len(self._waiters))
            timer = loop.call_later(remaining, self._expire, waiter)
            try:
                handed = await waiter
            except BaseException:
                # Only the failing paths leave a waiter in the queue.
                # `_hand_off`, `_wake_one` and `stop` all `popleft` before they
                # resolve, so on the success path this waiter is already gone
                # and `deque.remove` walks the whole queue to raise ValueError
                # for `_unqueue` to swallow. That queue is `concurrency -
                # max_size` deep -- about 456 entries at the Fortunes board's
                # 512 against a pool of 56 -- so the scan was O(queue) per
                # acquisition, and worst under exactly the load the queue
                # exists for. `_expire` and cancellation are the two that do
                # need it: neither touches the deque.
                self._unqueue(waiter)
                raise
            finally:
                timer.cancel()
                self._settle_drain()
            if handed is not None:
                # `release` handed this caller a connection directly and has
                # already marked it borrowed on its behalf.
                return handed
            # Woken because capacity freed rather than a connection arriving;
            # go round and try to open one.

    def try_acquire_shared(self) -> Any | None:
        """Take an idle connection into the shared set, or return None.

        The uncontended shared lease, done in the caller's frame. Returning
        `None` means "this one needs the coroutine" -- nothing idle, not
        started, or shutting down -- and the caller falls through to
        `_acquire_shared`, which owns every decision this does not make.

        It is a separate synchronous method rather than a branch inside
        `_acquire_shared` because the cost being removed *is* the coroutine: a
        lease and its release created and stepped seven of them to move one
        connection between a deque and a dict, which measured 18,732
        instructions with no query attached to it. `wreath-decomp --suite
        calibrate` prices a non-suspending await at 95.7ns against 49.8ns for a
        guarded synchronous call, and seven of the first is most of that lease.

        The policy stays in one place: this is the same branch `_acquire_shared`
        opens with, in the same order, and every other case is still that
        method's.
        """
        # At depth 1 a "shared" lease *is* an exclusive one, so there is no fast
        # path to take: `acquire()` owns that case whole, including the queueing
        # this method cannot do.
        if self._config.pipeline_depth <= 1:
            return None
        if not self._started or self._stopping:
            return None
        # An idle connection is always the best choice, and taking it here also
        # brings it into the shared set.
        if self._available:
            connection = self._available.pop()
            self._shared[id(connection)] = (connection, 1)
            return connection
        # Otherwise the least-loaded shared connection, if any has room. This
        # selection has to be here rather than only in the coroutine: a pool
        # sized so that requests *do* collide -- which is the point of sharing --
        # reaches this branch far more often than the idle one, so leaving it
        # behind would mean the fast path never ran under the load it is for.
        #
        # The scan stops at the first connection carrying one borrower because
        # nothing can be lower, which is also what the loop below it would have
        # settled on: it takes the first strict minimum, and 1 is the minimum.
        best_key = None
        best_count = self._config.pipeline_depth
        for key, (_connection, count) in self._shared.items():
            if count < best_count:
                best_key, best_count = key, count
                if count == 1:
                    break
        if best_key is None:
            return None
        connection, count = self._shared[best_key]
        self._shared[best_key] = (connection, count + 1)
        return connection

    def try_release_shared(self, connection: Any) -> bool:
        """Give back a shared lease without suspending, or return False.

        True means the lease is fully returned. False means the connection has
        to go back through `release`, which is every case that can await:
        the pool is shutting down, or the connection reports itself closed and
        has to be closed here.

        Raises:
            InterfaceError: this connection holds no shared lease from this
                pool. Refused here rather than deferred to the slow path so a
                double release is caught in the frame that made it.
        """
        # Depth 1 leases exclusively, so the connection is in `_borrowed` rather
        # than `_shared` and the ordinary release is the only one that applies.
        if self._config.pipeline_depth <= 1:
            return False
        key = id(connection)
        entry = self._shared.get(key)
        if entry is None:
            raise InterfaceError("connection was not borrowed from this pool")
        count = entry[1]
        if count > 1:
            self._shared[key] = (connection, count - 1)
            return True
        if self._stopping or getattr(connection, "closed", False):
            return False
        # Last borrower out, and the connection is reusable: idle it exactly as
        # the exclusive release does -- hand-off first, so a queued caller gets
        # it without it ever touching the idle list.
        del self._shared[key]
        if not self._hand_off(connection):
            self._available.append(connection)
        self._settle_drain()
        return True

    async def _acquire_shared(self) -> Any:
        """Lease a connection that concurrent statements may share.

        Up to `pipeline_depth` callers hold one at a time, so their operations
        queue on the same connection and the driver batches them into one
        flight. Pair it with `release_shared`.

        Falls back to `acquire()` at depth 1, which is exactly exclusive
        leasing, so the serial path stays one code path rather than a special
        case of this one.

        The connection with the fewest borrowers wins, which spreads load
        rather than filling one connection before touching the next: a deep
        queue on one connection is latency that a `max_emitted_operations`
        flight cannot drain in one go, while the same operations spread across
        idle connections go out in parallel.
        """
        depth = self._config.pipeline_depth
        if depth <= 1:
            return await self.acquire()

        if not self._started or self._stopping:
            raise InterfaceError("PostgreSQL pool is not accepting acquisitions")

        # Everything that can be decided without suspending lives in
        # `try_acquire_shared`, and `acquire` has already tried it -- but not
        # every caller arrives through `acquire`, so this is not an assumption.
        connection = self.try_acquire_shared()
        if connection is not None:
            return connection

        # Every shared connection is full and none is idle: open one if the
        # pool has room, else wait exactly as an exclusive caller would.
        connection = await self.acquire()
        self._borrowed.discard(connection)
        self._shared[id(connection)] = (connection, 1)
        return connection

    async def _release_shared(self, connection: Any) -> None:
        """Return a shared lease. The connection idles when the last one goes."""
        if self._config.pipeline_depth <= 1:
            await self.release(connection)
            return
        key = id(connection)
        entry = self._shared.get(key)
        if entry is None:
            raise InterfaceError("connection was not borrowed from this pool")
        _connection, count = entry
        if count > 1:
            self._shared[key] = (connection, count - 1)
            return
        del self._shared[key]
        # Last borrower out: it becomes an ordinary idle connection again, so
        # every path that reasons about idleness -- hand-off, drain, shutdown --
        # sees it in the one place it expects.
        self._borrowed.add(connection)
        await self.release(connection)

    def _expire(self, waiter: asyncio.Future[Any]) -> None:
        """Fail one queued caller when its own deadline passes."""
        if not waiter.done():
            waiter.set_exception(
                TimeoutError("timed out acquiring PostgreSQL connection")
            )

    def _unqueue(self, waiter: asyncio.Future[Any]) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass

    def _hand_off(self, connection: Any) -> bool:
        """Give a returned connection straight to the longest-waiting caller.

        Returns False when nobody is queued, so the caller idles it instead.
        Expired and cancelled futures are discarded on the way past: a waiter
        that has already timed out must not be handed a lease nobody will
        release.
        """
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.done():
                continue
            self._borrowed.add(connection)
            waiter.set_result(connection)
            return True
        return False

    def _wake_one(self) -> None:
        """Tell one queued caller that capacity freed, so it can open."""
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return

    def _settle_drain_now(self) -> None:
        """Stop waiting for leases: the grace period is over."""
        drained = self._drained
        if drained is not None and not drained.done():
            drained.set_result(None)

    def _settle_drain(self) -> None:
        """Resolve `stop`'s waiter once every lease is back."""
        drained = self._drained
        if drained is not None and not drained.done() and not self._borrowed:
            drained.set_result(None)

    async def release(self, connection: Any, *, shared: bool = False) -> None:
        """Return a leased connection, and wake one waiter.

        `shared` must match the acquisition: a shared lease is one of several on
        that connection, and only the last one out returns it to the pool.

        The connection goes back to the idle set and is reused — unless the pool
        is shutting down or the connection reports itself closed, in which case
        it is dropped from the pool and closed here instead. Either way a waiter
        is notified, so a connection that turned out to be dead still frees the
        capacity it was holding.

        Raises:
            InterfaceError: this connection was not leased from this pool.
                Releasing twice is the usual way to see it, and it is refused
                rather than tolerated because a double release would put one
                connection in the idle set twice and hand it to two callers.
        """
        if shared and self._config.pipeline_depth > 1:
            if self.try_release_shared(connection):
                return
            await self._release_shared(connection)
            return
        if connection not in self._borrowed:
            raise InterfaceError("connection was not borrowed from this pool")
        self._borrowed.remove(connection)
        if self._stopping or getattr(connection, "closed", False):
            # Dropped rather than reused. The capacity it was holding is freed,
            # so a queued caller is woken to open a replacement.
            self._connections.discard(connection)
            self._wake_one()
            self._settle_drain()
            if not getattr(connection, "closed", False):
                await connection.close()
            return
        # Hand it straight to the longest-waiting caller if there is one; the
        # connection never touches the idle list in that case, which is one
        # fewer place for a lease to be lost.
        if not self._hand_off(connection):
            self._available.append(connection)
        self._settle_drain()

    async def stop(self, grace_period: float) -> None:
        """Stop accepting acquisitions, drain for `grace_period`, then close everything.

        Draining is best-effort: the pool waits up to `grace_period` seconds for
        leased connections to come back, and then closes what is still out
        anyway. A caller still holding one at that point has it closed
        underneath it — the grace period is the promise, not the outcome.

        Waiting callers are woken immediately rather than left on the queue, and
        `acquire` refuses from the first moment of the stop. Calling this on a
        pool that never started does nothing.

        Args:
            grace_period: Seconds to wait for leases to be returned. Zero closes
                everything at once.
        """
        if not self._started:
            return
        self._stopping = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_period
        # Refuse everybody still queued before waiting: they can never be
        # served now, and holding them would spend the whole grace period on
        # callers that are going to fail anyway.
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_exception(
                    InterfaceError("PostgreSQL pool is shutting down")
                )
        if self._borrowed and grace_period > 0:
            self._drained = loop.create_future()
            timer = loop.call_later(deadline - loop.time(), self._settle_drain_now)
            try:
                await self._drained
            finally:
                timer.cancel()
                self._drained = None
        idle = tuple(self._available)
        borrowed = tuple(self._borrowed)
        self._available.clear()
        self._borrowed.clear()
        self._connections.clear()
        self._started = False
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
    """One named, prepared SQL statement bound to a database and a workload.

    Register with `Database.statement(name, sql, workload=...)` rather than
    constructing directly; that is what makes the pools prepare it on every
    connection they open, so running it never pays a parse.

    A statement carries its `workload`, and that is the whole of its routing:
    each call acquires a connection from *that* pool, runs, and releases it
    before returning. Nothing is held between calls, so two statements never
    share a transaction — for that, use a session.

    The four run methods differ only in what they ask the connection for, and
    each takes the query parameters as positional arguments, bound by position
    (`$1`, `$2`, ...). `map` runs the same statement over many argument sets on
    one connection.

    Attributes:
        database: The `Database` this was registered on.
        name: The registered name, unique within that database.
        sql: The statement text.
        workload: Which pool its connections come from — `"read"`,
            `"security_read"` or `"write"`. The two read workloads open their
            connections with `default_transaction_read_only`.
    """

    __slots__ = ("_pool", "database", "name", "sql", "workload")

    def __init__(self, database: Database, name: str, sql: str, workload: Workload) -> None:
        self.database = database
        self.name = name
        self.sql = sql
        self.workload = workload
        # Bound after Database.start() has built the workload pools. Production
        # statements then reach their fixed pool directly; a Statement created
        # over a database-shaped double keeps None and the compatibility path.
        self._pool: Pool | None = None

    async def _call(self, method: str, args: tuple[object, ...]) -> Any:
        # Shared, not exclusive: a statement is one autocommit round trip and
        # never opens a transaction, so several may share a connection and be
        # batched into one flight. At `pipeline_depth=1` this is exactly
        # `acquire()`.
        #
        # The synchronous attempt first: an uncontended lease is a deque pop and
        # a dict store, and awaiting it through `Database` and `Pool` cost two
        # coroutines to do that. `None` means it has to be awaited after all.
        # Each `getattr` is the same double-compatibility check
        # `Database.acquire_shared` documents -- a database object that
        # implements neither still gets exclusive leases.
        database = self.database
        marker = _phase_marker.get(None)
        pool = self._pool
        if pool is not None and marker is None:
            # Startup fixed this statement's workload to this exact pool. The
            # ordinary request therefore need not rediscover Database methods,
            # validate the workload, resolve its dict entry and dynamically
            # discover the Pool fast paths twice around every query. Armed
            # requests deliberately retain the Database seams below: that is
            # where pool wait and query phases are recorded.
            connection = pool.try_acquire_shared()
            if connection is None:
                connection = await pool.acquire(shared=True)
            try:
                if _NATIVE_STATEMENT_SUBMIT and type(connection) is Connection:
                    # This coroutine is already running, so native submission
                    # can happen now without changing Connection.fetch()'s
                    # lazy call-site contract. Awaiting the Future here removes
                    # the deferred SubmitAwait object and its first-step
                    # iterator. Cancellation remains paired to the Operation,
                    # exactly as it is inside that public awaitable.
                    operation = connection._submit_now(method, self.sql, args)
                    try:
                        return await operation.future
                    except asyncio.CancelledError:
                        connection._cancel_operation(operation)
                        raise
                if method == "fetch_batch" and not hasattr(connection, method):
                    return RecordBatch(await connection.fetch(self.sql, *args))
                return await getattr(connection, method)(self.sql, *args)
            finally:
                if not pool.try_release_shared(connection):
                    await pool.release(connection, shared=True)
        try_acquire_shared = getattr(database, "try_acquire_shared", None)
        connection = (
            try_acquire_shared(self.workload) if try_acquire_shared is not None
            else None
        )
        if connection is None:
            acquire_shared = getattr(database, "acquire_shared", None)
            connection = await (
                acquire_shared(self.workload) if acquire_shared is not None
                else database.acquire(self.workload)
            )
        try:
            if marker is None:
                if method == "fetch_batch" and not hasattr(connection, method):
                    return RecordBatch(await connection.fetch(self.sql, *args))
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
                if method == "fetch_batch" and not hasattr(connection, method):
                    result = RecordBatch(await connection.fetch(self.sql, *args))
                else:
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
            try_release_shared = getattr(database, "try_release_shared", None)
            if try_release_shared is None or not try_release_shared(
                self.workload, connection
            ):
                release_shared = getattr(database, "release_shared", None)
                await (
                    release_shared(self.workload, connection)
                    if release_shared is not None
                    else database.release(self.workload, connection)
                )

    def execute(self, *args: object) -> Awaitable[str]:
        """Run the statement for its effect and return the command tag.

        The tag is PostgreSQL's own summary of what happened — `"INSERT 0 1"`,
        `"UPDATE 3"` — so it is where a row count comes from when there are no
        rows to fetch. Rows the statement does produce are discarded.
        """
        if _NATIVE_STATEMENT_AWAIT:
            return _backend._statement_call(self, "execute", args)
        return self._call("execute", args)

    def fetch(self, *args: object) -> Awaitable[list[Any]]:
        """Run the statement and return every row, as a list of `Record`.

        The whole result is materialized; there is no cursor here. A statement
        that can match an unbounded number of rows should carry its own `LIMIT`.
        """
        if _NATIVE_STATEMENT_AWAIT:
            return _backend._statement_call(self, "fetch", args)
        return self._call("fetch", args)

    def fetch_batch(self, *args: object) -> Awaitable[Any]:
        """Run the statement into a native row collection.

        The batch keeps the familiar sequence and `append` surface while
        exposing `sort_by(column)` so sorting does not allocate a Python key
        callable or dispatch it once per row.  Individual rows remain ordinary
        `Record` values when Python observes them.
        """
        if _NATIVE_STATEMENT_AWAIT:
            return _backend._statement_call(self, "fetch_batch", args)
        return self._call("fetch_batch", args)

    def fetchrow(self, *args: object) -> Awaitable[Any]:
        """Run the statement and return its first row, or `None` for no rows.

        `None` means the query matched nothing. It is not an error, and a
        statement whose first column can itself be null is better read with
        this than with `fetchval`, which cannot tell the two apart.
        """
        if _NATIVE_STATEMENT_AWAIT:
            return _backend._statement_call(self, "fetchrow", args)
        return self._call("fetchrow", args)

    def fetchval(self, *args: object) -> Awaitable[Any]:
        """Run the statement and return the first column of the first row.

        `None` for no rows *and* for a first column that is null — the two are
        indistinguishable here, which is what `fetchrow` is for.
        """
        if _NATIVE_STATEMENT_AWAIT:
            return _backend._statement_call(self, "fetchval", args)
        return self._call("fetchval", args)

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
        """The logical name this database was registered under.

        The handle an application knows it by — the `name` given to
        `Wreath.postgres`, which is also what `Wreath.orm(database=...)` and
        the other database-taking helpers name — rather than anything
        PostgreSQL itself knows. Required, non-empty, and fixed at construction.
        """
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
            statement._pool = self._pools.get(workload)
            self._statements[name] = statement
        return statement

    def _for_workload(self, workload: Workload) -> tuple[Statement, ...]:
        return tuple(item for item in self._statements.values() if item.workload == workload)

    async def start(self) -> None:
        """Build a `Pool` per configured workload and start them all.

        Each pool gets the workload's own DSN when one was supplied and the
        database's otherwise, opens read-only for `read` and `security_read`,
        and prepares that workload's registered statements on every connection.

        All-or-nothing across pools as well as within them: if one fails to
        start, the pools already up are stopped, the set is cleared, and the
        exception propagates with `started` still false. Calling it twice does
        nothing the second time.

        A workload first named by `statement()` once this call has begun is not
        started here, and `pool()` raises `KeyError` for it.
        """
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
        for statement in self._statements.values():
            statement._pool = self._pools[statement.workload]
        self.started = True

    async def stop(self) -> None:
        """Stop every pool, in reverse start order, within one shared deadline.

        `shutdown_timeout` is the budget for the *whole* shutdown, not for each
        pool: whatever an earlier pool spends draining is taken off what the
        next one gets, and a pool reached after the deadline has passed is
        stopped with no grace at all. Calling it on a database that never
        started does nothing.
        """
        if not self.started:
            return
        deadline = asyncio.get_running_loop().time() + self.shutdown_timeout
        for pool in reversed(tuple(self._pools.values())):
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await pool.stop(remaining)
        self.started = False

    def counters(self) -> Any:
        """This database's pools, summed, for `wreath.metrics.collect`.

        One reading per database rather than per workload pool: the instance
        label is the database, and a caller who wants the split reads
        `pool(workload).snapshot()`. Summing is right for the four fields that
        are counts of connections; `queue_high_water` is a maximum, so it is
        taken as one rather than added.

        Only pools that exist are read. Asking for one that has not been built
        would *create* it, and a metrics scrape must not open connections.
        """
        from .metrics import Counters

        totals = {"borrowed": 0, "available": 0, "waiters": 0, "max_size": 0}
        high_water = 0
        for pool in self._pools.values():
            reading = pool.snapshot()
            totals["borrowed"] += reading.borrowed
            totals["available"] += reading.available
            totals["waiters"] += reading.waiters
            totals["max_size"] += reading.max_size
            high_water = max(high_water, reading.queue_high_water)
        return Counters(
            subsystem="pool",
            instance=self._name,
            values={**totals, "queue_high_water": high_water},
        )

    def pool(self, workload: Workload) -> Pool:
        """The pool serving `workload`.

        Available only once the database has started. Before that, the call
        exists to answer one question — *is this workload configured at all?* —
        and answers it by raising one of two different errors, which are worth
        telling apart.

        Raises:
            KeyError: this workload is not configured on this database, whether
                or not it has started.
            InterfaceError: the workload is configured, but the database has not
                started, so there is no pool to hand back yet. Inspecting a pool
                before startup is deliberately unsupported.
        """
        workload = _workload(workload)
        try:
            return self._pools[workload] if self.started else self._configured_pool(workload)
        except KeyError:
            raise KeyError(f"PostgreSQL workload is not configured: {workload}") from None

    def _resolve_pool(self, workload: Workload) -> Pool:
        """The started pool for `workload`, without re-validating the name.

        `pool()` runs `_workload()` on its argument and is the right shape for a
        public accessor. It is the wrong shape for `acquire`/`release`, which
        run it twice per query on a string a `Statement` already validated when
        it was registered -- two interpreter frames per request buying a check
        that can only fail on a programming error.

        A miss falls through to `pool()`, so an unconfigured or misspelled
        workload raises exactly what it raised before -- `ValueError` for a
        string that is not a workload, `KeyError` for one that is not
        configured, `InterfaceError` before startup. The fast path is a single
        dict lookup and no frame beyond this one.
        """
        pool = self._pools.get(workload) if self.started else None
        return pool if pool is not None else self.pool(workload)

    def _configured_pool(self, workload: Workload) -> Pool:
        if workload not in self._configs:
            raise KeyError(workload)
        # Pool inspection before startup is intentionally unsupported except for
        # validating that a workload exists.
        raise InterfaceError("PostgreSQL pool has not started")

    async def acquire(self, workload: Workload = "read") -> Any:
        """Lease a connection from `workload`'s pool. Pair it with `release`.

        The one acquisition seam in this module: `Statement`, `Statement.map`
        and direct callers all come through here, which is why the Flight
        Recorder's pool-wait phase can be attributed from this single point.

        Raises whatever `Pool.acquire` raises, plus `KeyError` when the workload
        is not configured.
        """
        # Armed-request pool-wait phase; every other request pays exactly the
        # ContextVar read. This is the one acquisition seam, so Statement,
        # Statement.map, and direct acquire callers are all covered.
        marker = _phase_marker.get(None)
        pool = self._resolve_pool(workload)
        if marker is None:
            return await pool.acquire()
        start = _monotonic_ns()
        connection = await pool.acquire()
        marker(_PH_DB_POOL_WAIT, self._flight_dep_id, _COV_PYTHON,
               _monotonic_ns() - start)
        return connection

    async def acquire_shared(self, workload: Workload = "read") -> Any:
        """Lease a connection concurrent statements may share. See `PoolConfig`.

        Additive and internal on purpose. Folding `shared=` into `acquire()`
        instead looked tidier -- one seam, one keyword -- and broke twenty-four
        tests across jobs, passes and workflow recording, because every
        `Database`-shaped double in the tree is written against `acquire()`'s
        current signature. A new method they do not have is invisible to them;
        a new keyword on one they do have is not.

        `Statement` reaches this through `_acquire_for_statement`, which falls
        back to `acquire()` for any database object that does not implement it
        -- so a double keeps working and simply gets exclusive leases, which is
        the conservative answer rather than a failure.
        """
        marker = _phase_marker.get(None)
        pool = self._resolve_pool(workload)
        if marker is None:
            return await pool.acquire(shared=True)
        start = _monotonic_ns()
        connection = await pool.acquire(shared=True)
        marker(_PH_DB_POOL_WAIT, self._flight_dep_id, _COV_PYTHON,
               _monotonic_ns() - start)
        return connection

    def try_acquire_shared(self, workload: Workload = "read") -> Any | None:
        """The uncontended shared lease, taken without a coroutine.

        `None` means it has to be awaited: nothing idle, or the pool is
        shutting down -- and deliberately also whenever a Flight Recorder phase
        marker is armed, because `acquire_shared` is the one seam pool waiting
        is attributed from and an armed request must keep going through it.

        See `Pool.try_acquire_shared` for why the synchronous path exists.
        """
        if _phase_marker.get(None) is not None:
            return None
        # A pool-shaped double predates these methods, and the rule is the same
        # one `acquire_shared` states: a method a double does not have has to be
        # invisible to it. Missing means "await it", which is what every such
        # double already does.
        attempt = getattr(self._resolve_pool(workload), "try_acquire_shared", None)
        return attempt() if attempt is not None else None

    def try_release_shared(self, workload: Workload, connection: Any) -> bool:
        """Return a shared lease without a coroutine; False if it has to await."""
        attempt = getattr(self._resolve_pool(workload), "try_release_shared", None)
        return attempt(connection) if attempt is not None else False

    async def release_shared(self, workload: Workload, connection: Any) -> None:
        """Return a shared lease to the pool it came from."""
        await self._resolve_pool(workload).release(connection, shared=True)

    async def release(self, workload: Workload, connection: Any) -> None:
        """Return a connection to the pool it was leased from.

        `workload` must be the one it was acquired with — the connection is
        handed to that pool, and a pool refuses a connection it did not lease
        with `InterfaceError`.
        """
        await self._resolve_pool(workload).release(connection)

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


if _NATIVE_STATEMENT_AWAIT:
    _backend._statement_configure(Statement, Pool, PoolConfig, _phase_marker)


__all__ = [
    "MAX_SPARSEVEC_DIM", "MAX_SPARSEVEC_NNZ",
    "AdvisoryLock", "AdvisoryTryLock", "Connection", "Database", "FromDatabase",
    "InterfaceError", "OperationalError", "PipelineFullError", "Pool", "PoolConfig",
    "PoolSnapshot", "PostgresError", "ProtocolError", "Record", "RecordBatch",
    "SingletonRunner",
    "SparseVector", "Statement",
    "Workload", "connect",
]
