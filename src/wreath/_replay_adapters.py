"""Request-scoped boundary adapters and their fault injection (Stage 7).

Endpoint-plan replay can run a *real* handler (INVOKE) that reaches out to the
PostgreSQL driver or an outbound HTTP client. To replay that deterministically —
and, more valuably, to *red-team the owned handling of a boundary failure* — we
substitute injected doubles for those boundaries. A double either returns a
scripted result or raises a modeled fault (``pool-acquire timeout``, ``server
error after the round trip``, ``connection drop mid-result``, ...), and the
framework's owned recovery — error mapping, connection release, transaction
outcome — runs for real.

Adapters are explicit and request-scoped. They never touch a real socket, and
they are installed only for the duration of a replay:

- **PostgreSQL** doubles replace ``app._databases[name]``. Because the binder
  looks its database up by name at request time, installing a double plus forcing
  a route recompile routes every ``FromDatabase`` connection to it. The double
  counts acquisitions and releases, so a test can assert the framework returned
  the connection to the pool even on the error path.
- **Outbound HTTP** doubles subclass the real ``HTTPClient`` and override only
  the transport seam (``_request_timed``), so the client's own timeout, phase,
  and error handling runs while the modeled fault is injected beneath it.
"""

from __future__ import annotations

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
    "ReplayAdapters",
    "installed_adapters",
]


class AdapterFault(StrEnum):
    """A modeled failure at an owned boundary seam, keyed to an owned coordinate.

    Database seam faults are keyed to acquisition or to the Nth query on a leased
    connection; HTTP seam faults are keyed to the Nth outbound request.
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


def _db_error(fault: AdapterFault) -> Exception:
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
    return PostgresError("server error after the round trip began")


class _ConnectionDouble:
    """A leased-connection double: scripted results, or a query-keyed fault.

    Query methods advance a per-connection counter so a fault descriptor can name
    "the Nth query on this connection" — a stable owned coordinate.
    """

    __slots__ = ("_double", "_query")

    def __init__(self, double: DatabaseDouble) -> None:
        self._double = double
        self._query = 0

    def _next(self, default: Any) -> Any:
        index = self._query
        self._query += 1
        fault = self._double.query_faults.get(index)
        if fault is not None:
            raise _db_error(fault)
        results = self._double.results
        return results[index] if index < len(results) else default

    async def execute(self, sql: object, *args: object) -> str:
        return self._next("OK")

    async def fetch(self, sql: object, *args: object) -> list[Any]:
        return self._next([])

    async def fetchrow(self, sql: object, *args: object) -> Any:
        return self._next(None)

    async def fetchval(self, sql: object, *args: object) -> Any:
        return self._next(None)

    async def map(self, method: str, sql: object, argument_sets: Any, *, max_in_flight: int = 32):
        return self._next([])

    async def close(self) -> None:
        return None


class DatabaseDouble:
    """A ``Database`` double that scripts results and injects boundary faults.

    ``acquired``/``released`` count the owned pool lifecycle so a test can prove
    the framework returned the connection even when a query raised.
    """

    __slots__ = (
        "name", "results", "query_faults", "acquire_fault", "release_fault",
        "acquired", "released", "_flight_dep_id",
    )

    def __init__(
        self,
        name: str = "main",
        *,
        results: tuple[Any, ...] = (),
        query_faults: dict[int, AdapterFault] | None = None,
        acquire_fault: AdapterFault | None = None,
        release_fault: AdapterFault | None = None,
    ) -> None:
        self.name = name
        self.results = results
        self.query_faults = query_faults or {}
        self.acquire_fault = acquire_fault
        self.release_fault = release_fault
        self.acquired = 0
        self.released = 0
        self._flight_dep_id = 0

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
    """An ``HTTPClient`` whose transport seam injects modeled faults.

    Overriding only ``_request_timed`` keeps the client's owned timeout, phase,
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

    @classmethod
    def from_faults(cls, adapter_faults: Any) -> ReplayAdapters:
        """Build adapter doubles from a fault schedule's serialized adapter faults
        (``AdapterFaultDescriptor`` records). Each named target becomes one double
        carrying its acquire/query/release or request faults, so a checksummed
        schedule fully reconstructs the boundary perturbations for a replay."""
        from .replay import AdapterSeam  # local import: replay imports this module

        db_query: dict[str, dict[int, AdapterFault]] = {}
        db_acquire: dict[str, AdapterFault] = {}
        db_release: dict[str, AdapterFault] = {}
        http_request: dict[str, dict[int, AdapterFault]] = {}
        for fault in adapter_faults:
            kind = AdapterFault(fault.kind)
            if fault.seam == int(AdapterSeam.DB_ACQUIRE):
                db_acquire[fault.target] = kind
            elif fault.seam == int(AdapterSeam.DB_RELEASE):
                db_release[fault.target] = kind
            elif fault.seam == int(AdapterSeam.DB_QUERY):
                db_query.setdefault(fault.target, {})[fault.coordinate] = kind
            elif fault.seam == int(AdapterSeam.HTTP_REQUEST):
                http_request.setdefault(fault.target, {})[fault.coordinate] = kind
        databases: dict[str, DatabaseDouble] = {}
        for name in db_query.keys() | db_acquire.keys() | db_release.keys():
            databases[name] = DatabaseDouble(
                name,
                query_faults=db_query.get(name),
                acquire_fault=db_acquire.get(name),
                release_fault=db_release.get(name),
            )
        clients = {
            name: FaultyHttpClient(name, request_faults=faults)
            for name, faults in http_request.items()
        }
        return cls(databases=databases, clients=clients)


@contextmanager
def installed_adapters(app: Any, adapters: ReplayAdapters | None) -> Iterator[None]:
    """Install boundary doubles on ``app`` for the duration of a replay.

    Databases are swapped in place and the routes are marked dirty so the binder
    recompiles against the doubles; HTTP clients are swapped by name. Everything
    is restored on exit, even if the replay raised.
    """
    if adapters is None:
        yield
        return
    saved_databases = dict(getattr(app, "_databases", {}))
    saved_clients = dict(getattr(app, "_http_clients", {}))
    databases = getattr(app, "_databases", None)
    clients = getattr(app, "_http_clients", None)
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
        if adapters.databases and hasattr(app, "_dirty"):
            app._dirty = True
