"""Request-scoped boundary adapters and their fault injection (Stage 7).

Endpoint-plan replay can run a *real* handler (INVOKE) that reaches out to the
PostgreSQL driver or an outbound HTTP client. To replay that deterministically —
and, more valuably, to *red-team the owned handling of a boundary failure* — we
substitute injected doubles for those boundaries. A double either returns a
scripted result or raises a modeled fault (`pool-acquire timeout`, ``server
error after the round trip`, `connection drop mid-result``, ...), and the
framework's owned recovery — error mapping, connection release, transaction
outcome — runs for real.

Adapters are explicit and request-scoped. They never touch a real socket, and
they are installed only for the duration of a replay:

- **PostgreSQL** doubles replace `app._databases[name]`. Because the binder
  looks its database up by name at request time, installing a double plus forcing
  a route recompile routes every `FromDatabase` connection to it. The double
  counts acquisitions and releases, so a test can assert the framework returned
  the connection to the pool even on the error path.
- **Outbound HTTP** doubles subclass the real `HTTPClient` and override only
  the transport seam (`_request_timed`), so the client's own timeout, phase,
  and error handling runs while the modeled fault is injected beneath it.
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .http_client import ClientResponse, HTTPClient
from .postgres import InterfaceError, OperationalError, PostgresError

__all__ = [
    "AdapterFault",
    "DatabaseDouble",
    "FaultyHttpClient",
    "ObjectStoreDouble",
    "ReplayAdapters",
    "SILENT_FAULTS",
    "installed_adapters",
]


class AdapterFault(StrEnum):
    """A modeled failure at an owned boundary seam, keyed to an owned coordinate.

    Database seam faults are keyed to acquisition or to the Nth query on a leased
    connection; HTTP seam faults are keyed to the Nth outbound request; listen,
    transaction and object-store faults are keyed to the Nth operation of their
    kind. Every coordinate is a count the owned code itself produces, so a
    schedule stays bit-for-bit reproducible.
    """

    # Database seam
    POOL_TIMEOUT = "pool_timeout"  # acquire times out (owned: TimeoutError)
    POOL_EXHAUSTED = "pool_exhausted"  # acquire refused (owned: InterfaceError)
    SERVER_ERROR = "server_error"  # statement errors after the round trip begins
    CONNECTION_DROP = "connection_drop"  # connection lost mid-result
    LOST_COMMIT = "lost_commit"  # ambiguous completion after a write
    RELEASE_ERROR = "release_error"  # returning the connection to the pool fails
    # HTTP client seam
    CONNECT_ERROR = "connect_error"  # DNS/connect/TLS failure
    READ_TIMEOUT = "read_timeout"  # response read times out
    # LISTEN/NOTIFY doorbell seam
    #
    # STREAM_END and STREAM_ERROR are deliberately distinct. `Connection.
    # notifications()` *returns* when the connection closes rather than raising
    # (`_pure/postgres.py:985`), so a supervisor written around `except` sees
    # nothing at all -- which is exactly how the bus doorbell died silently for
    # the life of a process. A corpus that only models the raising case would
    # re-bless that bug.
    LISTEN_REFUSED = "listen_refused"  # LISTEN fails (a database down at boot)
    NOTIFY_STREAM_END = "notify_stream_end"  # the iterator returns, no exception
    NOTIFY_STREAM_ERROR = "notify_stream_error"  # the iterator raises mid-stream
    # Transaction seam
    BEGIN_ERROR = "begin_error"  # the transaction never opens
    COMMIT_ERROR = "commit_error"  # work applied, commit outcome unknown
    STATEMENT_TIMEOUT = "statement_timeout"  # a statement inside the scope times out
    # Claim seam: the statement succeeds and returns *no row*. Not an error --
    # the shape every `INSERT ... ON CONFLICT ... RETURNING` claim degrades to
    # when the row it expected has been purged underneath it.
    CLAIM_LOST = "claim_lost"
    # Connection seam: the connection itself is gone, not one statement on it.
    # Distinct from CONNECTION_DROP, which fails the Nth statement and leaves
    # the connection usable: this *latches*, so every later operation on the
    # same lease raises the identical error object. A caller may retry a
    # dropped statement on the connection it already holds; it must take a new
    # connection after this one. Code that cannot tell them apart retries into
    # the same failure until its attempt budget runs out.
    CONNECTION_FAILED = "connection_failed"
    # Decode seam: the statement succeeded on the wire and the *result* could
    # not be read back. Deliberately not a `PostgresError` -- the live defect
    # was `ValueError: text-format array decoding is not supported`, raised by
    # the driver on a cold catalog path, and every `except PostgresError` in
    # this tree steps straight around it. Modelling it as a server error would
    # prove the wrong thing.
    DECODE_ERROR = "decode_error"
    # Inference seam: the statement works once and fails forever after.
    # PostgreSQL infers a parameter's type on first execution and the prepared
    # statement carries the inference, so the second execution binds by an OID
    # nothing can encode. Nothing else in the corpus models a failure that only
    # appears on the *second* call, and a smoke test that runs each statement
    # once cannot see it at all.
    PREPARED_POISON = "prepared_poison"
    # Object store seam
    OBJECT_UNREACHABLE = "object_unreachable"  # the store cannot be reached
    OBJECT_WRITE_TORN = "object_write_torn"  # a write fails part-way through
    OBJECT_READ_SHORT = "object_read_short"  # a read returns fewer bytes than stat


#: Faults whose modeled outcome is "the call succeeds and yields nothing".
#: They are not errors, and treating them as errors is the mistake this set
#: exists to prevent.
SILENT_FAULTS = frozenset(
    {AdapterFault.CLAIM_LOST, AdapterFault.NOTIFY_STREAM_END}
)


def _db_error(fault: AdapterFault) -> Exception:
    if fault is AdapterFault.DECODE_ERROR:
        # Not a PostgresError, on purpose. See the enum member's comment: the
        # statement succeeded and the driver could not read the answer, which is
        # a different class of failure from anything the server reported.
        return ValueError("text-format array decoding is not supported")
    if fault is AdapterFault.CONNECTION_FAILED:
        return InterfaceError("PostgreSQL connection failed; every operation on it")
    if fault is AdapterFault.PREPARED_POISON:
        return TypeError("no binary encoder for PostgreSQL OID (inferred parameter type)")
    if fault is AdapterFault.POOL_TIMEOUT:
        return TimeoutError("timed out acquiring PostgreSQL connection")
    if fault is AdapterFault.POOL_EXHAUSTED:
        return InterfaceError("PostgreSQL pool queue is full")
    if fault is AdapterFault.CONNECTION_DROP:
        return OperationalError("connection dropped mid-result")
    if fault is AdapterFault.LOST_COMMIT:
        return OperationalError("commit acknowledgement lost (ambiguous completion)")
    if fault is AdapterFault.RELEASE_ERROR:
        return InterfaceError("returning the connection to the pool failed")
    if fault is AdapterFault.LISTEN_REFUSED:
        return OperationalError("LISTEN refused: no connection to the server")
    if fault is AdapterFault.NOTIFY_STREAM_ERROR:
        return OperationalError("notification stream lost")
    if fault is AdapterFault.BEGIN_ERROR:
        return OperationalError("could not open a transaction")
    if fault is AdapterFault.COMMIT_ERROR:
        return OperationalError("commit failed after the work was applied")
    if fault is AdapterFault.STATEMENT_TIMEOUT:
        return PostgresError("canceling statement due to statement timeout")
    return PostgresError("server error after the round trip began")


def _object_error(fault: AdapterFault, key: str) -> Exception:
    from .objects import ObjectError

    if fault is AdapterFault.OBJECT_UNREACHABLE:
        return ObjectError(f"object store unreachable while addressing {key!r}")
    return ObjectError(f"write of {key!r} failed part-way through")


# --- what a real connection refuses, before any fault is considered ----------
#
# A double that accepts SQL the driver rejects is not a faithful boundary, and
# three defects shipped this session because of exactly that. Each rule below
# was measured against PostgreSQL 17.10 and is pinned by
# `tests/postgres/test_double_fidelity.py`, which runs the same assertions
# against a real connection so the two cannot drift apart.

#: What `_infer_oid` can encode. A `list` is absent, which is why `= ANY($1)`
#: raises before PostgreSQL is ever reached.
_BINDABLE = (bool, int, float, str, bytes, uuid.UUID, decimal.Decimal,
             datetime.datetime, datetime.date)

#: A cast on a placeholder declares the *parameter* type, and only bites on the
#: second execution of the same SQL text — the first is coerced, the second
#: binds by the inferred OID. These have no binary encoder at all, so no Python
#: value rescues them; `$1::regclass` was the one that shipped.
_UNENCODABLE_CASTS = frozenset({
    "regclass", "regtype", "regproc", "oid", "name", "inet", "xml",
})

#: These have an encoder that demands a particular Python type. `$1::uuid` with
#: a string is the same trap wearing a friendlier name.
_CAST_REQUIRES: dict[str, tuple[type, ...]] = {
    "uuid": (uuid.UUID,),
    "numeric": (decimal.Decimal, int),
    "timestamptz": (datetime.datetime,),
    "timestamp": (datetime.datetime,),
    "date": (datetime.date,),
}

_PLACEHOLDER_CAST = re.compile(r"\$(\d+)::(\w+)")
_STRING_OR_DOLLAR = re.compile(r"'(?:[^']|'')*'|\$\$.*?\$\$", re.DOTALL)


def refuse_unbindable(args: tuple[Any, ...]) -> None:
    """Raise what the driver raises for a value it cannot encode."""
    for value in args:
        if value is None or isinstance(value, _BINDABLE):
            continue
        raise TypeError(f"unsupported PostgreSQL value type: {type(value).__name__}")


def refuse_multiple_commands(sql: str) -> None:
    """The extended query protocol takes one command per statement."""
    stripped = _STRING_OR_DOLLAR.sub("", sql).strip().rstrip(";")
    if ";" in stripped:
        raise PostgresError("cannot insert multiple commands into a prepared statement")


def refuse_uninferable_cast(sql: str, args: tuple[Any, ...], seen: set[str]) -> None:
    """The trap that survives one call. `seen` is the prepared-statement cache."""
    text = " ".join(sql.split())
    first_time = text not in seen
    seen.add(text)
    if first_time:
        return
    for index, cast in _PLACEHOLDER_CAST.findall(text):
        position = int(index) - 1
        if position >= len(args):
            continue
        lowered = cast.lower()
        if lowered in _UNENCODABLE_CASTS:
            raise TypeError(f"no binary encoder for PostgreSQL OID (cast to {lowered})")
        required = _CAST_REQUIRES.get(lowered)
        if required is not None and not isinstance(args[position], required):
            names = " or ".join(t.__name__ for t in required)
            raise TypeError(f"{lowered} codec requires {names}")


def refuse_what_postgres_refuses(
    sql: object, args: tuple[Any, ...], seen: set[str]
) -> None:
    """Every check a real connection makes before it runs anything."""
    if not isinstance(sql, str):
        return
    refuse_multiple_commands(sql)
    refuse_unbindable(args)
    refuse_uninferable_cast(sql, args, seen)


class _ConnectionDouble:
    """A leased-connection double: scripted results, or a query-keyed fault.

    Query methods advance a per-connection counter so a fault descriptor can name
    "the Nth query on this connection" — a stable owned coordinate.

    It also refuses what a real connection refuses, *before* considering a
    fault: a double that accepts an unbindable parameter is not modelling a
    boundary, it is hiding one.
    """

    __slots__ = ("_double", "_query", "_txn", "_prepared", "_failure")

    def __init__(self, double: DatabaseDouble) -> None:
        self._double = double
        self._query = 0
        self._txn = 0
        self._prepared: set[str] = set()
        #: Set once a `CONNECTION_FAILED` fault fires. It latches: the lease is
        #: over, and every later operation on it raises the *same* error object,
        #: which is what makes "retry on this connection" visibly wrong.
        self._failure: Exception | None = None

    @property
    def failed(self) -> bool:
        """Whether this lease has been failed by a connection-level fault."""
        return self._failure is not None

    def _next(self, default: Any, sql: object = None) -> Any:
        if self._failure is not None:
            raise self._failure
        index = self._query
        self._query += 1
        double = self._double
        text = " ".join(sql.split()) if isinstance(sql, str) else None
        if text is not None and text in double.poisoned:
            # Second and every subsequent execution of a statement whose
            # parameter type was inferred once and cannot be encoded again.
            raise _db_error(AdapterFault.PREPARED_POISON)
        connection_fault = double.connection_faults.get(index)
        if connection_fault is not None:
            self._failure = _db_error(connection_fault)
            raise self._failure
        fault = double.query_faults.get(index)
        if fault is AdapterFault.CLAIM_LOST:
            # The statement ran. It simply returned no row -- which for a
            # `RETURNING` claim means somebody else holds it, or it is gone.
            # Returning the empty shape rather than raising is the whole point.
            return None if default is None else type(default)()
        if fault is AdapterFault.PREPARED_POISON:
            # *This* execution succeeds. The inference it left behind is what
            # fails, so the poison is recorded on the double rather than on this
            # lease: a caller that reconnects and runs the same SQL still gets
            # it, which is precisely why "it worked when I tried it" holds.
            if text is not None:
                double.poisoned.add(text)
            fault = None
        if fault is not None:
            raise _db_error(fault)
        results = double.results
        return results[index] if index < len(results) else default

    # --- LISTEN/NOTIFY doorbell seam -----------------------------------------

    async def listen(self, channel: str) -> None:
        # The coordinate counts on the *double*, not this connection: a doorbell
        # fault is addressed to "the Nth LISTEN this bus attempts", and the
        # attempts that matter are the ones after a reconnect. Counting per
        # connection would reset on every reopen, so a fault keyed past the
        # first could never fire -- which silently turns a compound schedule
        # into its first fault alone.
        index = self._double.listens
        self._double.listens += 1
        self._double.listened.append(channel)
        if self._double.listen_faults.get(index) is AdapterFault.LISTEN_REFUSED:
            raise _db_error(AdapterFault.LISTEN_REFUSED)

    async def unlisten(self, channel: str) -> None:
        return None

    async def notifications(self) -> Any:
        """The doorbell's stream, and the two ways it can stop.

        `NOTIFY_STREAM_END` returns without raising, mirroring the real
        connection closing; `NOTIFY_STREAM_ERROR` raises. A supervisor that
        only handles the second is the bug this seam exists to catch.

        With **no** fault the stream *stays open*, which is what a real one does
        when there is simply nothing to deliver. It used to return, so an
        un-faulted double made the doorbell churn exactly as hard as
        `NOTIFY_STREAM_END` did -- and a region whose behaviour is
        indistinguishable from the control is a region that proves nothing. Held
        open, the reconnect counter is a signal again.
        """
        self._double.streams += 1
        fault = self._double.stream_fault
        for notification in self._double.notifications:
            yield notification
        if fault is AdapterFault.NOTIFY_STREAM_ERROR:
            raise _db_error(AdapterFault.NOTIFY_STREAM_ERROR)
        if fault is None and self._double.hold_stream:
            # Park until cancelled. Not a sleep loop: a supervised doorbell is
            # meant to sit here for the process's lifetime, and anything that
            # wakes on a timer would model a connection that keeps dropping.
            await asyncio.Event().wait()
        return  # NOTIFY_STREAM_END lands here

    # --- transaction seam -----------------------------------------------------

    def transaction(self) -> _TransactionDouble:
        index = self._txn
        self._txn += 1
        return _TransactionDouble(self, self._double.transaction_faults.get(index))

    async def execute(self, sql: object, *args: object) -> str:
        refuse_what_postgres_refuses(sql, args, self._prepared)
        return self._next("OK", sql)

    async def fetch(self, sql: object, *args: object) -> list[Any]:
        refuse_what_postgres_refuses(sql, args, self._prepared)
        return self._next([], sql)

    async def fetchrow(self, sql: object, *args: object) -> Any:
        refuse_what_postgres_refuses(sql, args, self._prepared)
        return self._next(None, sql)

    async def fetchval(self, sql: object, *args: object) -> Any:
        refuse_what_postgres_refuses(sql, args, self._prepared)
        return self._next(None, sql)

    async def map(self, method: str, sql: object, argument_sets: Any, *, max_in_flight: int = 32):
        return self._next([], sql)

    async def close(self) -> None:
        return None


class _TransactionDouble:
    """One `async with connection.transaction()` scope.

    The three faults sit at genuinely different moments, and the difference is
    what a caller's recovery has to distinguish: `BEGIN_ERROR` means no work
    ran, `STATEMENT_TIMEOUT` means the scope died mid-body and rolls back
    cleanly, and `COMMIT_ERROR` means the work may or may not be durable --
    the only one of the three where retrying is not obviously safe.
    """

    __slots__ = ("_connection", "_fault", "committed", "rolled_back")

    def __init__(self, connection: _ConnectionDouble, fault: AdapterFault | None) -> None:
        self._connection = connection
        self._fault = fault
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> _TransactionDouble:
        if self._fault is AdapterFault.BEGIN_ERROR:
            raise _db_error(AdapterFault.BEGIN_ERROR)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            self.rolled_back = True
            return False
        if self._fault is AdapterFault.COMMIT_ERROR:
            raise _db_error(AdapterFault.COMMIT_ERROR)
        self.committed = True
        return False

    async def execute(self, sql: object, *args: object) -> str:
        self._maybe_timeout()
        return self._connection._next("OK", sql)

    async def fetch(self, sql: object, *args: object) -> list[Any]:
        self._maybe_timeout()
        return self._connection._next([], sql)

    async def fetchrow(self, sql: object, *args: object) -> Any:
        self._maybe_timeout()
        return self._connection._next(None, sql)

    async def fetchval(self, sql: object, *args: object) -> Any:
        self._maybe_timeout()
        return self._connection._next(None, sql)

    def _maybe_timeout(self) -> None:
        if self._fault is AdapterFault.STATEMENT_TIMEOUT:
            raise _db_error(AdapterFault.STATEMENT_TIMEOUT)


class ObjectStoreDouble:
    """An `ObjectStore` double that injects modeled storage failures.

    Only the four methods a fault can meaningfully perturb are overridden;
    everything else delegates to a real `MemoryObjectStore`, so a handler
    exercising the store's ordinary behaviour runs against real code and only
    the faulted operation is synthetic.

    `OBJECT_READ_SHORT` is the interesting one: it returns *fewer bytes than
    `stat` reported*, without raising. A caller that trusts the length it was
    given, rather than what it received, silently processes a truncated object.
    """

    __slots__ = ("name", "_inner", "op_faults", "_ops", "reads", "writes")

    def __init__(
        self,
        name: str = "objects",
        *,
        op_faults: dict[int, AdapterFault] | None = None,
    ) -> None:
        from .objects import MemoryObjectStore

        self.name = name
        self._inner = MemoryObjectStore()
        self.op_faults = op_faults or {}
        self._ops = 0
        self.reads = 0
        self.writes = 0

    def _next_fault(self) -> AdapterFault | None:
        index = self._ops
        self._ops += 1
        return self.op_faults.get(index)

    async def read(self, key: str) -> bytes:
        self.reads += 1
        fault = self._next_fault()
        if fault is AdapterFault.OBJECT_UNREACHABLE:
            raise _object_error(fault, key)
        data = await self._inner.read(key)
        if fault is AdapterFault.OBJECT_READ_SHORT:
            return data[: len(data) // 2]
        return data

    async def write(self, key: str, data: bytes, *, content_type: str | None = None) -> Any:
        self.writes += 1
        fault = self._next_fault()
        if fault in (AdapterFault.OBJECT_UNREACHABLE, AdapterFault.OBJECT_WRITE_TORN):
            if fault is AdapterFault.OBJECT_WRITE_TORN:
                # A torn write leaves a *partial* object behind, which is worse
                # than none: `exists()` says yes and the bytes are wrong.
                await self._inner.write(key, data[: len(data) // 2], content_type=content_type)
            raise _object_error(fault, key)
        return await self._inner.write(key, data, content_type=content_type)

    async def stat(self, key: str) -> Any:
        fault = self._next_fault()
        if fault is AdapterFault.OBJECT_UNREACHABLE:
            raise _object_error(fault, key)
        return await self._inner.stat(key)

    async def delete(self, key: str) -> None:
        fault = self._next_fault()
        if fault is AdapterFault.OBJECT_UNREACHABLE:
            raise _object_error(fault, key)
        return await self._inner.delete(key)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class DatabaseDouble:
    """A `Database` double that scripts results and injects boundary faults.

    `acquired`/`released` count the owned pool lifecycle so a test can prove
    the framework returned the connection even when a query raised.
    """

    __slots__ = (
        "name", "results", "query_faults", "acquire_fault", "release_fault",
        "acquired", "released", "_flight_dep_id",
        "listen_faults", "stream_fault", "transaction_faults",
        "notifications", "listened", "streams", "listens",
        "connection_faults", "poisoned", "_statements", "hold_stream",
    )

    def __init__(
        self,
        name: str = "main",
        *,
        results: tuple[Any, ...] = (),
        query_faults: dict[int, AdapterFault] | None = None,
        acquire_fault: AdapterFault | None = None,
        release_fault: AdapterFault | None = None,
        listen_faults: dict[int, AdapterFault] | None = None,
        stream_fault: AdapterFault | None = None,
        transaction_faults: dict[int, AdapterFault] | None = None,
        connection_faults: dict[int, AdapterFault] | None = None,
        notifications: tuple[Any, ...] = (),
        hold_stream: bool = True,
    ) -> None:
        self.name = name
        self.results = results
        self.query_faults = query_faults or {}
        self.acquire_fault = acquire_fault
        self.release_fault = release_fault
        self.acquired = 0
        self.released = 0
        self._flight_dep_id = 0
        self.listen_faults = listen_faults or {}
        self.stream_fault = stream_fault
        self.transaction_faults = transaction_faults or {}
        #: Faults that end the *lease* rather than one statement, keyed to the
        #: Nth operation on it. See `AdapterFault.CONNECTION_FAILED`.
        self.connection_faults = connection_faults or {}
        #: SQL texts whose parameter inference has been poisoned. Held on the
        #: double, not on a lease, because that is where the real hazard lives:
        #: reconnecting does not un-poison a statement.
        self.poisoned: set[str] = set()
        self._statements: dict[str, Any] = {}
        #: Whether an un-faulted notification stream stays open rather than
        #: returning. See `_ConnectionDouble.notifications`.
        self.hold_stream = hold_stream
        #: Notifications the doorbell stream yields before it ends.
        self.notifications = notifications
        #: Channels a caller asked to LISTEN on, in order -- a reconnect is
        #: only real if it re-subscribes, so the count is the assertion.
        self.listened: list[str] = []
        #: How many times `notifications()` was entered. A supervised doorbell
        #: that reconnects enters it more than once; one that died does not.
        self.streams = 0
        #: LISTEN attempts across every connection this double has handed out --
        #: the coordinate a listen fault is keyed to.
        self.listens = 0

    def statement(self, name: str, sql: str, *, workload: str = "read") -> Any:
        """Register a prepared statement, exactly as `Database` does.

        Present so the stores built on `wreath.store.PostgresStore` --
        the idempotency replay table, the session store, the cache -- replay
        against a double at all. They reach the database only through a
        `Statement`, which leases and releases a connection per call, so
        without this seam their claim/read/purge paths were the one family of
        owned PostgreSQL code a fault schedule could not touch.

        The real `wreath.postgres.Statement` is used rather than a
        double of it: it is the code that acquires, calls, and releases, and
        replacing it would replace the behaviour under test with a copy of it.
        """
        from .postgres import Statement
        from .postgres import _workload as check_workload

        # The same three refusals the real `Database.statement` makes, in the
        # same order. A double that accepts a registration the driver rejects is
        # not modelling the boundary, it is hiding it -- which is exactly how
        # thirteen introspection tests passed against a fake scripted with rows
        # no PostgreSQL would ever send.
        if not name or not sql.strip():
            raise ValueError("statement name and SQL are required")
        check_workload(workload)
        if name in self._statements:
            raise ValueError(f"duplicate PostgreSQL statement: {name}")
        # Structural, not nominal: `Statement` only ever calls `acquire`,
        # `release`, and reads `_flight_dep_id`, all of which this double has.
        # The annotation says `Database` because that is the only production
        # caller, and widening it to a protocol for a test double's sake would
        # put replay's needs into the driver's public types.
        statement = Statement(
            self,  # ty: ignore[invalid-argument-type]
            name,
            sql,
            workload,  # ty: ignore[invalid-argument-type]
        )
        self._statements[name] = statement
        return statement

    async def acquire(self, workload: str = "read") -> _ConnectionDouble:
        self.acquired += 1
        if self.acquire_fault is not None:
            raise _db_error(self.acquire_fault)
        return _ConnectionDouble(self)

    async def release(self, workload: str, connection: Any) -> None:
        self.released += 1
        if self.release_fault is not None:
            raise _db_error(self.release_fault)

    @property
    def leaked(self) -> bool:
        """Whether an acquisition was never returned to the pool."""
        return self.acquired != self.released


class FaultyHttpClient(HTTPClient):
    """An `HTTPClient` whose transport seam injects modeled faults.

    Overriding only `_request_timed` keeps the client's owned timeout, phase,
    and error handling on the real code path; the fault (or scripted response) is
    delivered where the socket would be. Faults are keyed to the Nth request.
    """

    def __init__(
        self,
        name: str = "api",
        *,
        base_url: str = "http://replay.invalid",
        responses: tuple[ClientResponse, ...] = (),
        request_faults: dict[int, AdapterFault] | None = None,
    ) -> None:
        super().__init__(name, base_url=base_url)
        self._replay_responses = responses
        self._replay_faults = request_faults or {}
        self._replay_index = 0

    async def _request_timed(self, method, target, *, headers, body, idempotency_key):
        index = self._replay_index
        self._replay_index += 1
        fault = self._replay_faults.get(index)
        if fault is AdapterFault.CONNECT_ERROR:
            raise ConnectionError("connect failed")
        if fault is AdapterFault.READ_TIMEOUT:
            raise TimeoutError("response read timed out")
        if index < len(self._replay_responses):
            return self._replay_responses[index]
        return ClientResponse(status=200, headers=(), body=b"", http_version="1.1")


@dataclass(frozen=True, slots=True)
class ReplayAdapters:
    """A bundle of request-scoped boundary doubles installed for one replay."""

    databases: dict[str, DatabaseDouble] = field(default_factory=dict)
    clients: dict[str, FaultyHttpClient] = field(default_factory=dict)
    object_stores: dict[str, ObjectStoreDouble] = field(default_factory=dict)

    @classmethod
    def from_faults(cls, adapter_faults: Any) -> ReplayAdapters:
        """Build adapter doubles from a fault schedule's serialized adapter faults
        (`AdapterFaultDescriptor` records). Each named target becomes one double
        carrying its acquire/query/release/listen/transaction, request, or object
        faults, so a checksummed schedule fully reconstructs the boundary
        perturbations for a replay."""
        from .replay import AdapterSeam  # local import: replay imports this module

        db_query: dict[str, dict[int, AdapterFault]] = {}
        db_acquire: dict[str, AdapterFault] = {}
        db_release: dict[str, AdapterFault] = {}
        db_listen: dict[str, dict[int, AdapterFault]] = {}
        db_stream: dict[str, AdapterFault] = {}
        db_txn: dict[str, dict[int, AdapterFault]] = {}
        db_connection: dict[str, dict[int, AdapterFault]] = {}
        http_request: dict[str, dict[int, AdapterFault]] = {}
        object_op: dict[str, dict[int, AdapterFault]] = {}
        for fault in adapter_faults:
            kind = AdapterFault(fault.kind)
            if fault.seam == int(AdapterSeam.DB_ACQUIRE):
                db_acquire[fault.target] = kind
            elif fault.seam == int(AdapterSeam.DB_RELEASE):
                db_release[fault.target] = kind
            elif fault.seam == int(AdapterSeam.DB_QUERY):
                db_query.setdefault(fault.target, {})[fault.coordinate] = kind
            elif fault.seam == int(AdapterSeam.DB_LISTEN):
                # A stream outcome is a property of the held connection, not of
                # the Nth LISTEN, so it lands on the double rather than a map.
                if kind is AdapterFault.LISTEN_REFUSED:
                    db_listen.setdefault(fault.target, {})[fault.coordinate] = kind
                else:
                    db_stream[fault.target] = kind
            elif fault.seam == int(AdapterSeam.DB_TRANSACTION):
                db_txn.setdefault(fault.target, {})[fault.coordinate] = kind
            elif fault.seam == int(AdapterSeam.DB_CONNECTION):
                db_connection.setdefault(fault.target, {})[fault.coordinate] = kind
            elif fault.seam == int(AdapterSeam.HTTP_REQUEST):
                http_request.setdefault(fault.target, {})[fault.coordinate] = kind
            elif fault.seam == int(AdapterSeam.OBJECT_STORE):
                object_op.setdefault(fault.target, {})[fault.coordinate] = kind
        databases: dict[str, DatabaseDouble] = {}
        named = (
            db_query.keys() | db_acquire.keys() | db_release.keys()
            | db_listen.keys() | db_stream.keys() | db_txn.keys()
            | db_connection.keys()
        )
        for name in named:
            databases[name] = DatabaseDouble(
                name,
                query_faults=db_query.get(name),
                acquire_fault=db_acquire.get(name),
                release_fault=db_release.get(name),
                listen_faults=db_listen.get(name),
                stream_fault=db_stream.get(name),
                transaction_faults=db_txn.get(name),
                connection_faults=db_connection.get(name),
            )
        clients = {
            name: FaultyHttpClient(name, request_faults=faults)
            for name, faults in http_request.items()
        }
        stores = {
            name: ObjectStoreDouble(name, op_faults=faults)
            for name, faults in object_op.items()
        }
        return cls(databases=databases, clients=clients, object_stores=stores)


@contextmanager
def installed_adapters(app: Any, adapters: ReplayAdapters | None) -> Iterator[None]:
    """Install boundary doubles on `app` for the duration of a replay.

    Databases are swapped in place and the routes are marked dirty so the binder
    recompiles against the doubles; HTTP clients and object stores are swapped by
    name. Everything is restored on exit, even if the replay raised.
    """
    if adapters is None:
        yield
        return
    saved_databases = dict(getattr(app, "_databases", {}))
    saved_clients = dict(getattr(app, "_http_clients", {}))
    saved_stores = dict(getattr(app, "_object_stores", {}))
    databases = getattr(app, "_databases", None)
    clients = getattr(app, "_http_clients", None)
    stores = getattr(app, "_object_stores", None)
    registries = getattr(app, "_orm_registries", None)
    # An ORM Session acquires its connection from `registry.database`, so swap the
    # double there too -- that alone makes ORM Session-based handlers replay
    # against the double, no separate Session double needed.
    saved_registry_dbs: dict[str, Any] = {}
    try:
        if databases is not None:
            for name, double in adapters.databases.items():
                databases[name] = double
        if registries is not None:
            for name, double in adapters.databases.items():
                registry = registries.get(name)
                if registry is not None and hasattr(registry, "database"):
                    saved_registry_dbs[name] = registry.database
                    registry.database = double
        if clients is not None:
            for name, double in adapters.clients.items():
                clients[name] = double
        if stores is not None:
            for name, double in adapters.object_stores.items():
                stores[name] = double
        if adapters.databases and hasattr(app, "_dirty"):
            app._dirty = True  # force the binder to recompile against the doubles
        yield
    finally:
        if databases is not None:
            databases.clear()
            databases.update(saved_databases)
        if registries is not None:
            for name, original in saved_registry_dbs.items():
                registries[name].database = original
        if clients is not None:
            clients.clear()
            clients.update(saved_clients)
        if stores is not None:
            stores.clear()
            stores.update(saved_stores)
        if adapters.databases and hasattr(app, "_dirty"):
            app._dirty = True
