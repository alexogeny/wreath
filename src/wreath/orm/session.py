"""Request-scoped sessions.

A session owns one leased connection, one identity map, and the pending write
sets. It acquires the connection on first use and returns it exactly once, on
success, error, or cancellation.

Nothing here loads data implicitly: every statement this module issues is the
direct result of an awaited call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from time import monotonic_ns as _monotonic_ns
from typing import Any

from .. import _nplusone
from .._flight_markers import COV_PYTHON as _COV_PYTHON
from .._flight_markers import PH_ORM_HYDRATE as _PH_ORM_HYDRATE
from .._flight_markers import phase_marker as _phase_marker
from .._locks import _KEYED
from .._nplusone import query_ledger as _query_ledger
from .._orm_events import has_subscribers as _has_write_subscribers
from .._orm_events import publish_write as _publish_write
from ..postgres import _WORKLOADS, Workload
from .compiler import (
    MAX_BIND_PARAMETERS,
    MAX_SELECTIN_KEYS,
    CompiledQuery,
    JoinedStep,
    SelectinStep,
    _count_write_sql_builds,
    compile_count,
    compile_delete,
    compile_insert,
    compile_select,
    compile_update,
    qualified,
    quote,
)
from .errors import (
    DetachedInstanceError,
    MappingError,
    MultipleResultsError,
    ORMError,
    RegistryError,
    SessionClosedError,
    SessionError,
    StaleDataError,
)
from .model import DELETED, DETACHED, PERSISTENT, TRANSIENT, Model, validate_identifier
from .query import Select
from .relations import LoadOption, Relationship, RelationshipExpr
from .schema import ColumnSpec, ModelSpec, RelationshipSpec

_WRITE_WORKLOADS = frozenset({"write"})


def _native_models() -> Any:
    """The native hydration module, or None when models are stored in Python."""
    from .model import _native

    if _native is None or not hasattr(_native, "_compile_hydrate_plan"):
        return None
    return _native


@dataclass(frozen=True, slots=True)
class FromORM:
    """Marks a handler parameter as a request-scoped ORM session.

    `database` names an `app.orm()` registry; it may be omitted when the
    application has exactly one.
    """

    database: str | None = None
    workload: Workload = "read"

    def __post_init__(self) -> None:
        if self.workload not in _WORKLOADS:
            raise ValueError(f"unknown PostgreSQL workload: {self.workload}")
        if self.workload == "security_read":
            raise ValueError("security_read connections cannot back an ORM session")


class RawQuery:
    """Unmodified SQL, executed on this session's leased connection.

    Wreath does not parse, rewrite, or cache the SQL; results come back as the
    driver's own `Record` objects unless `models()` is used.
    """

    __slots__ = ("_args", "_session", "_sql")

    def __init__(self, session: Session, sql: str, args: tuple[Any, ...]) -> None:
        self._session = session
        self._sql = sql
        self._args = args

    # Every entry point checks the tenant binding. Compiled queries route
    # through `Session.fetch`/`count`, which check it once; raw SQL is the one
    # path that reaches the connection directly, and it is also the path whose
    # statements are *unqualified by construction* -- so an unbound tenant
    # session running raw SQL resolves against whatever `search_path` the
    # pooled connection last held, which is another tenant's.

    async def execute(self) -> str:
        self._session._check_tenant_bound()
        connection = await self._session._acquire()
        return await connection.execute(self._sql, *self._args)

    async def fetch(self) -> list[Any]:
        self._session._check_tenant_bound()
        connection = await self._session._acquire()
        return await connection.fetch(self._sql, *self._args)

    async def fetchrow(self) -> Any:
        self._session._check_tenant_bound()
        connection = await self._session._acquire()
        return await connection.fetchrow(self._sql, *self._args)

    async def fetchval(self) -> Any:
        self._session._check_tenant_bound()
        connection = await self._session._acquire()
        return await connection.fetchval(self._sql, *self._args)

    async def models(self, model: type) -> list[Any]:
        """Hydrate this result into `model` instances.

        The result must contain every column of the model exactly once, named
        as in the database. Extra or missing columns are rejected rather than
        silently dropped, so a drifting query fails loudly.
        """
        session = self._session
        session._check_tenant_bound()
        spec = session._registry.spec_for(model)
        connection = await session._acquire()
        rows = await connection.fetch(self._sql, *self._args)
        order = _validate_raw_result(connection, self._sql, spec)
        if not rows:
            return []
        offsets = _pk_offsets(spec, order)     # once, not once per row
        return [
            item
            for item in (
                session._hydrate(spec, order, row, 0, offsets) for row in rows
            )
            if item is not None
        ]


def _validate_raw_result(
    connection: Any, sql: str, spec: ModelSpec
) -> tuple[ColumnSpec, ...]:
    plan = getattr(connection, "_plans", {}).get(sql)
    if plan is None:
        raise MappingError(
            "the driver reported no result description for this statement, so its "
            "columns cannot be checked against " + spec.model_type.__name__
        )
    names: tuple[str, ...] = plan.result_names
    oids: tuple[int, ...] = plan.result_oids
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise MappingError(
            f"result names {', '.join(duplicates)} appear more than once; "
            f"{spec.model_type.__name__} needs each column exactly once"
        )
    expected = set(spec.by_database_name)
    returned = set(names)
    missing = sorted(expected - returned)
    extra = sorted(returned - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise MappingError(
            f"result does not match {spec.model_type.__name__}: {'; '.join(details)}"
        )
    order: list[ColumnSpec] = []
    for name, oid in zip(names, oids, strict=True):
        column = spec.by_database_name[name]
        if oid != column.oid:
            raise MappingError(
                f"result column {name!r} has OID {oid}, but "
                f"{spec.model_type.__name__}.{column.python_name} declares "
                f"{column.pg_type.name} (OID {column.oid})"
            )
        order.append(column)
    return tuple(order)


# Test-only: when a counter is installed, the session records each identity
# membership probe and model-order lookup so tests can assert the work stays
# linear without timing anything. `None` in production keeps this to one
# predictable global load per probe.
_probes: list[int] | None = None


@contextmanager
def _count_probes() -> Iterator[list[int]]:
    """Count session bookkeeping probes performed inside the block."""
    global _probes
    counter = [0]
    previous, _probes = _probes, counter
    try:
        yield counter
    finally:
        _probes = previous


# Test-only, same contract as `_probes`: counts how many times a projection's
# primary-key offsets were resolved. The invariant is that this is a function of
# the query *shape*, not of the row count.
_key_map_builds: list[int] | None = None


@contextmanager
def _count_key_map_builds() -> Iterator[list[int]]:
    """Count primary-key offset resolutions performed inside the block."""
    global _key_map_builds
    counter = [0]
    previous, _key_map_builds = _key_map_builds, counter
    try:
        yield counter
    finally:
        _key_map_builds = previous


# Re-exported so write-path probes are reached the same way as the two above;
# the counter itself lives in the compiler, beside the code it counts.
_count_write_sql_builds = _count_write_sql_builds


def _pk_offsets(spec: ModelSpec, columns: tuple[ColumnSpec, ...]) -> tuple[int, ...]:
    """Where `spec`'s primary-key columns sit within `columns`.

    Depends only on the compiled projection, so it is resolved once per query
    and reused for every row. It used to be rebuilt inside `_hydrate`, which
    made hydration O(rows x columns) in pure repetition -- and every joined
    load pays that per row *per join step*, because a joined shape always takes
    this Record path rather than the native hydrate plan.
    """
    if _key_map_builds is not None:
        _key_map_builds[0] += 1
    positions = {item.python_name: index for index, item in enumerate(columns)}
    try:
        return tuple(positions[item.python_name] for item in spec.primary_key)
    except KeyError:
        raise MappingError(
            f"{spec.model_type.__name__} was selected without its primary key"
        ) from None


@dataclass(frozen=True, slots=True)
class _JoinCursor:
    """A joined step with its primary-key offsets already resolved."""

    step: JoinedStep
    pk_offsets: tuple[int, ...]
    nested: tuple[_JoinCursor, ...]


def _join_cursors(steps: tuple[JoinedStep, ...]) -> tuple[_JoinCursor, ...]:
    """Resolve the whole joined tree's key offsets once, before any row."""
    return tuple(
        _JoinCursor(
            step,
            _pk_offsets(step.relationship.target, step.columns),
            _join_cursors(step.nested),
        )
        for step in steps
    )


