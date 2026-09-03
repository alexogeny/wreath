"""Tenant resolution and PostgreSQL-enforced isolation.

A `TenantDirectory` is the application-owned authority that maps a trusted
tenant id to its schema, role and lifecycle state. `TenantHostLabel` and
`TenantHeader` extract a candidate id; `TenancyMiddleware` resolves it through
the directory, refuses unknown, suspended or unready tenants, and binds the
result for the request. Request host, path, header and token values never become
schema or role names directly.

`SchemaMode.isolated(isolation="role")` combines transaction-local
`search_path` selection with `SET LOCAL ROLE`. The tenant role owns privileges
on its schema and read access to the central schema. The application login role
is `NOINHERIT`, so it has no ambient tenant-table privilege outside a bound
transaction.

The boundary has explicit limits:

- a deliberate `SET ROLE` to another role of which the login is a member can
  cross tenants; source hardening and `isolation_report` expose that residual;
- a superuser or schema owner bypasses grants and is refused by
  `verify_isolation`;
- PostgreSQL catalog rows reveal object names even when grants prevent access to
  their data; confidential tenant names require a database-per-tenant design.

`connection_budget` prices database-per-tenant alternatives from server memory
and `max_connections`. Central models remain read-only to tenant roles and stay
behind the tenant schema in the search path, so tenant-local and shared data can
be joined in one statement.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ._jobcore import validate_identifier
from ._pgname import quote_identifier
from .orm.session import TenantContext

__all__ = [
    "BACKEND_MEMORY_BYTES",
    "CLIENT_MEMORY_BYTES",
    "ConnectionBudget",
    "ROLE_ISOLATION_IMPLEMENTED",
    "TENANCY_PREFLIGHT_SOURCE",
    "FromTenant",
    "InMemoryTenantDirectory",
    "IsolationReport",
    "Tenancy",
    "TenancyError",
    "TenancyMiddleware",
    "Tenant",
    "TenantDirectory",
    "TenantHeader",
    "TenantHostLabel",
    "TenantNotBound",
    "TenantNotReady",
    "TenantSessionClaim",
    "TenantSource",
    "TenantStatus",
    "TenantSuspended",
    "UnknownTenant",
    "cedar_context",
    "connection_budget",
    "check_enqueue_tenant",
    "current_tenant",
    "current_tenant_or_none",
    "deprovision_tenant",
    "find_schema_literals",
    "isolation_report",
    "provision_tenant",
    "require_connection_budget",
    "tenant_scope",
    "telemetry_attributes",
    "verify_isolation",
]

ROLE_ISOLATION_IMPLEMENTED = True

#: The `source` name `wreath doctor preflight` files tenancy findings under.
TENANCY_PREFLIGHT_SOURCE = "tenancy"


class TenancyError(Exception):
    """A refusal made before any tenant SQL is sent."""


class UnknownTenant(TenancyError):
    """The directory holds no tenant under that name.

    A miss is never a fallback. Serving a request against "no tenant" means
    serving it against whatever namespace the pooled connection last held, which
    belongs to somebody else.
    """


class TenantSuspended(TenancyError):
    """The tenant exists and may not be bound."""


class TenantNotReady(TenancyError):
    """The tenant exists and its schema is not yet at the target migration."""


class TenantNotBound(TenancyError):
    """A tenant-scoped registry was reached with no tenant resolved."""


class TenantStatus(StrEnum):
    """Where a tenant is in its life.

    Four values rather than a boolean, because "exists" and "may serve a
    request" are different questions and collapsing them is how a half-migrated
    tenant answers a request with a missing-relation error deep inside a
    handler.
    """

    #: Schema and role exist; the artifact has not been applied to the target.
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    #: Deliberately stopped. Requests refused, queued work not claimed.
    SUSPENDED = "suspended"
    #: Kept for the record, holding no data anybody may reach.
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class Tenant:
    """One tenant: who it is, where its data lives, and whether it may serve.

    `key` is the name the outside world uses -- a subdomain label, a header
    value, a claim. `schema` and `role` are physical placement, and they are
    deliberately separate fields rather than derived from the key: a key is
    customer-visible and may have to change, while a schema holding a terabyte
    may not, and deriving one from the other welds them together.
    """

    key: str
    schema: str
    role: str | None = None
    status: TenantStatus = TenantStatus.ACTIVE
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validated here, at the one place a directory row is written, rather
        # than where it is interpolated -- these three names reach DDL and
        # `SET LOCAL`, and a row is written once and read on every request.
        for value, kind in ((self.key, "tenant key"), (self.schema, "tenant schema")):
            try:
                validate_identifier(value, kind)
            except ValueError as error:
                raise TenancyError(str(error)) from error
        if self.role is not None:
            try:
                validate_identifier(self.role, "tenant role")
            except ValueError as error:
                raise TenancyError(str(error)) from error

    def require_bindable(self) -> None:
        """Raise unless this tenant may serve a request right now.

        Checked at the bind rather than at the route. On a route it is one
        forgotten decorator away from not happening; at the bind there is no way
        to reach the data without passing through it.
        """
        if self.status is TenantStatus.ACTIVE:
            return
        if self.status is TenantStatus.SUSPENDED:
            raise TenantSuspended(f"tenant {self.key!r} is suspended")
        if self.status is TenantStatus.PROVISIONING:
            raise TenantNotReady(
                f"tenant {self.key!r} is still provisioning: its schema exists and its "
                "migration artifact has not been applied"
            )
        raise TenantNotReady(f"tenant {self.key!r} is retired")

    def context(self) -> TenantContext:
        """The transaction-local binding for this tenant."""
        return TenantContext(schema=self.schema, role=self.role)


@runtime_checkable
class TenantDirectory(Protocol):
    """The application's own record of who its tenants are.

    A protocol rather than a base class: the directory is usually a table the
    application already has, and `migrations.TenantState` already says the
    runner never invents tenant identity.
    """

    def resolve(self, key: str) -> Tenant: ...

    def all(self) -> tuple[Tenant, ...]: ...


class InMemoryTenantDirectory:
    """A directory held in the process, for tests and single-tenant fixtures.

    Not for a fleet: it cannot see a tenant another worker provisioned. The
    lookup is a dict, so resolution is one hash of a short string on the request
    path rather than a scan.
    """

    __slots__ = ("_by_key",)

    def __init__(self, tenants: Iterable[Tenant] = ()) -> None:
        self._by_key: dict[str, Tenant] = {tenant.key: tenant for tenant in tenants}

    def resolve(self, key: str) -> Tenant:
        tenant = self._by_key.get(key)
        if tenant is None:
            raise UnknownTenant(
                f"no tenant named {key!r}; there is no default tenant, because a "
                "default is a request served against whichever schema the pooled "
                "connection last held"
            )
        return tenant

    def all(self) -> tuple[Tenant, ...]:
        return tuple(self._by_key.values())

    def add(self, tenant: Tenant) -> None:
        self._by_key[tenant.key] = tenant


@runtime_checkable
class TenantSource(Protocol):
    """How a request names a tenant. Never how it *chooses* one."""

    def name_for(self, request: Any) -> str | None: ...

    def describe(self) -> str: ...


def _single_selector(request: Any, name: str) -> str | None:
    single = getattr(request, "_single_header", None)
    if single is None:
        return request.header(name)
    try:
        raw_name = name.lower().encode("latin-1")
    except UnicodeEncodeError as error:
        raise TenancyError(
            f"tenant selector header {name!r} must be a Latin-1 header name"
        ) from error
    try:
        value = single(raw_name)
    except ValueError as error:
        raise TenancyError(f"tenant selector header {name!r} occurs more than once") from error
    if value is None:
        return None
    if not isinstance(value, bytes):
        raise TenancyError(f"tenant selector header {name!r} must be raw bytes")
    return value.decode("latin-1")


@dataclass(frozen=True, slots=True)
class TenantHeader:
    """The tenant name arrives in a header.

    For a gateway that has already authenticated the caller and rewritten the
    header. On an internet-facing listener a client-supplied header is a client
    choosing its own tenant *name* -- which is safe only because the name is
    looked up rather than used, and because the identity check still has to
    pass.
    """

    header: str
    trusted: bool = False

    def name_for(self, request: Any) -> str | None:
        # `request.header(name)`, never `request.headers.get(...)`: `headers` is
        # the raw ASGI list of byte pairs, not a mapping. The dict spelling was
        # here first, and only an end-to-end test through a real `Request`
        # caught it -- the unit tests had a fake whose `headers` was a dict, so
        # they were testing the fake.
        return _single_selector(request, self.header)

    def describe(self) -> str:
        return f"the {self.header} header"


@dataclass(frozen=True, slots=True)
class TenantHostLabel:
    """The tenant name is the first label of the host, under a fixed suffix.

    `acme.example.com` under `suffix="example.com"` is `acme`. The suffix is
    required and matched exactly: without it the apex `example.com` resolves
    `example` as a tenant, and `www` becomes a customer.
    """

    suffix: str
    #: `".example.com"`, built once. It was an f-string inside `name_for`, which
    #: allocated a new string on **every request** to compare against a constant
    #: -- measured at 657ns for the whole method against a ~2us request, of which
    #: this and the `len()` beside it were the only avoidable part. Deleting the
    #: work beat making it faster, which is the order AGENTS.md asks for.
    _dotted: str = field(init=False, repr=False, compare=False)
    _cut: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_dotted", f".{self.suffix.lower()}")
        object.__setattr__(self, "_cut", -len(self.suffix) - 1)

    def name_for(self, request: Any) -> str | None:
        host = _single_selector(request, "host")
        if not host:
            return None
        # One partition rather than a split: the host is short and only the
        # boundary matters, so there is no list to build. `lower()` after the
        # partition rather than before, so the port is not case-folded too.
        host = host.partition(":")[0].lower()
        if not host.endswith(self._dotted):
            return None
        label = host[: self._cut]
        return label or None

    def describe(self) -> str:
        return f"the host label under {self.suffix}"


@dataclass(frozen=True, slots=True)
class TenantSessionClaim:
    """The tenant name is a claim on the established session.

    The strongest of the three, and the one that needs an authenticated request:
    the name comes from state the server wrote, so a caller cannot name a tenant
    at all.
    """

    claim: str = "tenant"

    def name_for(self, request: Any) -> str | None:
        session = getattr(request.state, "session", None)
        if session is None:
            return None
        return session.get(self.claim)

    def describe(self) -> str:
        return f"the {self.claim!r} session claim"


@dataclass(frozen=True, slots=True)
class FromTenant:
    """Bind this session to the tenant the request resolved to.

    Written inside `FromORM`, never on its own:

        session: Annotated[Session, FromORM("main", tenant=FromTenant())]

    That spelling is the whole point of this class. Before it, the only route to
    a tenant-bound session was building `Session(registry, workload,
    tenant=TenantContext(...))` by hand in the handler body -- so the
    framework's own convenience produced an *unbound* session and the safe path
    was the one you had to remember. Now the declarative path is the safe one
    and the unsafe one is refused at route-compile time.
    """

    def resolve(self, request: Any) -> TenantContext:
        """The bound tenant's context, or refuse.

        Reads the ambient scope rather than the request, because the scope is
        what a job, a pass and a workflow also run under -- one binding to
        resolve, wherever the work is happening.
        """
        tenant = _CURRENT.get()
        if tenant is None:
            raise TenantNotBound(
                "this route binds a tenant session and no tenant is bound; install the "
                "tenancy middleware, or enter tenant_scope(...) before the handler runs"
            )
        return tenant.context()


class Tenancy:
    """Resolution: a source that *names* a tenant, and a directory that finds it.

    The two halves are separate because collapsing them is the whole
    vulnerability. A header used directly as a schema is a header that can name
    any schema in the database; a header used as a *key* into a directory can
    only ever name a tenant somebody provisioned.
    """

    __slots__ = ("_directory", "_source")

    def __init__(self, *, directory: TenantDirectory, source: TenantSource | None = None) -> None:
        if source is None:
            raise TenancyError(
                "Tenancy needs source=: there is no default place a tenant name comes "
                "from. Pass TenantHeader(...), TenantHostLabel(...), or "
                "TenantSessionClaim(...) -- guessing at a subdomain is how a service "
                "that was never multi-tenant on its apex starts resolving 'www' as a "
                "customer."
            )
        if isinstance(source, TenantHeader) and not source.trusted:
            raise TenancyError(
                "TenantHeader requires trusted=True after a trusted gateway has "
                "authenticated the caller and replaced the header; use "
                "TenantSessionClaim for an end-user supplied request"
            )
        self._directory = directory
        self._source = source

    @property
    def source(self) -> TenantSource:
        return self._source

    @property
    def directory(self) -> TenantDirectory:
        return self._directory

    def resolve_name(self, name: str) -> Tenant:
        """The directory's answer for one name, with its status checked."""
        tenant = self._directory.resolve(name)
        tenant.require_bindable()
        return tenant

    def resolve_request(self, request: Any) -> Tenant:
        """The tenant this request belongs to.

        Three steps and no fourth: read a name, look it up, check it may serve.
        Everything expensive about tenancy happens at provisioning time.
        """
        name = self._source.name_for(request)
        if not name:
            raise UnknownTenant(f"this request names no tenant in {self._source.describe()}")
        return self.resolve_name(name)


