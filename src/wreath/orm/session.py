"""Request-scoped sessions.

A session owns one leased connection, one identity map, and the pending write
sets. It acquires the connection on first use and returns it exactly once, on
success, error, or cancellation.

Nothing here loads data implicitly: every statement this module issues is the
direct result of an awaited call.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from string.templatelib import Template
from time import monotonic_ns as _monotonic_ns
from typing import Any

from .. import _nplusone
from .._flight_markers import COV_PYTHON as _COV_PYTHON
from .._flight_markers import PH_ORM_HYDRATE as _PH_ORM_HYDRATE
from .._flight_markers import phase_marker as _phase_marker
from .._locks import _KEYED
from .._native import _core as _native_core
from .._nplusone import query_ledger as _query_ledger
from .._orm_events import has_subscribers as _has_write_subscribers
from .._orm_events import publish_write as _publish_write
from ..postgres import _WORKLOADS, Workload
from ..sql import Statement
from .compiler import (
    MAX_BIND_PARAMETERS,
    MAX_SELECTIN_KEYS,
    CompiledQuery,
    JoinedStep,
    LoadPlan,
    SelectinStep,
    compile_count,
    compile_delete,
    compile_delete_where,
    compile_insert,
    compile_select,
    compile_update,
    compile_update_where,
    qualified,
    quote,
)
from .compiler import (
    _count_write_sql_builds as _count_write_sql_builds,
)
from .errors import (
    DetachedInstanceError,
    MappingError,
    MultipleResultsError,
    NoResultError,
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


def _model_storage() -> Any:
    from .model import _storage

    return _storage


@dataclass(frozen=True, slots=True)
class FromORM:
    """Marks a handler parameter as a request-scoped ORM session.

    `database` names an `app.orm()` registry; it may be omitted when the
    application has exactly one.

    `tenant` says where the tenant binding comes from for an *isolated*
    registry, and `wreath.tenancy.FromTenant()` is the usual answer -- the
    tenant this request resolved to. It exists because without it the only route
    to a tenant-bound session was constructing `Session(registry, workload,
    tenant=...)` by hand in the handler body, which made the framework's own
    convenience the unsafe spelling and hand-rolling the safe one. An isolated
    registry with no `tenant` here is refused at route-compile time.
    """

    database: str | None = None
    workload: Workload = "read"
    tenant: Any = None

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
        plan = _row_plan(spec, order)  # once, not once per row
        return [
            item
            for item in (session._hydrate(spec, plan, row, 0) for row in rows)
            if item is not None
        ]


def _validate_raw_result(connection: Any, sql: str, spec: ModelSpec) -> tuple[ColumnSpec, ...]:
    plan = getattr(connection, "_plans", {}).get(sql)
    if plan is None:
        raise MappingError(
            "the driver reported no result description for this statement, so its "
            "columns cannot be checked against " + spec.model_type.__name__
        )
    names: tuple[str, ...] = plan.result_names
    oids: tuple[int, ...] = plan.result_oids
    # `Counter`, not `names.count(name)` per name: that spelling rescanned the
    # whole tuple once for every column, so checking a 50-column result cost
    # 2,500 comparisons to answer a question one pass settles.
    duplicates = sorted(name for name, seen in Counter(names).items() if seen > 1)
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


def _pk_offsets(spec: ModelSpec, columns: tuple[ColumnSpec, ...]) -> tuple[int, ...]:
    """Where `spec`'s primary-key columns sit within `columns`.

    Depends only on the compiled projection, so it is resolved once per query
    and reused for every row. It used to be rebuilt inside `_hydrate`, which
    made hydration O(rows x columns) in repeated setup -- and every joined
    load pays that per row *per join step*, because a joined shape always takes
    this decoded-record path rather than the direct decoder plan.
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
class _RowPlan:
    """Everything `_hydrate` needs that a row cannot change.

    The key offsets were lifted out of the per-row loop once already; the cell
    loop beside them kept reading `ColumnSpec.index` -- a `@property` returning
    `self.column.index` -- twice per column per row, and calling
    `PgType.from_wire`, which for most types returns its argument unchanged.
    Both are settled by the compiled projection, so both were O(rows x columns)
    Python frames spent re-deriving constants.

    `decode` is `PgType._from_wire` itself rather than the `from_wire` wrapper,
    so a column whose wire value is already its Python value carries `None` here
    and the row loop skips the call entirely instead of making it and being
    handed its argument back.
    """

    #: (offset of this key column within the row, decoder or None), in key order.
    key: tuple[tuple[int, Any], ...]
    #: (storage cell index, decoder or None), in projection order.
    cells: tuple[tuple[int, Any], ...]