@dataclass(frozen=True, slots=True)
class TenantContext:
    """A validated, request-scoped tenant binding for an isolated registry.

    `schema` is the tenant's physical PostgreSQL schema; `role` is an
    optional database role selected transaction-locally when the deployment
    relies on PostgreSQL to enforce hostile-tenant isolation. Both are
    validated as unquoted identifiers at construction, so the transaction-local
    `SET LOCAL` statements the session issues can never carry untrusted text.

    A context must be resolved from an application-owned tenant directory, never
    from a request host, path, header, or token. It is transaction-local by
    construction: the session binds it after `BEGIN` and PostgreSQL discards
    it at transaction end, so a pooled connection never leaks one tenant's
    binding into the next lease.
    """

    schema: str
    role: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.schema, "tenant schema")
        if self.role is not None:
            validate_identifier(self.role, "tenant role")

    def _bind_statements(self) -> tuple[str, ...]:
        """The transaction-local statements that bind this context."""
        statements = [f"SET LOCAL search_path = {quote(self.schema)}"]
        if self.role is not None:
            statements.append(f"SET LOCAL ROLE {quote(self.role)}")
        return tuple(statements)


#: A suggested threshold for `identity_map_warn_at`, for a caller who wants one
#: without picking a number. Not the default: the check costs a boundary
#: crossing on every fetch, and `docs/agents/request-boundary-baseline.json`
#: is not the place to spend one on a diagnostic that fires for almost nobody.
#: Turn it on while chasing memory, or set it on the registry for a service that
#: has been bitten.
SUGGESTED_IDENTITY_MAP_WARN_AT = 50_000