# A ContextVar rather than a parameter threaded through every call, for the same
# reason `wreath.telemetry` binds a span: the propagation targets are a job
# enqueue, a Cedar context and a log record, and threading a tenant through all
# three is a change to every signature between here and there. Read cost is one
# ContextVar lookup; there is no loop to price.

_CURRENT: ContextVar[Tenant | None] = ContextVar("wreath_tenant", default=None)
_ENQUEUE_TENANT: ContextVar[str | None] = ContextVar("wreath_enqueue_tenant", default=None)


def current_tenant() -> Tenant:
    """The tenant bound to this task, or raise `TenantNotBound`."""
    tenant = _CURRENT.get()
    if tenant is None:
        raise TenantNotBound(
            "no tenant is bound here; enter tenant_scope(...) or resolve one from the "
            "request before touching a tenant-scoped registry"
        )
    return tenant


def current_tenant_or_none() -> Tenant | None:
    """The tenant bound to this task, or `None`. For code that may run outside one."""
    return _CURRENT.get()


@contextmanager
def tenant_scope(
    tenant: Tenant | str,
    *,
    directory: TenantDirectory | None = None,
) -> Iterator[Tenant]:
    """Bind one tenant for the duration of the block.

    Takes a `Tenant` or, with a directory, a key. Restores the previous binding
    on exit rather than clearing it, so a nested scope in a background task
    cannot unbind its caller's.
    """
    if isinstance(tenant, str):
        if directory is None:
            raise TenancyError("tenant_scope was given a name and no directory to resolve it in")
        tenant = directory.resolve(tenant)
    tenant.require_bindable()
    token = _CURRENT.set(tenant)
    try:
        yield tenant
    finally:
        _CURRENT.reset(token)