def _row_plan(spec: ModelSpec, columns: tuple[ColumnSpec, ...]) -> _RowPlan:
    """Resolve one model's per-row constants, once per query.

    `_pk_offsets` is called rather than inlined, so the projection-missing-its-
    primary-key refusal and its call counter keep firing exactly where they did.
    """
    offsets = _pk_offsets(spec, columns)
    # `tuple([...])` rather than `tuple(genexpr)`: this runs once per query, and
    # a `fetch_one` amortises it over a single row, so its own cost decides
    # whether the hoist pays at all on the commonest shape. Measured on this
    # model, the two comprehensions are 0.84us against the generators' 1.31us.
    return _RowPlan(
        key=tuple(
            [
                (offset, item.pg_type._from_wire)
                for offset, item in zip(offsets, spec.primary_key, strict=True)
            ]
        ),
        cells=tuple([(item.column.index, item.pg_type._from_wire) for item in columns]),
    )


@dataclass(frozen=True, slots=True)
class _JoinCursor:
    """A joined step with its per-row constants already resolved."""

    step: JoinedStep
    plan: _RowPlan
    nested: tuple[_JoinCursor, ...]


def _join_cursors(steps: tuple[JoinedStep, ...]) -> tuple[_JoinCursor, ...]:
    """Resolve the whole joined tree's row plans once, before any row."""
    return tuple(
        _JoinCursor(
            step,
            _row_plan(step.relationship.target, step.columns),
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
#: crossing on every fetch.
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
        "_audit",
        "_audit_pending",
        "_broken",
        "_closed",
        "_connection",
        "_deleted",
        "_deleted_ids",
        "_dirty_items",
        "_written",
        "_depth",
        "_identity",
        "_identity_warn_at",
        "_identity_warned",
        "_new_ids",
        "_new_items",
        "_new_stale",
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
        audit: Any = None,
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
        # A `wreath.audit_log.AuditTrail`, or None. Typed loosely on purpose:
        # the ORM must not import the audit trail, which imports the log, which
        # imports the store -- the dependency runs the other way, and
        # `wreath.orm` is the layer everything else is allowed to depend on.
        self._audit = audit
        # Changes this flush has recorded but not yet appended. One list per
        # session rather than per flush, because a session that audits nothing
        # should allocate nothing: it stays empty and `_flush_inner` reads its
        # length.
        self._audit_pending: list[Any] = []
        self._registry = registry
        self._workload = workload
        self._connection: Any = None
        self._identity: dict[tuple[Any, tuple[Any, ...]], Any] = {}
        self._new_items: list[Any] = []
        self._new_stale = False
        self._new_ids: set[int] = set()
        self._deleted: list[Any] = []
        self._deleted_ids: set[int] = set()
        self._dirty_items: list[Any] = []
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

    async def _acquire(self) -> Any:
        if self._closed:
            raise SessionClosedError("this ORM session is closed")
        if self._connection is None:
            self._connection = await self._registry.database.acquire(self._workload)
        return self._connection

    async def close(self) -> None:
        """Roll back any open transaction and return the connection exactly once."""
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

    async def require(self, model: type, primary_key: Any, *, load: Iterable[Any] = ()) -> Any:
        """Fetch one object by primary key, raising when it does not exist."""
        result = await self.get(model, primary_key, load=load)
        if result is None:
            raise NoResultError(f"{model.__name__} with primary key {primary_key!r} does not exist")
        return result

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

    async def _fetch_compiled(self, query: Select, compiled: CompiledQuery) -> list[Any]:
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

    async def _fetch_one_compiled(self, query: Select, compiled: CompiledQuery) -> Any:
        results = await self._fetch_compiled(query, compiled)
        if len(results) > 1:
            raise MultipleResultsError(
                f"fetch_one() matched {len(results)} rows for "
                f"{query.model.__name__}; use fetch() or narrow the query"
            )
        return results[0] if results else None

    def _count_read(self, spec: ModelSpec | None) -> None:
        ledger = _query_ledger.get(None)
        if ledger is not None and spec is not None:
            model = spec.model_type
            ledger.record(f"{model.__module__}.{model.__qualname__}")

    async def _fetch_recorded(
        self, model_ids: dict[Any, int], connection: Any, compiled: CompiledQuery
    ) -> list[Any]:
        spec = compiled.result_model
        model_id = model_ids.get(spec.model_type, 0) if spec is not None else 0
        marker = _phase_marker.get(None) if model_id else None
        if marker is None:
            return await self._fetch_objects(
                connection, compiled, compiled.sql, compiled.bind_values
            )
        started = _monotonic_ns()
        try:
            return await self._fetch_objects(
                connection, compiled, compiled.sql, compiled.bind_values
            )
        finally:
            marker(_PH_ORM_HYDRATE, model_id, _COV_PYTHON, _monotonic_ns() - started)

    async def _fetch_objects(
        self,
        connection: Any,
        compiled: CompiledQuery,
        sql: str,
        args: tuple[Any, ...],
    ) -> list[Any]:
        plan = self._hydrate_plan(connection, compiled)
        if plan is not None:
            rows = await connection._fetch_into(sql, args, (plan, self._identity, self))
            identities = set(map(id, rows))
            if len(identities) == len(rows):
                return rows
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
        """Return a direct decoder plan, or `None` for Record hydration."""
        storage = _model_storage()
        if getattr(connection, "_decode_dest", None) is None:
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
            or len(compiled.selected_columns) != len(compiled.load_plan.columns)
        ):
            cached.hydrate_plan = False
            return None
        try:
            plan = storage._compile_hydrate_plan(
                spec.model_type,
                spec,
                tuple(item.index for item in compiled.load_plan.columns),
            )
        except TypeError, ValueError, IndexError, RuntimeError, MappingError:
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

    async def require_one(self, query: Select) -> Any:
        """Run `query` and require exactly one result."""
        result = await self.fetch_one(query)
        if result is None:
            raise NoResultError(f"require_one() matched no rows for {query.model.__name__}")
        return result

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

    def raw(self, sql: str | Template, *args: Any) -> RawQuery:
        """Run SQL exactly as written on this session's connection.

        A **t-string** is the spelling to reach for when any part of the
        statement is a value. `wreath.sql` compiles it to `$1`-style
        placeholders and binds what was interpolated, so the value cannot
        become syntax:

        ```python
        await db.raw(t"SELECT id FROM shipments WHERE reference = {ref}").fetch()
        ```

        A plain `str` is still accepted, and still means what it always meant:
        SQL wreath does not parse, rewrite, or cache, with `$1` placeholders
        bound from `args`. That is the right tool for a statement written in
        full at the call site. It is the wrong one for a statement assembled
        from a caller's text, which is what `wreath.hardening`'s SQL101 exists
        to say -- one character at the quote turns the wrong one into the right
        one.
        """
        self._check_usable()
        if isinstance(sql, Template):
            if args:
                raise SessionError(
                    "raw() takes either a t-string or SQL text with arguments, "
                    "not both: a t-string already carries its values"
                )
            statement = Statement(sql)
            if not statement.text:
                raise SessionError("raw() requires non-empty SQL")
            return RawQuery(self, statement.text, statement.args)
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
                f"load() takes a model relationship such as User.posts, got {relationship!r}"
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
            raise RegistryError(f"{owner.__name__}.{declared.python_name} is not registered")
        await self._load_relationship(found, instances, ())

    def _hydrate_rows(self, compiled: CompiledQuery, rows: list[Any]) -> list[Any]:
        spec = compiled.result_model
        if spec is None:
            raise MappingError("a hydrated query must declare its result model")
        plan = compiled.load_plan
        if not rows:
            # Empty results do not validate a projection's primary-key offsets.
            return []
        row_plan, cursors = self._record_plan(compiled, spec, plan)
        models = _model_storage()
        return _native_core.orm_hydrate_records(
            self, spec, row_plan, cursors, rows, models._MODEL_API
        )

    def _assemble_joins(self, cursors: tuple[_JoinCursor, ...], parent: Any, row: Any) -> None:
        models = _model_storage()
        _native_core.orm_assemble_joins(self, cursors, parent, row, models._MODEL_API)

    def _record_plan(
        self, compiled: CompiledQuery, spec: ModelSpec, plan: LoadPlan
    ) -> tuple[_RowPlan, tuple[_JoinCursor, ...]]:
        """Resolve and cache Record hydration constants by query shape."""
        cached = self._registry.cached_plan(compiled.shape_key)
        if cached is not None and cached.record_plan is not None:
            return cached.record_plan
        built = (_row_plan(spec, plan.columns), _join_cursors(plan.joined))
        if cached is not None:
            cached.record_plan = built
        return built

    def _hydrate(self, spec: ModelSpec, plan: _RowPlan, row: Any, offset: int) -> Any:
        key: list[Any] = []
        for index, decode in plan.key:
            value = row[offset + index]
            if value is None:
                return None
            key.append(value if decode is None else decode(value))
        identity = (spec, tuple(key))
        instance = self._identity.get(identity)
        if instance is None:
            instance = spec.model_type._orm_new()
            instance._orm_owner = self
            instance._orm_state = PERSISTENT
            self._identity[identity] = instance
        for index, (cell, decode) in enumerate(plan.cells):
            # A pending dirty value wins over later rows for the same identity.
            if instance._orm_is_dirty(cell):
                continue
            value = row[offset + index]
            instance._orm_set_loaded(cell, value if decode is None else decode(value))
        return instance

    async def _run_selectin(self, steps: tuple[SelectinStep, ...], objects: list[Any]) -> None:
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
        storage = _model_storage()

        # Deduplicate by key so one identity is fetched once no matter how many
        # parents share it.
        keys: dict[tuple[Any, ...], list[Any]] = _native_core.orm_relationship_keys(
            parents,
            tuple(column.index for column in local),
            relationship.index,
            many,
            storage._MODEL_API,
        )
        if not keys:
            return

        children: list[Any] = []
        # The projection is the target's own column tuple for every batch and
        # every row, so its key offsets resolve once for the whole load.
        record_plan = _row_plan(target, target.columns)
        direct_plan = None
        try:
            direct_plan = storage._compile_hydrate_plan(
                target.model_type,
                target,
                tuple(column.index for column in target.columns),
            )
        except TypeError, ValueError, IndexError, RuntimeError, MappingError:
            direct_plan = None
        batch_limit = _batch_limit(len(remote))
        remote_indexes = tuple(column.index for column in remote)
        identities = tuple(keys)
        for start in range(0, len(identities), batch_limit):
            batch = identities[start : start + batch_limit]
            sql, values = _selectin_sql(target, remote, _pad_to_width(batch, batch_limit))
            connection = await self._acquire()
            if direct_plan is not None and getattr(connection, "_decode_dest", None) is not None:
                batch_children = await connection._fetch_into(
                    sql, tuple(values), (direct_plan, self._identity, self)
                )
            else:
                rows = await connection.fetch(sql, *values)
                batch_children = [
                    child
                    for row in rows
                    if (child := self._hydrate(target, record_plan, row, 0)) is not None
                ]
            children.extend(batch_children)
            _native_core.orm_attach_relationships(
                keys,
                batch_children,
                remote_indexes,
                relationship.index,
                many,
                storage._MODEL_API,
            )
        if nested and children:
            for option in nested:
                found = target.relationship(option.relationship.python_name)
                if found is None:
                    raise RegistryError(
                        f"{target.model_type.__name__}."
                        f"{option.relationship.python_name} is not registered"
                    )
                await self._load_relationship(found, children, option.nested)

    # Identity keys keep equal but distinct unsaved rows scheduled separately.

    @property
    def _new(self) -> list[Any]:
        """The objects pending insertion, in add() order."""
        if self._new_stale:
            identifiers = self._new_ids
            self._new_items = [item for item in self._new_items if id(item) in identifiers]
            self._new_stale = False
        return self._new_items

    def _schedule_new(self, instance: Any) -> None:
        if _probes is not None:
            _probes[0] += 1
        key = id(instance)
        if key in self._new_ids:
            return
        self._new_ids.add(key)
        # Appending past tombstones is safe: they only ever precede the new
        # entry, and `_new` compacts before anyone reads the order.
        self._new_items.append(instance)
        instance._orm_owner = self

    def _unschedule_new(self, instance: Any) -> bool:
        if _probes is not None:
            _probes[0] += 1
        key = id(instance)
        if key not in self._new_ids:
            return False
        self._new_ids.remove(key)
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
        self._new_ids.clear()
        self._deleted.clear()
        self._deleted_ids.clear()
        self._dirty_items = []

    async def create(self, model: type, **values: Any) -> Any:
        """Construct, insert, and return one mapped model.

        The model constructor remains the single validation path. This is a
        session convenience, not a second model or repository abstraction.
        """
        self._check_usable()
        self._registry.spec_for(model)
        instance = model(**values)
        self.add(instance)
        await self.flush()
        return instance

    async def update_where(self, query: Select, **values: Any) -> int:
        """Update rows selected by an explicit predicate and return its count."""
        spec = self._registry.spec_for(query.model)
        if getattr(query.model, "__wreath_compiled_rules__", ()):
            raise SessionError(
                f"{query.model.__name__} has cross-field rules; update loaded "
                "objects so those rules run once, or use explicit SQL"
            )
        self._refuse_bulk_audit(query.model)
        sql, bind_values, _oids = compile_update_where(self._registry, query, values)
        return await self._execute_bulk(spec, "UPDATE", sql, bind_values)

    async def delete_where(self, query: Select) -> int:
        """Delete rows selected by an explicit predicate and return its count."""
        spec = self._registry.spec_for(query.model)
        self._refuse_bulk_audit(query.model)
        sql, bind_values, _oids = compile_delete_where(self._registry, query)
        return await self._execute_bulk(spec, "DELETE", sql, bind_values)

    def _refuse_bulk_audit(self, model: type) -> None:
        if getattr(model, "__wreath_facets__", {}).get("audit") is not None:
            raise SessionError(
                f"{model.__name__} is audited; a set-based write cannot produce "
                "one truthful change record per row. Update loaded objects or "
                "use an explicit audited statement."
            )

    async def _execute_bulk(
        self,
        spec: ModelSpec,
        verb: str,
        sql: str,
        bind_values: tuple[Any, ...],
    ) -> int:
        if self._depth:
            count = await self._execute_bulk_inner(verb, sql, bind_values)
            if count:
                self._written |= frozenset((spec.model_type.__name__,))
                self._detach_model(spec)
            return count
        async with self.begin():
            count = await self._execute_bulk_inner(verb, sql, bind_values)
            if count:
                self._written |= frozenset((spec.model_type.__name__,))
        # An internally owned transaction can still fail while leaving the
        # context manager (for example, while COMMIT is written). Do not
        # invalidate live objects until that commit has actually succeeded.
        # Caller-owned transactions detach immediately above because returning
        # a stale instance while that transaction continues would be a lie; a
        # later rollback merely leaves the conservative, reloadable DETACHED
        # state behind.
        if count:
            self._detach_model(spec)
        return count

    async def _execute_bulk_inner(self, verb: str, sql: str, bind_values: tuple[Any, ...]) -> int:
        connection = await self._acquire()
        status = await connection.execute(sql, *bind_values)
        return _affected_count(status, verb)

    def _detach_model(self, spec: ModelSpec) -> None:
        for key, instance in tuple(self._identity.items()):
            if key[0] is not spec:
                continue
            self._identity.pop(key)
            instance._orm_state = DETACHED
            instance._orm_owner = None
        self._dirty_items = [
            instance for instance in self._dirty_items if instance._orm_owner is self
        ]

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
                f"{spec.model_type.__name__} has no loaded primary key and cannot be deleted"
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
        names = {type(item).__name__ for item in self._new}
        names.update(type(item).__name__ for item in dirty)
        names.update(type(item).__name__ for item in self._deleted)
        return frozenset(names)

    def _has_pending(self) -> bool:
        return bool(self._new_ids or self._deleted or self._any_dirty())

    def _any_dirty(self) -> bool:
        return any(
            item._orm_has_changes() and item._orm_state == PERSISTENT for item in self._dirty_items
        )

    def _dirty_objects(self) -> list[Any]:
        return [
            item
            for item in self._dirty_items
            if item._orm_has_changes() and item._orm_state == PERSISTENT
        ]

    def _mark_dirty(self, instance: Any) -> None:
        self._dirty_items.append(instance)

    def _order(self, model: type) -> int:
        if _probes is not None:
            _probes[0] += 1
        try:
            return self._registry.order_of(model)
        except RegistryError:
            raise RegistryError(f"{model.__name__} is not registered") from None

    def _new_batches(self) -> list[list[Any]]:
        batches = [[] for _ in self._registry.specs]
        for instance in self._new:
            batches[self._order(type(instance))].append(instance)
        return batches

    async def _flush_inner(self) -> frozenset[str]:
        dirty = self._dirty_objects()
        written = (
            self._collect_written(dirty)
            if (_has_write_subscribers() or self._depth)
            else frozenset()
        )
        try:
            for batch in self._new_batches():
                for instance in batch:
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
            if self._audit_pending:
                await self._drain_audit()
        except BaseException:
            # Failed writes must not survive into the next audit batch.
            self._audit_pending = []
            raise
        self._clear_pending()
        return written

    def _audit_facet(self, instance: Any) -> Any:
        if self._audit is None:
            return None
        return getattr(type(instance), "__wreath_facets__", {}).get("audit")

    def _audit_write(self, instance: Any, spec: Any, operation: str, mask: int | None) -> None:
        """Hold the record for a write that has just happened.

        Called *after* the statement, because an insert's primary key is only
        known once `RETURNING` has answered, and because the values it reads --
        the dirty mask for an update, the row about to be detached for a delete
        -- are only correct at this moment. So the `Change` is *built* here and
        appended at the end of the flush, in one batch.

        Deferring the append and not its construction is what keeps the batch
        free: `_flush_inner` runs inside a transaction on every path (`flush`
        opens one when the caller has not), so a batch appended before it
        returns is in the same transaction as every write it describes and
        rolls back with them. What batching removes is a round trip per audited
        instance, which is all it removes.

        The attribution check runs *before* the statement; see the call sites.
        """
        from ..audit_log import Change, changed_fields

        facet = self._audit_facet(instance)
        if facet is None:
            return
        key = instance._orm_primary_key()
        self._audit_pending.append(
            Change(
                table=spec.qualified_name,
                key="" if key is None else ":".join(str(part) for part in key),
                operation=operation,
                actor=self._audit.attribute(),
                fields=changed_fields(instance, spec, facet, mask=mask),
            )
        )

    async def _drain_audit(self) -> None:
        """Append this flush's audit records, in one batch, before it returns.

        On the session's own connection, so the records share the fate of the
        rows. Taking a second connection here would put them in their own
        transaction, and a rolled-back write would leave a record saying it
        happened.

        Cleared before the append rather than after: a failed append must not
        leave the records queued for the *next* flush, where they would describe
        a transaction that has already rolled back.
        """
        pending = self._audit_pending
        self._audit_pending = []
        await self._audit.record_many(pending, connection=await self._acquire())

    def _audit_attribute(self, instance: Any) -> None:
        """Refuse an unattributed write before it reaches the database.

        Deliberately separate from recording it. A write that has already
        happened cannot be undone by a failed append, so the check that can
        refuse has to run first; the record that needs the row's key has to run
        second.
        """
        if self._audit_facet(instance) is not None:
            self._audit.attribute()

    async def _insert(self, instance: Any) -> None:
        spec = self._registry.spec_for(type(instance))
        # The mask `_insert_columns` splits on is also what keys the compiled
        # statement, so the linear split and the plan cache cannot disagree
        # about which columns an insert supplies.
        plan = compile_insert(self._registry, spec, _insert_mask(spec.columns, instance))
        returning = plan.returning
        values = [_wire_value(instance, item) for item in plan.columns]
        self._audit_attribute(instance)
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
        self._audit_write(instance, spec, "insert", None)

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
        self._audit_attribute(instance)
        connection = await self._acquire()
        status = await connection.execute(plan.sql, *values)
        _check_affected(status, spec, "UPDATE")
        # Recorded before the dirty mask is cleared: the mask *is* the record of
        # which fields this write set, and clearing it first would leave the
        # audit row saying "something changed" without saying what.
        self._audit_write(instance, spec, "update", mask)
        instance._orm_clear_dirty()

    async def _delete(self, instance: Any) -> None:
        spec = self._registry.spec_for(type(instance))
        plan = compile_delete(self._registry, spec)
        values = [_wire_value(instance, item) for item in plan.key_columns]
        self._audit_attribute(instance)
        connection = await self._acquire()
        status = await connection.execute(plan.sql, *values)
        _check_affected(status, spec, "DELETE")
        # Before the identity map drops it and the instance is detached: the
        # record needs the primary key, and detaching takes it away.
        self._audit_write(instance, spec, "delete", None)
        key = instance._orm_primary_key()
        if key is not None:
            self._identity.pop((spec, key), None)
        instance._orm_state = DETACHED
        instance._orm_owner = None

    @asynccontextmanager
    async def begin(self) -> Any:
        """Open a transaction, or a savepoint when one is already open."""
        self._check_usable()
        connection = await self._acquire()
        depth = self._depth
        savepoint = f"wreath_sp_{depth}"
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
            self._written = written_before
            raise
        else:
            self._depth = depth
            await self._unwind(connection, depth, savepoint, commit=True)
            if depth == 0 and self._written:
                # Publish only after the outermost commit is durable.
                written, self._written = self._written, frozenset()
                _publish_write(written)

    async def _unwind(self, connection: Any, depth: int, savepoint: str, *, commit: bool) -> None:
        if self._closed:
            # `close()` already returned this connection to the pool.
            return
        if commit:
            statement = "COMMIT" if depth == 0 else f"RELEASE SAVEPOINT {savepoint}"
        else:
            statement = "ROLLBACK" if depth == 0 else f"ROLLBACK TO SAVEPOINT {savepoint}"
        try:
            await connection.execute(statement)
        except BaseException:
            # Never return a connection with unproven transaction state.
            self._broken = True
            raise

    def _check_locking(self, query: Select) -> None:
        if not query.for_update_:
            return
        if self._workload not in _WRITE_WORKLOADS:
            raise SessionError(
                f"for_update() needs a write-workload session; this session is {self._workload!r}"
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
            raise SessionError(f"advisory lock mode must be 'exclusive' or 'shared', not {mode!r}")
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
                self._tenant.schema if self._tenant is not None else self._registry.database.name
            )
        function = (
            "pg_advisory_xact_lock" if mode == "exclusive" else "pg_advisory_xact_lock_shared"
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


def _affected_count(status: Any, verb: str) -> int:
    """Read PostgreSQL's ``VERB n`` command tag without inventing a count."""
    if isinstance(status, str):
        reported_verb, _separator, reported_count = status.partition(" ")
        if reported_verb == verb and reported_count.isascii() and reported_count.isdecimal():
            return int(reported_count)
    raise SessionError(
        f"the PostgreSQL adapter returned {status!r} for {verb}; expected "
        f"a {verb} <row-count> command tag"
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


def _pad_to_width(batch: tuple[tuple[Any, ...], ...], limit: int) -> tuple[tuple[Any, ...], ...]:
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


def _selectin_sql(
    target: ModelSpec, remote: tuple[ColumnSpec, ...], batch: tuple[tuple[Any, ...], ...]
) -> tuple[str, list[Any]]:
    """Build the batched relationship query Wreath issues on its own behalf.

    Wreath owns this statement, so it adds a primary-key tiebreaker for a stable
    order. User queries are never reordered.
    """
    values: list[Any] = []
    columns = ", ".join(f"{quote('t0')}.{quote(item.database_name)}" for item in target.columns)
    sql = f"SELECT {columns} FROM {qualified(target)} AS {quote('t0')} WHERE "
    if len(remote) == 1:
        column = remote[0]
        placeholders = []
        for key in batch:
            values.append(column.pg_type.to_wire(key[0]))
            placeholders.append(f"${len(values)}")
        sql += f"{quote('t0')}.{quote(column.database_name)} IN ({', '.join(placeholders)})"
    else:
        left = ", ".join(f"{quote('t0')}.{quote(item.database_name)}" for item in remote)
        rows = []
        for key in batch:
            placeholders = []
            for item, value in zip(remote, key, strict=True):
                values.append(item.pg_type.to_wire(value))
                placeholders.append(f"${len(values)}")
            rows.append(f"({', '.join(placeholders)})")
        sql += f"({left}) IN ({', '.join(rows)})"
    order = ", ".join(f"{quote('t0')}.{quote(item.database_name)}" for item in target.primary_key)
    return sql + f" ORDER BY {order}", values


def compile_session_binding(registries: Any, marker: FromORM) -> tuple[str, Any]:
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
    # Refused where a person is looking, not on the first request. A route that
    # binds a bare `FromORM` against an isolated registry is the mistake this
    # whole seam exists to prevent, and `Session` would refuse it per request
    # anyway -- at which point the application is already serving.
    if registry.schema_mode.kind == "isolated" and marker.tenant is None:
        raise TypeError(
            f"ORM registry {name!r} is tenant-isolated, so a session bound from it needs "
            "a tenant: write Annotated[Session, FromORM(tenant=FromTenant())]. Without "
            "one there is no schema for its statements to resolve in, and the pooled "
            "connection's last binding is another tenant's."
        )
    return name, registry


__all__ = ["FromORM", "RawQuery", "Session", "TenantContext", "compile_session_binding"]