class Session:
    """One unit of work over one leased connection.

    **The identity map holds every object this session hydrated**, for as long
    as the session is open. That is what makes the map an identity map -- two
    reads of one row give one object, and a pending change is not lost to a
    refetch -- and it means a fetch of a million rows pins a million objects
    until `close()`. There is deliberately no ceiling: evicting an entry would
    detach an object the caller may still be holding, which changes what the ORM
    means rather than bounding it. Past `identity_map_warn_at` the session emits
    one `ResourceWarning`, so the situation is visible before it is a memory
    incident. Page large reads, or open a session per page.
    """

    __slots__ = (
        "_broken",
        "_closed",
        "_connection",
        "_deleted",
        "_deleted_ids",
        "_written",
        "_depth",
        "_identity",
        "_identity_warn_at",
        "_identity_warned",
        "_new_items",
        "_new_ordinals",
        "_new_stale",
        "_ordinal",
        "_registry",
        "_statement_timeout",
        "_tenant",
        "_workload",
    )

    def __init__(
        self,
        registry: Any,
        workload: Workload,
        *,
        tenant: TenantContext | None = None,
        statement_timeout: float | None = None,
        identity_map_warn_at: int | None = None,
    ) -> None:
        isolated = getattr(getattr(registry, "schema_mode", None), "kind", None) == "isolated"
        if isolated and tenant is None:
            raise SessionError(
                "an isolated tenant registry needs a tenant context; pass "
                "Session(registry, workload, tenant=TenantContext(schema=...))"
            )
        if tenant is not None and not isolated:
            raise SessionError(
                "tenant context is only meaningful for an isolated registry; this "
                "registry resolves every model to a qualified schema"
            )
        # Seconds a statement may run before PostgreSQL cancels it. Applied as
        # `SET LOCAL` inside the transaction rather than on the connection: a
        # session-level `SET` would outlive this unit of work and travel with
        # the pooled connection into somebody else's. Falls back to the
        # registry's default so an application configures it once.
        if statement_timeout is None:
            statement_timeout = getattr(registry, "statement_timeout", None)
        if statement_timeout is not None and statement_timeout <= 0:
            raise SessionError("statement_timeout must be positive")
        self._statement_timeout = statement_timeout
        self._tenant = tenant
        self._registry = registry
        self._workload = workload
        self._connection: Any = None
        self._identity: dict[tuple[Any, tuple[Any, ...]], Any] = {}
        self._new_items: list[Any] = []
        self._new_stale = False
        self._new_ordinals: dict[int, int] = {}
        self._deleted: list[Any] = []
        self._deleted_ids: set[int] = set()
        self._ordinal = 0
        self._depth = 0
        # Model names written inside an open transaction, published on commit.
        self._written: frozenset[str] = frozenset()
        self._closed = False
        self._broken = False
        # Read from the registry only when the caller did not say, and only
        # inside `_note_identity_size` -- a `getattr` here is a boundary
        # crossing on every session, i.e. on every request, for a diagnostic
        # that is off.
        self._identity_warn_at = identity_map_warn_at
        self._identity_warned = False

    @property
    def registry(self) -> Any:
        return self._registry

    @property
    def workload(self) -> Workload:
        return self._workload

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    @property
    def connection(self) -> Any:
        """The leased connection, or None while the session is still lazy."""
        return self._connection

    # -- lifecycle ----------------------------------------------------------

    async def _acquire(self) -> Any:
        if self._closed:
            raise SessionClosedError("this ORM session is closed")
        if self._connection is None:
            self._connection = await self._registry.database.acquire(self._workload)
        return self._connection

    async def close(self) -> None:
        """Roll back any open transaction and return the connection exactly once."""
        if self._closed:
            return
        self._closed = True
        connection = self._connection
        self._connection = None
        for item in self._identity.values():
            item._orm_state = DETACHED
            item._orm_owner = None
        self._identity.clear()
        self._clear_pending()
        if connection is None:
            return
        try:
            if self._depth:
                await self._rollback_all(connection)
        finally:
            self._depth = 0
            if self._broken and not getattr(connection, "closed", False):
                # The pool discards a closed connection, which is the only safe
                # outcome when transaction state cannot be proven synchronized.
                await connection.close()
            await self._registry.database.release(self._workload, connection)

    async def _rollback_all(self, connection: Any) -> None:
        try:
            await connection.execute("ROLLBACK")
        except asyncio.CancelledError:
            # Still a connection whose transaction state cannot be proven, so it
            # is still broken -- but a cancellation is not ours to swallow, and
            # catching BaseException here meant `close()` absorbed one and the
            # task carried on as though it had not been cancelled.
            self._broken = True
            raise
        except Exception:  # noqa: BLE001 -- every failure means the same thing here
            # A rollback that did not complete leaves transaction state unproven
            # whatever went wrong, so the type carries no information the caller
            # could act on -- and `_close_connection`'s `finally` already turns
            # `_broken` into the only safe outcome, discarding the connection.
            # Swallowing is deliberate and matches cleanup-while-unwinding
            # elsewhere: this runs on the way out of a failure, so raising here
            # would replace the error the caller actually needs with a rollback
            # detail. Not counted -- `_broken` is the signal.
            self._broken = True
        except BaseException:
            # KeyboardInterrupt and SystemExit leave the transaction just as
            # unproven, so the connection is still broken -- but they are no more
            # ours to absorb than a cancellation, and a swallowed SystemExit
            # hands a dirty connection back to a pool that outlives it.
            self._broken = True
            raise

    def check_identity_map(self) -> None:
        """Warn, once, if this session is holding an unusual number of objects.

        **Nothing calls this for you.** Every automatic placement costs a
        boundary crossing on a path the whole application pays for -- per fetch,
        or per session close -- and `wreath-request-trace` prices each at +1 on
        the realistic scenario. That is a poor trade for a diagnostic that fires
        for almost nobody, so the check is a thing you *call* while chasing a
        memory problem:

            rows = await session.fetch(Big.select())
            session.check_identity_map()

        Set `identity_map_warn_at` on the session or the registry to pick the
        threshold; without one this does nothing.
        """
        threshold = self._identity_warn_at
        if threshold is None:
            threshold = getattr(self._registry, "identity_map_warn_at", None)
        if threshold is None or self._identity_warned:
            return
        if len(self._identity) <= threshold:
            return
        self._identity_warned = True
        import warnings

        warnings.warn(
            f"this session's identity map holds {len(self._identity)} objects and "
            "keeps every one of them until the session closes; page the read or "
            "open a session per page",
            ResourceWarning,
            stacklevel=3,
        )

    def _check_usable(self) -> None:
        if self._closed:
            raise SessionClosedError("this ORM session is closed")

    def _check_tenant_bound(self) -> None:
        """Tenant SQL runs only under a bound context transaction.

        A tenant registry compiles its tenant-template models to unqualified
        SQL that resolves through `search_path`. That binding is transaction
        local, so a statement outside a transaction would run with whatever
        namespace the pooled connection last held — a cross-tenant hazard. A
        tenant session therefore requires an explicit
        `async with session.begin()` around its work, exactly as `for_update`
        does.
        """
        if self._tenant is not None and self._depth == 0:
            raise SessionError(
                "a tenant session must run inside an explicit transaction so its "
                "search_path is bound; wrap the work in 'async with session.begin():'"
            )

    # -- reads --------------------------------------------------------------

    async def get(self, model: type, primary_key: Any, *, load: Iterable[Any] = ()) -> Any:
        """Fetch one object by primary key, or None."""
        self._check_usable()
        spec = self._registry.spec_for(model)
        values = _key_tuple(spec, primary_key)
        query = Select.build(model, ())
        for column, value in zip(spec.primary_key, values, strict=True):
            query = query.where(column.column.expression == value)
        options = tuple(load)
        if options:
            query = query.include(*options)
        return await self.fetch_one(query.limit(1))

    async def fetch(self, query: Select) -> list[Any]:
        """Run `query` and hydrate every row."""
        self._check_usable()
        self._check_tenant_bound()
        compiled = compile_select(self._registry, query)
        self._check_locking(query)
        if _nplusone.WATCHING:
            # Gated on a module flag rather than reading the ContextVar
            # unconditionally: `ContextVar.get` is a boundary crossing, and an
            # application with no guard installed -- which is every production
            # one -- should not pay one per query to learn that.
            self._count_read(compiled.result_model)
        connection = await self._acquire()
        model_ids = self._registry._flight_model_ids
        if not model_ids:
            # No metadata image, so nothing is recording and there is no ID to
            # attribute to. Same reasoning: an empty dict is a truth test, not
            # a crossing.
            objects = await self._fetch_objects(
                connection, compiled, compiled.sql, compiled.bind_values
            )
        else:
            objects = await self._fetch_recorded(model_ids, connection, compiled)
        await self._run_selectin(compiled.load_plan.selectin, objects)
        return objects

    async def _fetch_compiled(
        self, query: Select, compiled: CompiledQuery
    ) -> list[Any]:
        """Execute a query whose immutable plan and bind values are ready.

        This deliberately mirrors `fetch`'s execution tail. Factoring the tail
        into a third async method puts another Python frame and await on every
        ordinary ORM read; declared queries are the exceptional entry point,
        so they carry the duplicate rather than taxing the general path.
        """
        self._check_usable()
        self._check_tenant_bound()
        self._check_locking(query)
        if _nplusone.WATCHING:
            # Gated on a module flag rather than reading the ContextVar
            # unconditionally: `ContextVar.get` is a boundary crossing, and an
            # application with no guard installed -- which is every production
            # one -- should not pay one per query to learn that.
            self._count_read(compiled.result_model)
        connection = await self._acquire()
        model_ids = self._registry._flight_model_ids
        if not model_ids:
            # No metadata image, so nothing is recording and there is no ID to
            # attribute to. Same reasoning: an empty dict is a truth test, not
            # a crossing.
            objects = await self._fetch_objects(
                connection, compiled, compiled.sql, compiled.bind_values
            )
        else:
            objects = await self._fetch_recorded(model_ids, connection, compiled)
        await self._run_selectin(compiled.load_plan.selectin, objects)
        return objects

    async def _fetch_one_compiled(
        self, query: Select, compiled: CompiledQuery
    ) -> Any:
        """The prepared-query twin of `fetch_one`, without recompiling shape."""
        results = await self._fetch_compiled(query, compiled)
        if len(results) > 1:
            raise MultipleResultsError(
                f"fetch_one() matched {len(results)} rows for "
                f"{query.model.__name__}; use fetch() or narrow the query"
            )
        return results[0] if results else None

    def _count_read(self, spec: ModelSpec | None) -> None:
        """Tell the request's query ledger which model this read hydrates.

        The N+1 seam. The ledger may raise -- that is the point, and it happens
        before the statement runs, so the traceback lands on the loop rather
        than on the row that finally overflowed it.

        Keyed by module *and* qualname, because the ledger counts per key and
        `__qualname__` alone is not unique across a tree: `billing.Invoice` and
        `reporting.Invoice` are both `"Invoice"`, so one query to each would
        count as two queries to one model and trip a guard on two innocent
        reads. Building the key costs an f-string per query, which only a
        request under a guard pays -- the caller gates this on
        `_nplusone.WATCHING`, and a guard is a development and staging tool.
        """
        ledger = _query_ledger.get(None)
        if ledger is not None and spec is not None:
            model = spec.model_type
            ledger.record(f"{model.__module__}.{model.__qualname__}")

    async def _fetch_recorded(
        self, model_ids: dict[Any, int], connection: Any, compiled: CompiledQuery
    ) -> list[Any]:
        """`_fetch_objects` under a recording app, timed and attributed.

        Split out so the ordinary path keeps its single branch and no clock
        reads. The phase carries the model's metadata-image ID, which is what
        lets a recorded trace say *fifty of them hydrated Trek* rather than
        *fifty queries*.
        """
        spec = compiled.result_model
        model_id = model_ids.get(spec.model_type, 0) if spec is not None else 0
        marker = _phase_marker.get(None) if model_id else None
        if marker is None:
            # Unsampled, or a model outside the image: attributing to ID 0 would
            # put these rows on whichever model happens to be first.
            return await self._fetch_objects(
                connection, compiled, compiled.sql, compiled.bind_values
            )
        started = _monotonic_ns()
        try:
            return await self._fetch_objects(
                connection, compiled, compiled.sql, compiled.bind_values
            )
        finally:
            # In a finally so a statement that raised still shows that it ran:
            # an N+1 that fails halfway is still an N+1.
            marker(_PH_ORM_HYDRATE, model_id, _COV_PYTHON, _monotonic_ns() - started)

    async def _fetch_objects(
        self,
        connection: Any,
        compiled: CompiledQuery,
        sql: str,
        args: tuple[Any, ...],
    ) -> list[Any]:
        """Hydrate `sql` into models, natively when the shape allows it.

        Both paths share the identity map and the same cell semantics, so which
        one runs is not observable beyond allocation counts.
        """
        plan = self._hydrate_plan(connection, compiled)
        if plan is not None:
            rows = await connection._fetch_into(sql, args, (plan, self._identity, self))
            # One row per match, so a key that matched twice yields the same
            # object twice; the object graph is right, the list needs collapsing.
            seen: set[int] = set()
            objects = []
            for item in rows:
                if id(item) not in seen:
                    seen.add(id(item))
                    objects.append(item)
            return objects
        records = await connection.fetch(sql, *args)
        return self._hydrate_rows(compiled, records)

    def _hydrate_plan(self, connection: Any, compiled: CompiledQuery) -> Any:
        """The native plan for this shape, or None to use the Record path.

        Direct hydration needs both a natively stored model and a connection
        that can decode into a destination. Anything else -- the reference
        driver, a test double, a joined load -- takes the Record path, which
        produces the same objects through the same identity map.
        """
        native = _native_models()
        if native is None or getattr(connection, "_decode_dest", None) is None:
            return None
        cached = self._registry.cached_plan(compiled.shape_key)
        if cached is None:
            return None
        if cached.hydrate_plan is not None:
            return cached.hydrate_plan or None
        spec = compiled.result_model
        if (
            spec is None
            or compiled.load_plan.joined
            or spec.storage.kind != "native"
            or len(compiled.selected_columns) != len(compiled.load_plan.columns)
        ):
            # Joined loads span several models per row; that assembly still runs
            # through the Record path.
            cached.hydrate_plan = False
            return None
        try:
            plan = native._compile_hydrate_plan(
                spec.model_type,
                spec,
                tuple(item.index for item in compiled.load_plan.columns),
            )
        except (TypeError, ValueError, IndexError, RuntimeError, MappingError):
            # The native compiler's whole documented failure set: a shape it
            # cannot lay out falls back to the Record path, which is what this
            # cache line records. Anything outside it -- a MemoryError, a bug in
            # the caller -- would otherwise pin every future fetch to the slower
            # path with no signal, which is a performance cliff nothing reports.
            cached.hydrate_plan = False
            return None
        cached.hydrate_plan = plan
        return plan

    async def fetch_one(self, query: Select) -> Any:
        """Run `query` and return one object, or None.

        Raises `MultipleResultsError` when the query matches more than one
        row; a stricter limit set by the caller is left alone.
        """
        self._check_usable()
        bounded = query if query.limit_ is not None and query.limit_ <= 2 else query.limit(2)
        results = await self.fetch(bounded)
        if len(results) > 1:
            raise MultipleResultsError(
                f"fetch_one() matched {len(results)} rows for "
                f"{query.model.__name__}; use fetch() or narrow the query"
            )
        return results[0] if results else None

    async def count(self, query: Select) -> int:
        """Count the rows `query` matches, without fetching or hydrating them.

        Emits `SELECT COUNT(*)` with the query's filters (and any to-one
        filter joins), ignoring its projection, ordering, and paging. This is
        what `wreath.pagination.paginate` uses for the page total, so a
        large result set costs one aggregate round trip rather than transferring
        and materializing every matching row.
        """
        self._check_usable()
        self._check_tenant_bound()
        sql, values, _oids = compile_count(self._registry, query)
        connection = await self._acquire()
        total = await connection.fetchval(sql, *values)
        return int(total) if total is not None else 0

    async def declared(self, sql: str, args: tuple[Any, ...]) -> list[Any]:
        """Run one statement rendered from a declaration, and return its rows.

        The seam `wreath.series` executes through. It exists rather than
        going via `raw` because a calculated view is compiled from model
        metadata the same way a `Select` is, so it owes the same two checks a
        `Select` gets: that the session is open, and that a tenant session is
        inside the transaction its `search_path` is bound by. Statement text
        comes from the compiler, never from a caller.
        """
        self._check_usable()
        self._check_tenant_bound()
        connection = await self._acquire()
        return await connection.fetch(sql, *args)

    def raw(self, sql: str, *args: Any) -> RawQuery:
        """Run SQL exactly as written on this session's connection."""
        self._check_usable()
        if not isinstance(sql, str) or not sql:
            raise SessionError("raw() requires non-empty SQL")
        return RawQuery(self, sql, args)

    async def load(self, target: Any, relationship: Any) -> None:
        """Load `relationship` on one object or a sequence, in one batch."""
        self._check_usable()
        self._check_tenant_bound()
        if isinstance(relationship, RelationshipExpr):
            declared = relationship.relationship
        elif isinstance(relationship, Relationship):
            declared = relationship
        else:
            raise TypeError(
                "load() takes a model relationship such as User.posts, got "
                f"{relationship!r}"
            )
        instances = [target] if isinstance(target, Model) else list(target)
        if not instances:
            return
        owner = declared.owner
        for item in instances:
            if type(item) is not owner:
                raise TypeError(
                    f"load() got a {type(item).__name__} for a relationship declared "
                    f"on {owner.__name__}"
                )
        spec = self._registry.spec_for(owner)
        found = spec.relationship(declared.python_name)
        if found is None:
            raise RegistryError(
                f"{owner.__name__}.{declared.python_name} is not registered"
            )
        await self._load_relationship(found, instances, ())

    # -- hydration ----------------------------------------------------------

    def _hydrate_rows(self, compiled: CompiledQuery, rows: list[Any]) -> list[Any]:
        spec = compiled.result_model
        assert spec is not None
        plan = compiled.load_plan
        objects: list[Any] = []
        if not rows:
            # Resolving offsets here would raise MappingError for a projection
            # missing its primary key even when nothing matched; an empty
            # result has never done that, so stay out of the way.
            return objects
        # Once per query, not once per row -- and once for the whole joined
        # tree, not once per step per row.
        pk_offsets = _pk_offsets(spec, plan.columns)
        cursors = _join_cursors(plan.joined)
        seen: set[int] = set()
        for row in rows:
            root = self._hydrate(spec, plan.columns, row, 0, pk_offsets)
            if root is None:
                continue
            if id(root) not in seen:
                seen.add(id(root))
                objects.append(root)
            self._assemble_joins(cursors, root, row)
        return objects

    def _assemble_joins(
        self, cursors: tuple[_JoinCursor, ...], parent: Any, row: Any
    ) -> None:
        for cursor in cursors:
            step = cursor.step
            child = self._hydrate(
                step.relationship.target,
                step.columns,
                row,
                step.offset,
                cursor.pk_offsets,
            )
            parent._orm_set_relation(step.relationship.index, child)
            if child is not None:
                self._assemble_joins(cursor.nested, child, row)

    def _hydrate(
        self,
        spec: ModelSpec,
        columns: tuple[ColumnSpec, ...],
        row: Any,
        offset: int,
        pk_offsets: tuple[int, ...],
    ) -> Any:
        key: list[Any] = []
        for item, index in zip(spec.primary_key, pk_offsets, strict=True):
            value = row[offset + index]
            if value is None:
                # A LEFT JOIN that matched nothing; not an object.
                return None
            key.append(item.pg_type.from_wire(value))
        identity = (spec, tuple(key))
        instance = self._identity.get(identity)
        if instance is None:
            instance = spec.model_type._orm_new()
            instance._orm_owner = self
            instance._orm_state = PERSISTENT
            self._identity[identity] = instance
        for index, item in enumerate(columns):
            # A dirty field is the session's pending change; a later row must
            # not silently revert it.
            if instance._orm_is_dirty(item.index):
                continue
            instance._orm_set_loaded(
                item.index, item.pg_type.from_wire(row[offset + index])
            )
        return instance

    # -- relationship loading ------------------------------------------------

    async def _run_selectin(
        self, steps: tuple[SelectinStep, ...], objects: list[Any]
    ) -> None:
        if not objects:
            return
        for step in steps:
            await self._load_relationship(step.relationship, objects, step.nested)

    async def _load_relationship(
        self,
        relationship: RelationshipSpec,
        parents: list[Any],
        nested: tuple[LoadOption, ...],
    ) -> None:
        many = relationship.cardinality == "many"
        local = relationship.local_columns
        remote = relationship.remote_columns
        target = relationship.target

        # Deduplicate by key so one identity is fetched once no matter how many
        # parents share it.
        keys: dict[tuple[Any, ...], list[Any]] = {}
        for parent in parents:
            key = _values_of(parent, local)
            if key is None:
                # A null foreign key cannot match anything.
                parent._orm_set_relation(relationship.index, [] if many else None)
                continue
            keys.setdefault(key, []).append(parent)
            parent._orm_set_relation(relationship.index, [] if many else None)
        if not keys:
            return

        children: list[Any] = []
        # The projection is the target's own column tuple for every batch and
        # every row, so its key offsets resolve once for the whole load.
        child_offsets = _pk_offsets(target, target.columns)
        batch_limit = _batch_limit(len(remote))
        for batch in _batches(tuple(keys), len(remote)):
            sql, values = _selectin_sql(
                target, remote, _pad_to_width(batch, batch_limit)
            )
            connection = await self._acquire()
            rows = await connection.fetch(sql, *values)
            for row in rows:
                child = self._hydrate(target, target.columns, row, 0, child_offsets)
                if child is None:
                    continue
                children.append(child)
                key = _values_of(child, remote)
                if key is None:
                    continue
                for parent in keys.get(key, ()):
                    if many:
                        parent._orm_get_relation(relationship.index).append(child)
                    else:
                        parent._orm_set_relation(relationship.index, child)
        if nested and children:
            for option in nested:
                found = target.relationship(option.relationship.python_name)
                if found is None:
                    raise RegistryError(
                        f"{target.model_type.__name__}."
                        f"{option.relationship.python_name} is not registered"
                    )
                await self._load_relationship(found, children, option.nested)

    # -- pending bookkeeping ------------------------------------------------
    #
    # `_new` and `_deleted` stay ordered lists because flush order is
    # observable. Membership and ordering are keyed by `id()`, never by model
    # `__eq__`/`__hash__`: two distinct unsaved rows may compare equal and must
    # still be written as two rows. The list holds a strong reference for as
    # long as the id is a key, so an id cannot be reused while it is scheduled.
    # Everything that touches these four attributes goes through the helpers
    # below so they cannot drift apart.

    @property
    def _new(self) -> list[Any]:
        """The objects pending insertion, in add() order."""
        if self._new_stale:
            ordinals = self._new_ordinals
            self._new_items = [
                item for item in self._new_items if id(item) in ordinals
            ]
            self._new_stale = False
        return self._new_items

    def _schedule_new(self, instance: Any) -> None:
        if _probes is not None:
            _probes[0] += 1
        key = id(instance)
        if key in self._new_ordinals:
            return
        self._new_ordinals[key] = self._ordinal
        self._ordinal += 1
        # Appending past tombstones is safe: they only ever precede the new
        # entry, and `_new` compacts before anyone reads the order.
        self._new_items.append(instance)
        instance._orm_owner = self

    def _unschedule_new(self, instance: Any) -> bool:
        if _probes is not None:
            _probes[0] += 1
        if self._new_ordinals.pop(id(instance), None) is None:
            return False
        # Dropping the ordinal is the removal; compacting the list here would
        # make unscheduling a whole unit of work quadratic.
        self._new_stale = True
        return True

    def _schedule_deleted(self, instance: Any) -> None:
        if _probes is not None:
            _probes[0] += 1
        key = id(instance)
        if key in self._deleted_ids:
            return
        self._deleted_ids.add(key)
        self._deleted.append(instance)

    def _clear_pending(self) -> None:
        self._new_items = []
        self._new_stale = False
        self._new_ordinals.clear()
        self._deleted.clear()
        self._deleted_ids.clear()

    # -- writes -------------------------------------------------------------

    def add(self, instance: Any) -> None:
        """Schedule `instance` for insertion on the next flush."""
        self._check_usable()
        _check_owned(self, instance)
        if instance._orm_state == PERSISTENT:
            return
        if instance._orm_state == DELETED:
            raise SessionError(
                f"{type(instance).__name__} is scheduled for deletion and cannot be added"
            )
        self._registry.spec_for(type(instance))
        self._schedule_new(instance)

    def delete(self, instance: Any) -> None:
        """Schedule `instance` for deletion on the next flush."""
        self._check_usable()
        _check_owned(self, instance)
        spec = self._registry.spec_for(type(instance))
        if instance._orm_state == TRANSIENT:
            self._unschedule_new(instance)
            return
        if instance._orm_primary_key() is None:
            raise SessionError(
                f"{spec.model_type.__name__} has no loaded primary key and cannot be "
                "deleted"
            )
        instance._orm_state = DELETED
        self._schedule_deleted(instance)

    async def flush(self) -> None:
        """Write pending inserts, updates, and deletes in a deterministic order.

        Outside an explicit transaction this opens one for the flush and
        commits or rolls it back atomically.

        That is also what makes this safe for an isolated-tenant session without
        the `_check_tenant_bound` guard the read paths carry: the
        `begin()` below binds `search_path` before any statement runs, so
        there is no window where tenant-template SQL sees the pooled
        connection's previous namespace. It is load-bearing rather than
        incidental -- do not "optimise" the transaction away for a single-
        statement flush.
        """
        self._check_usable()
        if not self._has_pending():
            return
        if self._depth:
            # Inside a caller's transaction: names accumulate on the session and
            # the outermost commit publishes them. A flush that is later rolled
            # back must not have invalidated anything.
            self._written |= await self._flush_inner()
            return
        async with self.begin():
            written = await self._flush_inner()
        # Published only once the transaction has committed.
        if written:
            _publish_write(written)

    def _collect_written(self, dirty: list[Any]) -> frozenset[str]:
        """Model names in the pending set, for write subscribers.

        Takes the dirty list rather than recomputing it: the caller has already
        scanned the identity map for this flush, and scanning twice for one
        flush is how a request that loaded five thousand rows paid to find the
        one it changed.
        """
        # The bare class name, which is the name `invalidate_on` and
        # `publish_write` callers use. Two models sharing one class name in
        # different modules therefore invalidate each other -- accepted, because
        # the failure is over-invalidation (a cache drops entries it did not
        # have to) rather than stale data, and qualifying the name would break
        # every caller that names a model as a string.
        names = {type(item).__name__ for item in self._new}
        names.update(type(item).__name__ for item in dirty)
        names.update(type(item).__name__ for item in self._deleted)
        return frozenset(names)

    def _has_pending(self) -> bool:
        # The ordinal map answers this without compacting the pending list, and
        # the dirty check short-circuits on the first changed object rather than
        # building the whole list -- this only has to answer *whether* anything
        # is pending. `_flush_inner` does the one scan that needs the list, and
        # it does it after `begin()`, so a write arriving while the transaction
        # opens is still picked up.
        return bool(self._new_ordinals or self._deleted or self._any_dirty())

    def _any_dirty(self) -> bool:
        return any(
            item._orm_has_changes() and item._orm_state == PERSISTENT
            for item in self._identity.values()
        )

    def _dirty_objects(self) -> list[Any]:
        return [
            item
            for item in self._identity.values()
            if item._orm_has_changes() and item._orm_state == PERSISTENT
        ]

    def _order(self, model: type) -> int:
        if _probes is not None:
            _probes[0] += 1
        try:
            return self._registry.order_of(model)
        except RegistryError:
            raise RegistryError(f"{model.__name__} is not registered") from None

    async def _flush_inner(self) -> frozenset[str]:
        # Collected before the pending set is cleared, and only when something
        # is listening -- an app with no cache subscribers pays one bool read.
        # Collected when anything is listening *or* when this flush is inside a
        # transaction: a subscriber that registers before the commit -- a
        # `@cached` handler decorated during startup, a broadcast attached by a
        # later hook -- would otherwise miss the names of a transaction that was
        # already open when it arrived, and see the write only from the next one.
        # One scan of the identity map per flush, shared by the name collection
        # and the update ordering below.
        dirty = self._dirty_objects()
        written = (
            self._collect_written(dirty)
            if (_has_write_subscribers() or self._depth)
            else frozenset()
        )
        ordinals = self._new_ordinals
        for instance in sorted(
            self._new, key=lambda item: (self._order(type(item)), ordinals[id(item)])
        ):
            await self._insert(instance)
        updates = sorted(
            dirty,
            key=lambda item: (self._order(type(item)), _sort_key(item)),
        )
        for instance in updates:
            await self._update(instance)
        for instance in sorted(
            self._deleted,
            key=lambda item: (-self._order(type(item)), _sort_key(item)),
        ):
            await self._delete(instance)
        self._clear_pending()
        return written

    async def _insert(self, instance: Any) -> None:
        spec = self._registry.spec_for(type(instance))
        # The mask `_insert_columns` splits on is also what keys the compiled
        # statement, so the linear split and the plan cache cannot disagree
        # about which columns an insert supplies.
        plan = compile_insert(self._registry, spec, _insert_mask(spec.columns, instance))
        returning = plan.returning
        values = [_wire_value(instance, item) for item in plan.columns]
        connection = await self._acquire()
        if returning:
            row = await connection.fetchrow(plan.sql, *values)
            if row is None:
                raise ORMError(
                    f"INSERT into {spec.qualified_name} returned no row for "
                    f"{spec.model_type.__name__}"
                )
            for index, item in enumerate(returning):
                instance._orm_set_loaded(item.index, item.pg_type.from_wire(row[index]))
        else:
            await connection.execute(plan.sql, *values)
        instance._orm_state = PERSISTENT
        instance._orm_clear_dirty()
        key = instance._orm_primary_key()
        if key is None:
            raise ORMError(
                f"{spec.model_type.__name__} has no primary key after INSERT; declare "
                "the key column so RETURNING can fill it"
            )
        self._identity[(spec, key)] = instance

    async def _update(self, instance: Any) -> None:
        spec = self._registry.spec_for(type(instance))
        mask = 0
        for position, item in enumerate(spec.columns):
            if instance._orm_is_dirty(item.index) and not item.primary_key:
                mask |= 1 << position
        if not mask:
            return
        plan = compile_update(self._registry, spec, mask)
        values = [_wire_value(instance, item) for item in plan.columns]
        values += [_wire_value(instance, item) for item in plan.key_columns]
        connection = await self._acquire()
        status = await connection.execute(plan.sql, *values)
        _check_affected(status, spec, "UPDATE")
        instance._orm_clear_dirty()

    async def _delete(self, instance: Any) -> None:
        spec = self._registry.spec_for(type(instance))
        plan = compile_delete(self._registry, spec)
        values = [_wire_value(instance, item) for item in plan.key_columns]
        connection = await self._acquire()
        status = await connection.execute(plan.sql, *values)
        _check_affected(status, spec, "DELETE")
        key = instance._orm_primary_key()
        if key is not None:
            self._identity.pop((spec, key), None)
        instance._orm_state = DETACHED
        instance._orm_owner = None

    # -- transactions --------------------------------------------------------

    @asynccontextmanager
    async def begin(self) -> Any:
        """Open a transaction, or a savepoint when one is already open."""
        self._check_usable()
        connection = await self._acquire()
        depth = self._depth
        savepoint = f"wreath_sp_{depth}"
        # What the enclosing transaction had already written when this block
        # opened. A rollback restores it rather than clearing it: a savepoint
        # undoes only the work done inside it, so the names from before are
        # still pending and still publish when the outermost commit lands.
        written_before = self._written
        await connection.execute("BEGIN" if depth == 0 else f"SAVEPOINT {savepoint}")
        if depth == 0 and self._statement_timeout is not None:
            # Transaction-local, so PostgreSQL discards it at COMMIT/ROLLBACK
            # and the pooled connection carries no timeout into its next lease.
            # Set on the outermost transaction only -- a savepoint inherits it,
            # and re-setting would let a nested block quietly widen the bound.
            milliseconds = int(self._statement_timeout * 1000)
            await connection.execute(f"SET LOCAL statement_timeout = {milliseconds}")
        if depth == 0 and self._tenant is not None:
            # Bind the tenant namespace (and role) transaction-locally, before
            # any tenant-template SQL runs. SET LOCAL is scoped to this
            # transaction, so PostgreSQL discards it at COMMIT/ROLLBACK and the
            # pooled connection carries no binding into its next lease.
            for statement in self._tenant._bind_statements():
                await connection.execute(statement)
        self._depth = depth + 1
        try:
            yield self
        except BaseException:
            self._depth = depth
            await self._unwind(connection, depth, savepoint, commit=False)
            # Rolled back: those writes never happened, so nothing is stale.
            # This applies at every depth. A savepoint that rolls back used to
            # leave its model names on the session, and the outer commit then
            # announced them -- invalidating caches for a write that was undone.
            # Over-invalidating is safe rather than wrong, but it contradicts
            # the rule this whole seam exists for.
            self._written = written_before
            raise
        else:
            self._depth = depth
            await self._unwind(connection, depth, savepoint, commit=True)
            if depth == 0 and self._written:
                # The outermost commit succeeded: everything written inside it
                # is now durable, so invalidation is safe to announce.
                written, self._written = self._written, frozenset()
                _publish_write(written)

    async def _unwind(
        self, connection: Any, depth: int, savepoint: str, *, commit: bool
    ) -> None:
        if self._closed:
            # close() already unwound the transaction and returned the
            # connection. A statement now would run against whoever holds the
            # lease next, so this block's exit is a no-op.
            return
        if commit:
            statement = "COMMIT" if depth == 0 else f"RELEASE SAVEPOINT {savepoint}"
        else:
            statement = "ROLLBACK" if depth == 0 else f"ROLLBACK TO SAVEPOINT {savepoint}"
        try:
            await connection.execute(statement)
        except BaseException:
            # Transaction state can no longer be proven; force the pool to
            # discard the connection rather than lease out a dirty one.
            self._broken = True
            raise

    def _check_locking(self, query: Select) -> None:
        if not query.for_update_:
            return
        if self._workload not in _WRITE_WORKLOADS:
            raise SessionError(
                "for_update() needs a write-workload session; this session is "
                f"{self._workload!r}"
            )
        if not self._depth:
            raise SessionError(
                "for_update() needs an explicit transaction; wrap the query in "
                "async with session.begin()"
            )

    async def lock(
        self,
        key: str,
        *,
        scope: str = "xact",
        mode: str = "exclusive",
        namespace: str | None = None,
    ) -> None:
        """Take a transaction-scoped PostgreSQL advisory lock on this session.

        `scope="xact"` (the default and recommended form) takes a lock that
        PostgreSQL releases automatically at `COMMIT`/`ROLLBACK`; it must run
        inside `async with session.begin():` and rides the connection the
        transaction already pins, so there is no explicit unlock and no
        connection-affinity bookkeeping.

        Advisory locks ignore `search_path`, so for an isolated-tenant session
        the tenant schema is folded into the lock namespace automatically -- two
        tenants never collide on the same *key*. Pass *namespace* to override
        (for example, to take a deliberately fleet-global lock).

        Session-*scoped* locks (held beyond the transaction) are intentionally not
        offered here: releasing them correctly is bound to returning the pooled
        connection, which `database.lock(...)` / `database.try_lock(...)` own.
        """
        self._check_usable()
        if mode not in ("exclusive", "shared"):
            raise SessionError(
                f"advisory lock mode must be 'exclusive' or 'shared', not {mode!r}"
            )
        if scope == "session":
            raise SessionError(
                "session-scoped advisory locks are not available on Session; use "
                "database.lock(...) / database.try_lock(...), which pin and release "
                "a dedicated connection. Session.lock supports scope='xact' only."
            )
        if scope != "xact":
            raise SessionError(f"advisory lock scope must be 'xact', not {scope!r}")
        if self._depth == 0:
            raise SessionError(
                "session.lock(scope='xact') needs an open transaction so the lock "
                "is released at COMMIT/ROLLBACK; wrap the work in "
                "'async with session.begin():'"
            )
        if namespace is None:
            namespace = (
                self._tenant.schema
                if self._tenant is not None
                else self._registry.database.name
            )
        function = (
            "pg_advisory_xact_lock"
            if mode == "exclusive"
            else "pg_advisory_xact_lock_shared"
        )
        connection = await self._acquire()
        # The same 64-bit, server-side key `wreath._locks` derives, so a lock
        # taken through a session and one taken through `database.lock(...)`
        # name the same lock.
        await connection.fetchval(f"SELECT {function}({_KEYED})", namespace, key)

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return (
            f"<Session {self._registry.database.name!r} {self._workload} {state} "
            f"identity={len(self._identity)}>"
        )