def cedar_context() -> dict[str, str]:
    """The active tenant as Cedar sees it.

    A separate key from `context.organizations`, deliberately. An organisation
    is tenancy at the *identity* layer -- who you are acting as -- and a tenant
    is where the rows live. A deployment can have one without the other, and a
    policy that conflated them would be right until the first customer with two
    organisations in one database.
    """
    tenant = _CURRENT.get()
    return {} if tenant is None else {"tenant": tenant.key}


def telemetry_attributes() -> dict[str, str]:
    """The active tenant as a metric and span dimension.

    "Which tenant is slow" is the first question of every incident, and
    `wreath.metrics` already carries `instance` for the same reason.
    """
    tenant = _CURRENT.get()
    return {} if tenant is None else {"tenant": tenant.key}


def check_enqueue_tenant(explicit: str | None) -> str:
    """Reconcile an explicit `tenant=` with the bound scope.

    `JobRunner.enqueue(tenant="")` defaults to empty, so work enqueued inside a
    tenant request runs later against no tenant at all -- on a worker, hours
    away, with no request left to attribute it to. Inside a scope the scope
    wins, and an explicit *mismatch* is refused rather than silently resolved in
    either direction: one of the two spellings is a bug and there is no way to
    tell which.
    """
    tenant = _CURRENT.get()
    bound = tenant.key if tenant is not None else _ENQUEUE_TENANT.get()
    if bound is None:
        return explicit or ""
    if explicit and explicit != bound:
        raise TenancyError(
            f"enqueued with tenant={explicit!r} inside the {bound!r} tenant scope; "
            "one of the two is wrong and this cannot tell which. Drop the argument to "
            "inherit the scope."
        )
    return bound


@contextmanager
def _enqueue_tenant_scope(tenant: str) -> Iterator[None]:
    enqueue_token = _ENQUEUE_TENANT.set(tenant or None)
    tenant_token = _CURRENT.set(None)
    try:
        yield
    finally:
        _CURRENT.reset(tenant_token)
        _ENQUEUE_TENANT.reset(enqueue_token)


# The residual above -- a *deliberate* `SET ROLE other_tenant` -- exists because
# one connection can switch between every tenant role. Removing it means a
# connection that cannot: one whose login role is the tenant's own, holding
# membership of nothing.
# That was described as "expensive" before it was measured, which was a guess.
# Measured against PostgreSQL 17 on this machine, 100 connections opened and
# each made to run a statement so its backend allocates a real working set:
#     client memory      162 KiB per connection
#     connection setup  4.19 ms per connection
#     server memory      ~15 MiB per backend   <-- 93x the client side
# So the cost is not in the application at all, which is where the guess put it.
# It is server RAM and the `max_connections` ceiling, and both are multiplied by
# the number of workers: 200 tenants across 4 workers is 800 backends, roughly
# 12 GiB of database server, against a default `max_connections` of 100.
# That is affordable for tens of tenants and not for thousands, which makes it a
# **configuration** rather than a verdict -- and makes the arithmetic something
# wreath should do for you rather than leave you to discover in production.