def _check_affected(status: Any, spec: ModelSpec, verb: str) -> None:
    """Raise when a write built from a loaded object matched no row.

    PostgreSQL reports `UPDATE <n>` / `DELETE <n>`; anything else -- a test
    double, a driver that reports nothing -- is left alone, because absence of
    evidence is not evidence of a lost write and this must not start raising for
    connections that never said anything either way.
    """
    if not isinstance(status, str) or not status.startswith(verb):
        return
    _, _, count = status.partition(" ")
    if count.strip() != "0":
        return
    raise StaleDataError(
        f"{verb} on {spec.model_type.__name__} matched no row; it was deleted or "
        "its key changed in another session"
    )


def _insert_mask(columns: Any, instance: Any) -> int:
    """Which columns this instance supplies, as a positional bitmask.

    The single definition of "supplied": loaded, and without a server default.
    `_insert_columns` renders it as two lists and `Session._insert` uses it as
    the key its compiled statement is cached under, so the rule cannot drift
    between the split and the cache.
    """
    mask = 0
    for position, item in enumerate(columns):
        if instance._orm_is_loaded(item.index) and item.server_default is None:
            mask |= 1 << position
    return mask


def _insert_columns(columns: Any, instance: Any) -> tuple[list[Any], list[Any]]:
    """Split a model's columns into "supplied" and "comes back from RETURNING".

    One pass. The `item not in columns` form this replaces re-scanned the
    supplied list once per column, which is quadratic in the column count on
    every insert -- invisible on a five-column model and not on a fifty-column
    one.
    """
    mask = _insert_mask(columns, instance)
    supplied: list[Any] = []
    returning: list[Any] = []
    for position, item in enumerate(columns):
        (supplied if mask & (1 << position) else returning).append(item)
    return supplied, returning