#: What one PostgreSQL backend costs in server memory, measured rather than
#: assumed (see above). Approximate and machine-dependent -- it moves with
#: `work_mem`, `shared_buffers` and what the connection actually does -- so
#: `connection_budget` reports it as an estimate and says so.
BACKEND_MEMORY_BYTES = 15 * 1024 * 1024

#: What one connection costs the application process. Two orders of magnitude
#: below the server side, which is why the ceiling is never here.
CLIENT_MEMORY_BYTES = 162 * 1024


@dataclass(frozen=True, slots=True)
class ConnectionBudget:
    """Whether a fleet fits, and what it would cost if it did.

    Reported rather than silently accepted, because the failure it prevents is
    the worst kind: the deployment works in staging with three tenants and
    refuses connections in production at two hundred.
    """

    tenants: int
    workers: int
    per_tenant_connections: int
    max_connections: int

    @property
    def required(self) -> int:
        return self.tenants * self.workers * self.per_tenant_connections

    @property
    def fits(self) -> bool:
        # Deliberately not `<=`: PostgreSQL reserves connections for superusers
        # and for its own background work, and a fleet that exactly fills
        # `max_connections` leaves nothing for the operator who needs to get in
        # and find out why.
        return self.required < self.max_connections

    @property
    def server_memory_bytes(self) -> int:
        return self.required * BACKEND_MEMORY_BYTES

    def explain(self) -> str:
        gib = self.server_memory_bytes / (1024**3)
        return (
            f"{self.tenants} tenants x {self.workers} workers x "
            f"{self.per_tenant_connections} connections = {self.required} backends "
            f"against max_connections={self.max_connections}; roughly {gib:.1f} GiB of "
            "database server memory at ~15 MiB per backend (an estimate that moves "
            "with work_mem and shared_buffers)"
        )


def connection_budget(
    *,
    tenants: int,
    workers: int = 1,
    per_tenant_connections: int = 1,
    max_connections: int = 100,
) -> ConnectionBudget:
    """Price connection-per-tenant isolation before choosing it.

    `max_connections` defaults to PostgreSQL's own default rather than to
    something generous: a deployment that has not raised it is the one this is
    most likely to surprise.
    """
    return ConnectionBudget(
        tenants=tenants,
        workers=workers,
        per_tenant_connections=per_tenant_connections,
        max_connections=max_connections,
    )


def require_connection_budget(budget: ConnectionBudget) -> None:
    """Refuse a fleet that cannot fit, at startup, with the arithmetic shown.

    Naming the numbers rather than the conclusion: an operator who disagrees can
    raise `max_connections`, cut workers, or go back to role isolation, and none
    of those choices is wreath's to make.
    """
    if budget.fits:
        return
    raise TenancyError(
        "connection-per-tenant isolation does not fit this deployment: "
        + budget.explain()
        + ". Raise max_connections, reduce workers, or use isolation='role' -- which "
        "shares one pool and defends every accidental crossing, leaving only a "
        "deliberate SET ROLE."
    )


class TenancyMiddleware:
    """Resolve the request's tenant and bind it for the rest of the request.

    **Global middleware, not route middleware**, and for the same reason
    `SessionPolicy` is: the binding has to exist before a route's own tape
    runs, because an authorization hook that reads tenant-scoped data would
    otherwise run unbound. `wreath.app` already refuses the wrong spelling for
    sessions and the same argument applies here.

    The `ContextVar` is set in `before` and reset in `after`, which is the
    lifetime the tape gives a hook. A request that never reaches `after` --
    because something above it raised -- leaves the token unreset on a task that
    is ending anyway; a `ContextVar` is per-task, so nothing else can observe it.
    """

    __slots__ = ("_optional", "_tenancy")

    #: Where the reset token rides. On `request.state`, never in a dict on the
    #: middleware: an instance-level map keyed by request would be unbounded
    #: in-process memory that grows by one entry for every request whose `after`
    #: hook never ran. The state object dies with the request.
    _TOKEN_KEY = "_wreath_tenant_token"

    def __init__(self, tenancy: Tenancy, *, optional: bool = False) -> None:
        self._tenancy = tenancy
        #: Whether a request that names no tenant may proceed unbound. Off by
        #: default: an unbound request against a tenant application is one that
        #: will either refuse deep in a handler or, worse, not refuse at all.
        #: On for a deployment whose health and login routes are shared.
        self._optional = optional

    async def before(self, request: Any) -> None:
        try:
            tenant = self._tenancy.resolve_request(request)
        except UnknownTenant:
            if self._optional:
                return None
            raise
        request.state.__setattr__(self._TOKEN_KEY, _CURRENT.set(tenant))
        request.state.__setattr__("tenant", tenant)
        return None

    async def after(self, request: Any, response: Any) -> Any:
        token = request.state.get(self._TOKEN_KEY)
        if token is not None:
            _CURRENT.reset(token)
        return response


# Three steps that are each wrong on their own: a schema with no role is
# unreachable, a role with no grants is useless, and a grant set maintained by
# hand drifts the first time somebody adds a table. `ALTER DEFAULT PRIVILEGES`
# is what stops the third -- without it every migration that adds a table adds
# one the tenant cannot read, found by a 500 rather than by the deploy.


@dataclass(frozen=True, slots=True)
class IsolationReport:
    """What a tenant role can actually reach, read out of the catalog.

    From `information_schema` rather than from the code that intended it:
    isolation you cannot audit is isolation you are hoping for.
    """

    tenant: str
    role: str
    readable_schemas: tuple[str, ...]
    writable_schemas: tuple[str, ...]
    #: Tables in the tenant's own schema the role holds no privilege on. Not
    #: empty means a migration added a table and the default privileges did not
    #: cover it, which is a 500 waiting for the first read.
    ungranted_tables: tuple[str, ...]
    inherits_by_membership: tuple[str, ...]

    def crosses_into(self, other: str) -> bool:
        return other in self.readable_schemas or other in self.writable_schemas


def _tenant_ddl(schema: str, role: str, central: str, login_role: str) -> tuple[str, ...]:
    """Every statement that makes one tenant reachable and no other.

    Written out as one list because the set *is* the security argument, and a
    reader has to be able to check it without running it. Identifiers are
    validated by `Tenant.__post_init__` and quoted here; nothing in this
    function interpolates anything a request supplied.
    """
    s, r, c, login = _q(schema), _q(role), _q(central), _q(login_role)
    return (
        f"CREATE SCHEMA IF NOT EXISTS {s}",
        # NOLOGIN: a tenant role is a privilege bundle the pool switches into,
        # never an account anything connects as.
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
        f"{_lit(role)}) THEN EXECUTE format('CREATE ROLE %I NOLOGIN', {_lit(role)}); "
        f"END IF; END $$",
        # PUBLIC holds CREATE on `public` and USAGE broadly by default; a tenant
        # schema that leaves it is a tenant schema every role can read.
        f"REVOKE ALL ON SCHEMA {s} FROM PUBLIC",
        f"GRANT USAGE ON SCHEMA {s} TO {r}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {s} TO {r}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {s} TO {r}",
        # The half that stops the grants drifting: everything a later migration
        # creates here is granted as it is created.
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {r}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT USAGE, SELECT ON SEQUENCES TO {r}",
        # Central: readable by every tenant, writable by none. "Immutable" that
        # a tenant can UPDATE is a word rather than a property.
        f"GRANT USAGE ON SCHEMA {c} TO {r}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {c} TO {r}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {c} GRANT SELECT ON TABLES TO {r}",
        # Membership is what lets the pool `SET LOCAL ROLE` at all.
        # It is also what would hand the login role every tenant's privileges
        # ambiently -- if that role inherits. `WITH INHERIT FALSE` says so per
        # grant and needs PostgreSQL 16; requiring it here would make this
        # module refuse a server that is otherwise fine. So the property is
        # asserted about the *role* instead, by `verify_isolation`, which
        # refuses an inheriting login role at startup and names the `ALTER ROLE`
        # that fixes it. Same guarantee, one version older, and checked rather
        # than assumed.
        f"GRANT {r} TO {login}",
    )