def _sort_key(instance: Any) -> tuple[Any, ...]:
    key = instance._orm_primary_key()
    # Sort by identity so a flush order never depends on dict iteration; the
    # repr fallback keeps unkeyed objects deterministic within one process.
    return tuple(str(item) for item in key) if key is not None else (repr(instance),)


def _check_owned(session: Session, instance: Any) -> None:
    if not isinstance(instance, Model):
        raise TypeError(f"expected a model instance, got {instance!r}")
    owner = instance._orm_owner
    if owner is not None and owner is not session:
        raise DetachedInstanceError(
            f"{type(instance).__name__} is owned by another session; an object "
            "cannot belong to two sessions"
        )


def _values_of(instance: Any, columns: tuple[ColumnSpec, ...]) -> tuple[Any, ...] | None:
    values: list[Any] = []
    for item in columns:
        if not instance._orm_is_loaded(item.index) or instance._orm_is_null(item.index):
            return None
        values.append(instance._orm_get(item.index))
    return tuple(values)


def _wire_value(instance: Any, column: ColumnSpec) -> Any:
    if instance._orm_is_null(column.index):
        return None
    return column.pg_type.to_wire(instance._orm_get(column.index))




def _key_tuple(spec: ModelSpec, primary_key: Any) -> tuple[Any, ...]:
    values = primary_key if isinstance(primary_key, tuple) else (primary_key,)
    if len(values) != len(spec.primary_key):
        raise TypeError(
            f"{spec.model_type.__name__} has a {len(spec.primary_key)}-column primary "
            f"key; got {len(values)} value(s)"
        )
    return values