def _q(identifier: str) -> str:
    """Quote an identifier that has already been validated."""
    return quote_identifier(identifier)


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def provision_tenant(
    connection: Any,
    *,
    key: str,
    schema: str | None = None,
    role: str | None = None,
    central: str = "central",
    login_role: str,
    status: TenantStatus = TenantStatus.PROVISIONING,
) -> Tenant:
    """Create one tenant's schema, role and grants, idempotently.

    Idempotent throughout, so a run stopped by a lock or a deploy is finished by
    running it again rather than by reasoning about which of eleven statements
    completed -- the property `migrations.apply_fleet` already holds for the
    fleet, for the same reason.

    Returns the tenant in `PROVISIONING`: the schema exists and the artifact has
    not been applied, and those are different facts. Move it to `ACTIVE` once
    `migrations.apply` has run against it.
    """
    resolved_role = role or f"tenant_{key}"
    tenant = Tenant(key=key, schema=schema or f"tenant_{key}", role=resolved_role, status=status)
    validate_identifier(central, "central schema")
    validate_identifier(login_role, "login role")
    for statement in _tenant_ddl(tenant.schema, resolved_role, central, login_role):
        await connection.execute(statement)
    return tenant


async def deprovision_tenant(
    connection: Any,
    tenant: Tenant,
    *,
    force: bool = False,
) -> None:
    """Drop a tenant's schema and role. Irreversible, so it asks first.

    Refuses while the schema holds any table unless `force`, following
    `privacy.erase`: an irreversible act recomputes what it is about to destroy
    and refuses when that does not match what the caller expected.
    """
    if not force:
        rows = await connection.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = $1::text LIMIT 1", tenant.schema
        )
        if rows:
            raise TenancyError(
                f"schema {tenant.schema!r} is not empty; deprovisioning is irreversible. "
                "Pass force=True once you have exported or erased what is there."
            )
    await connection.execute(f"DROP SCHEMA IF EXISTS {_q(tenant.schema)} CASCADE")
    if tenant.role is not None:
        # `DROP OWNED BY` first, and it is not about ownership: the default
        # privileges `provision_tenant` set in the central schema are catalog
        # rows that *depend* on this role, so `DROP ROLE` refuses while they
        # exist with a message about "some objects" that names none of them.
        await connection.execute(f"DROP OWNED BY {_q(tenant.role)} CASCADE")
        await connection.execute(f"DROP ROLE IF EXISTS {_q(tenant.role)}")


async def isolation_report(connection: Any, tenant: Tenant) -> IsolationReport:
    """What this tenant's role can reach, according to the catalog.

    Takes no `central=`: the point is to report every schema the role can reach,
    so naming the one it is *expected* to reach would let the report agree with
    the expectation instead of with the database.
    """
    role = tenant.role or tenant.schema
    readable = await connection.fetch(
        "SELECT DISTINCT table_schema FROM information_schema.role_table_grants "
        "WHERE grantee = $1::text AND privilege_type = 'SELECT' ORDER BY table_schema",
        role,
    )
    writable = await connection.fetch(
        "SELECT DISTINCT table_schema FROM information_schema.role_table_grants "
        "WHERE grantee = $1::text AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE') "
        "ORDER BY table_schema",
        role,
    )
    ungranted = await connection.fetch(
        "SELECT t.tablename FROM pg_tables t WHERE t.schemaname = $1::text AND NOT EXISTS ("
        "  SELECT 1 FROM information_schema.role_table_grants g"
        "  WHERE g.grantee = $2::text AND g.table_schema = t.schemaname"
        "    AND g.table_name = t.tablename) ORDER BY t.tablename",
        tenant.schema,
        role,
    )
    members = await connection.fetch(
        "SELECT r.rolname FROM pg_auth_members m "
        "JOIN pg_roles r ON r.oid = m.member "
        "JOIN pg_roles g ON g.oid = m.roleid WHERE g.rolname = $1::text ORDER BY r.rolname",
        role,
    )
    return IsolationReport(
        tenant=tenant.key,
        role=role,
        readable_schemas=tuple(_first(row) for row in readable),
        writable_schemas=tuple(_first(row) for row in writable),
        ungranted_tables=tuple(_first(row) for row in ungranted),
        inherits_by_membership=tuple(_first(row) for row in members),
    )


def _text(value: Any) -> str:
    """One catalog text value as a `str`.

    A raw `fetch` hands back the wire bytes -- these are catalog reads with no
    model behind them, so nothing has decoded them. `str(b"public")` is
    `"b'public'"`, which compares equal to nothing and reads like a working
    value in a report, so the decode happens here rather than at each call site.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _first(row: Any) -> str:
    """One column out of a driver row, whichever row type the driver returned."""
    if isinstance(row, Mapping):
        return _text(next(iter(row.values())))
    return _text(row[0])


async def verify_isolation(connection: Any, *, schemas: Iterable[str] = ()) -> None:
    """Refuse a connection whose role makes the boundary decorative.

    Both checks are about privileges that cannot be revoked from their holder,
    so no grant set can compensate for either. At startup, naming the role --
    the one moment somebody can act on it. A deployment that discovers this in
    production discovers it as a breach.
    """
    rows = await connection.fetch(
        "SELECT current_user AS name, r.rolsuper AS super, r.rolinherit AS inherits "
        "FROM pg_roles r WHERE r.rolname = current_user"
    )
    row = rows[0]
    name = _text(row["name"] if isinstance(row, Mapping) else row[0])
    is_super = row["super"] if isinstance(row, Mapping) else row[1]
    inherits = row["inherits"] if isinstance(row, Mapping) else row[2]
    # Superuser first, deliberately: a superuser also inherits, and reporting the
    # membership problem to somebody whose role bypasses every GRANT anyway
    # would name the smaller of the two findings.
    if is_super:
        raise TenancyError(
            f"the application connects as {name!r}, which is a superuser; a superuser "
            "bypasses every GRANT, so tenant isolation would be decorative. Connect as "
            "an unprivileged role and give it membership of the tenant roles."
        )
    if inherits:
        raise TenancyError(
            f"the application connects as {name!r}, which inherits the privileges of "
            "every role it is a member of -- including every tenant role, ambiently, "
            "with no SET ROLE. That makes RESET ROLE an escape hatch rather than a dead "
            f"end. Run: ALTER ROLE {name} NOINHERIT"
        )
    names = tuple(schemas)
    if not names:
        return
    # One placeholder per value rather than `= ANY($1)`: wreath's driver infers
    # one scalar type per placeholder and refuses a list, because `[]` names no
    # element type. Bounded by however many schemas the caller passed.
    # `::text` on each: `nspname` is `name`, so an uncast placeholder is
    # inferred as `name` and the driver cannot encode one (SQL002).
    placeholders = ", ".join(f"${index}::text" for index in range(1, len(names) + 1))
    owned = await connection.fetch(
        "SELECT nspname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner "
        f"WHERE r.rolname = current_user AND nspname IN ({placeholders})",
        *names,
    )
    if owned:
        raise TenancyError(
            f"the application role {name!r} owns tenant schemas "
            f"({', '.join(_first(item) for item in owned)}); an owner's privileges are "
            "implicit and cannot be revoked from itself. Own them with a migration role "
            "the request path never connects as."
        )


#: A schema-qualified reference to a tenant schema, written out in application
#: source. The grants make it fail closed; this makes it fail *early*, where the
#: person who wrote it is still looking at it.
_SCHEMA_LITERAL = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(tenant_[a-z0-9_$]+)\.", re.IGNORECASE)


def find_schema_literals(source: str) -> tuple[str, ...]:
    """Every tenant-schema literal in one piece of application source.

    One pass of a compiled pattern over the text rather than a parse: the input
    is a file, the answer is a list of names, and a SQL parser here would be a
    second dialect to keep correct for no extra finding.
    """
    return tuple(match.group(1) for match in _SCHEMA_LITERAL.finditer(source))