#: The batch sizes a select-in load is allowed to use. A statement's text
#: contains one placeholder per key, so *every distinct key count* used to be a
#: distinct statement -- a plan-cache entry per shape, evicting the shapes that
#: matter. Rounding each batch up to one of these caps the number of shapes at
#: the length of this tuple, at the cost of repeating a key to fill the last
#: slot (which `IN` deduplicates for free).
_BATCH_WIDTHS: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024)


def _batch_widths() -> tuple[int, ...]:
    """The allowed batch sizes, smallest first."""
    return _BATCH_WIDTHS


def _pad_to_width(
    batch: tuple[tuple[Any, ...], ...], limit: int
) -> tuple[tuple[Any, ...], ...]:
    """Round a batch up to the next allowed width by repeating its last key.

    Never past `limit` -- the caller's key and bind-parameter bounds are the
    real ceiling, and padding through one to reach a rounder number would trade
    a plan-cache entry for a statement the driver refuses.
    """
    for width in _BATCH_WIDTHS:
        if len(batch) <= width <= limit:
            return batch + (batch[-1],) * (width - len(batch))
    return batch


def _batch_limit(width: int) -> int:
    """The largest batch the key and bind-parameter bounds allow."""
    return min(MAX_SELECTIN_KEYS, max(1, MAX_BIND_PARAMETERS // max(width, 1)))


def _batches(keys: tuple[tuple[Any, ...], ...], width: int) -> list[tuple[Any, ...]]:
    """Split identities into batches bounded by keys and by bind parameters."""
    limit = _batch_limit(width)
    return [keys[start : start + limit] for start in range(0, len(keys), limit)]


def _selectin_sql(
    target: ModelSpec, remote: tuple[ColumnSpec, ...], batch: tuple[tuple[Any, ...], ...]
) -> tuple[str, list[Any]]:
    """Build the batched relationship query Wreath issues on its own behalf.

    Wreath owns this statement, so it adds a primary-key tiebreaker for a stable
    order. User queries are never reordered.
    """
    values: list[Any] = []
    columns = ", ".join(
        f"{quote('t0')}.{quote(item.database_name)}" for item in target.columns
    )
    sql = f"SELECT {columns} FROM {qualified(target)} AS {quote('t0')} WHERE "
    if len(remote) == 1:
        column = remote[0]
        placeholders = []
        for key in batch:
            values.append(column.pg_type.to_wire(key[0]))
            placeholders.append(f"${len(values)}")
        sql += f"{quote('t0')}.{quote(column.database_name)} IN ({', '.join(placeholders)})"
    else:
        left = ", ".join(
            f"{quote('t0')}.{quote(item.database_name)}" for item in remote
        )
        rows = []
        for key in batch:
            placeholders = []
            for item, value in zip(remote, key, strict=True):
                values.append(item.pg_type.to_wire(value))
                placeholders.append(f"${len(values)}")
            rows.append(f"({', '.join(placeholders)})")
        sql += f"({left}) IN ({', '.join(rows)})"
    order = ", ".join(
        f"{quote('t0')}.{quote(item.database_name)}" for item in target.primary_key
    )
    return sql + f" ORDER BY {order}", values


def compile_session_binding(
    registries: Any, marker: FromORM
) -> tuple[str, Any]:
    """Resolve `marker` to a registry at route-compile time."""
    name = marker.database
    if name is None:
        if len(registries) != 1:
            raise TypeError(
                "Session injection requires FromORM(<database>) when the "
                "application registers more than one ORM registry"
            )
        name = next(iter(registries))
    try:
        registry = registries[name]
    except KeyError:
        raise TypeError(f"unknown ORM registry: {name}") from None
    return name, registry


__all__ = ["FromORM", "RawQuery", "Session", "TenantContext", "compile_session_binding"]
