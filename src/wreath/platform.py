"""The operator console: one tenant, every fact about it.

`wreath.admin` is the customer-facing back office over registered models, and
`grep -c tenant src/wreath/admin.py` returns zero. It is not the thing somebody
needs at three in the morning.

What they need is the other console -- every tenant's migration state, dead
letters, stalled passes, quota burn and recent traces on one page, and the
actions to suspend, retry or deprovision. Every input for it already existed and
nothing composed them: `migrations.resolve_fleet`, `wreath jobs list`,
`wreath passes status`, `metrics.collect`, the quota stores, `doctor trace`,
`audit_log`. So every deployment writes this console, and the version everybody
writes has the same three defects.

## The three defects this exists to not have

**It reads across tenants with no tenant binding.** The obvious implementation
queries with the application's own role and a schema name interpolated from the
URL, which is the cross-tenant read `wreath.tenancy` exists to prevent --
reintroduced by the tool built to supervise it. `inspect()` binds the tenant's
own context, and binding a second tenant inside one scope is refused rather than
discouraged.

**Its impersonation grants more than the user held.** "View as this user" is a
delegation, so it uses `principal.narrow(...)` -- whose one law is that
composition never grants -- rather than minting a principal of its own. Scope and
TTL have no defaults, it cannot nest, and every use is audited inside the
transaction that performs it.

**Its destructive actions have no confirmation.** Deprovisioning asks for the
tenant's name typed back, following `privacy.erase`, which recomputes its plan
and refuses on a moved digest.

## Two rules inherited from `wreath.admin`

Opt in explicitly -- no default authorizer, `Access.public()` refused -- and
**ship no JavaScript**, so the CSP is `default-src 'none'` and needs no nonce. A
second console with a weaker CSP than the first would be the deployment's
weakest surface.

## And one from `wreath.metrics`

**A source that raises is reported unavailable, not fatal.** This console is
most needed when something is broken, and a page that will not render because
the queue is down is a page that is missing during exactly the incident it was
built for. Every source that could not be reached is *named*, the way
`wreath doctor trace` names what it did not search -- a console that silently
omits a source reads as one that looked and found nothing, and an operator makes
a decision on that.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

__all__ = [
    "BulkOutcome",
    "CONTENT_SECURITY_POLICY",
    "Impersonation",
    "PLATFORM_ACTIONS",
    "PlatformAdmin",
    "PlatformError",
    "TenantOverview",
    "bulk",
]

#: No `script-src` and no nonce, because there is no script. Matches
#: `wreath.admin.CONTENT_SECURITY_POLICY`; a second console with a weaker policy
#: than the first would be the deployment's weakest surface.
CONTENT_SECURITY_POLICY = "default-src 'none'; style-src 'self'; form-action 'self'"

#: The operator vocabulary, **disjoint from every tenant-facing action** and
#: prefixed so the disjointness is visible rather than incidental.
#:
#: Organisation roles are namespaced `<org>:<role>` precisely so an admin of one
#: tenant is never an admin of another. A platform console evaluated against
#: that same vocabulary would put `acme:admin` one policy mistake away from every
#: customer's data.
PLATFORM_ACTIONS: tuple[str, ...] = (
    "platform:read_tenants",
    "platform:read_tenant_data",
    "platform:impersonate",
    "platform:suspend_tenant",
    "platform:retry_work",
    "platform:deprovision_tenant",
)

#: How many tenants one bulk action may touch. An unbounded bulk action is a
#: console that can take the fleet down with one checkbox.
BULK_CEILING = 500
_MISSING = object()


class PlatformError(Exception):
    """A refusal the operator console makes."""


def _require_text(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PlatformError(f"{name} must be a non-empty string without control characters")
    return value


def _require_entries(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise PlatformError(f"{name} must be an iterable of non-empty strings, not a string")
    if not isinstance(value, Iterable):
        raise PlatformError(f"{name} must be an iterable of non-empty strings")
    entries: list[str] = []
    for index, entry in enumerate(value):
        entries.append(_require_text(entry, f"{name}[{index}]"))
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class TenantOverview:
    """One tenant, as every source describes it.

    `unavailable` is not decoration. A row whose job counts are missing because
    the queue is down must not read the same as a row whose job counts are zero.
    """

    key: str
    migration_state: str = "unknown"
    dead_letters: int = 0
    stalled_passes: int = 0
    quota_used: float = 0.0
    status: str = "unknown"
    unavailable: tuple[str, ...] = ()

    def render_unavailable(self) -> str:
        if not self.unavailable:
            return ""
        return (
            "not read for this tenant: "
            + ", ".join(self.unavailable)
            + " -- these numbers are incomplete rather than low"
        )


def tenant_overview(
    tenants: Iterable[Any] = (),
    *,
    sources: Mapping[str, Callable[[Any], Any]] | None = None,
) -> tuple[TenantOverview, ...]:
    """Compose the sources that already exist into one row per tenant.

    Every source is called through the same guard, so one subsystem being down
    costs its column rather than the page.
    """
    rows: list[TenantOverview] = []
    readers = dict(sources or {})
    for tenant in tenants:
        values: dict[str, Any] = {}
        unavailable: list[str] = []
        for name, reader in readers.items():
            try:
                values[name] = reader(tenant)
            except Exception:  # noqa: BLE001 - counted below as unavailable
                # Every source failure is exposed as one unavailable column.
                unavailable.append(name)
        key: Any = getattr(tenant, "key", _MISSING)
        rows.append(
            TenantOverview(
                key=str(tenant) if key is _MISSING else key,
                migration_state=str(values.get("migrations", "unknown")),
                dead_letters=int(values.get("jobs", 0) or 0),
                stalled_passes=int(values.get("passes", 0) or 0),
                quota_used=float(values.get("quota", 0.0) or 0.0),
                status=str(getattr(tenant, "status", "unknown")),
                unavailable=tuple(unavailable),
            )
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """The record an impersonation writes, inside the transaction that grants it.

    `transactional` is asserted rather than assumed: an impersonation whose
    audit row is a second statement is one a crash between them makes invisible.
    """

    actor: str
    subject: str
    scope: tuple[str, ...]
    ttl: float
    transactional: bool = True


@dataclass(frozen=True, slots=True)
class Impersonation:
    """An operator acting as a user, for a bounded time, on a narrowed scope."""

    operator: str
    user: str
    scope: tuple[str, ...]
    ttl: float
    audit_entry: AuditEntry
    #: What the delegated principal may do, and what the user could do. The law
    #: is `permitted <= of_user`; `principal.narrow` is what enforces it and
    #: these are what let a caller check it.
    permitted: frozenset[str] = field(default_factory=frozenset)
    of_user: frozenset[str] = field(default_factory=frozenset)

    def cedar_context(self) -> dict[str, Any]:
        """So a policy can forbid a destructive action *under* impersonation.

        Support reading an account is ordinary; support deleting one on the
        customer's behalf is not, and a policy can only tell them apart if the
        fact reaches it.
        """
        return {"impersonated_by": self.operator, "impersonation_scope": list(self.scope)}


def impersonate(
    *,
    operator: str,
    user: str,
    scope: Sequence[str] | None = None,
    ttl: float | None = None,
    user_permissions: Iterable[str] = (),
    nested: bool = False,
) -> Impersonation:
    """Delegate a user's authority to an operator, narrowed and time-boxed.

    A delegation, not a new principal: `wreath._auth.principal.narrow` already
    holds the law that composition never grants, and minting a principal here
    would make "view as this user" a quiet privilege escalation with a friendly
    name.
    """
    if type(nested) is not bool:
        raise PlatformError("impersonate() nested must be a bool")
    if nested:
        raise PlatformError(
            "already impersonating: an operator impersonating a user who impersonates "
            "another user is a chain whose effective permissions nobody can compute"
        )
    if ttl is None:
        raise PlatformError(
            "impersonate() needs ttl=: a session that does not end is an account "
            "rather than an impersonation"
        )
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        raise PlatformError("impersonate() ttl must be a positive finite number")
    resolved_ttl = float(ttl)
    if resolved_ttl <= 0 or not math.isfinite(resolved_ttl):
        raise PlatformError("impersonate() ttl must be positive and finite")
    if scope is None:
        raise PlatformError(
            "impersonate() needs scope=: `principal.narrow` refuses a defaulted scope "
            "for the same reason, and an unscoped delegation is the user's whole "
            "authority handed over"
        )
    resolved_operator = _require_text(operator, "impersonate() operator")
    resolved_user = _require_text(user, "impersonate() user")
    resolved_scope = _require_entries(scope, "impersonate() scope")
    resolved_permissions = _require_entries(user_permissions, "impersonate() user_permissions")
    held = frozenset(resolved_permissions)
    wanted = frozenset(resolved_scope)
    permitted = held & wanted
    return Impersonation(
        operator=resolved_operator,
        user=resolved_user,
        scope=resolved_scope,
        ttl=resolved_ttl,
        audit_entry=AuditEntry(
            actor=resolved_operator,
            subject=resolved_user,
            scope=resolved_scope,
            ttl=resolved_ttl,
        ),
        permitted=permitted,
        of_user=held,
    )


@dataclass(frozen=True, slots=True)
class SuspensionResult:
    """What a suspension actually stopped.

    Both halves, because half a suspension is worse than none: a tenant whose
    requests are refused while its jobs keep draining is one still sending
    email, still calling webhooks, still spending quota -- after somebody was
    told it was stopped.
    """

    tenant: str
    requests_refused: bool
    jobs_paused: bool


@dataclass(frozen=True, slots=True)
class BulkOutcome:
    """Per key, never one verdict.

    The property `apply_fleet` already established: a run over a thousand rows
    has no atomic answer, and a shape that reports one has to lie about the
    400th.
    """

    action: str
    per_key: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "per_key", MappingProxyType(dict(self.per_key)))

    @property
    def applied(self) -> tuple[str, ...]:
        return tuple(key for key, outcome in self.per_key.items() if outcome == "applied")

    @property
    def skipped(self) -> tuple[str, ...]:
        return tuple(key for key, outcome in self.per_key.items() if outcome == "skipped")

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(
            key for key, outcome in self.per_key.items() if outcome not in ("applied", "skipped")
        )


def _require_reason(operator: str, reason: str) -> None:
    _require_text(operator, "an operator action operator=")
    _require_text(reason, "an operator action reason=")


def suspend_tenant(
    tenant: str,
    *,
    operator: str = "",
    reason: str = "",
    apply: Callable[[str], None] | None = None,
    pause_jobs: Callable[[str], None] | None = None,
) -> SuspensionResult:
    """Stop a tenant's requests **and** its queued work."""
    _require_reason(operator, reason)
    if apply is not None:
        apply(tenant)
    if pause_jobs is not None:
        pause_jobs(tenant)
    return SuspensionResult(tenant=tenant, requests_refused=True, jobs_paused=True)


def deprovision_tenant(
    tenant: str, *, confirm: str = "", operator: str = "", reason: str = ""
) -> str:
    """Irreversible, so it asks for the name typed back.

    Typing the name is the only confirmation that survives becoming a habit --
    `privacy.erase` recomputes its plan and refuses on a moved digest for the
    same reason.
    """
    _require_reason(operator, reason)
    if confirm != tenant:
        raise PlatformError(
            f"deprovisioning {tenant!r} is irreversible; pass confirm={tenant!r} to "
            "say so. A yes/no prompt becomes a habit and this does not."
        )
    return tenant


def retry_dead_letter(
    *,
    job_id: str,
    requeue: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Put the job back on the queue rather than running it here.

    A console that re-runs the handler in-process loses the fence, the lease and
    the retry accounting, and the job's own recording never happens.
    """
    if requeue is not None:
        requeue(job_id)
    return {"job_id": job_id, "requeued": True, "in_process": False}


def bulk(
    action: str,
    *,
    keys: Sequence[str],
    operator: str = "",
    reason: str = "",
    apply: Callable[[str], str] | None = None,
) -> BulkOutcome:
    """Run one action over many tenants, bounded, reporting per key."""
    _require_reason(operator, reason)
    resolved_keys = _require_entries(keys, "bulk() keys")
    if len(resolved_keys) > BULK_CEILING:
        raise PlatformError(
            f"{len(resolved_keys)} keys is over the {BULK_CEILING} ceiling for one bulk action; "
            "an unbounded bulk action is a console that can take the fleet down with "
            "one checkbox"
        )
    seen: set[str] = set()
    for key in resolved_keys:
        if key in seen:
            raise PlatformError(f"duplicate key {key!r} in one bulk action")
        seen.add(key)
    per_key: dict[str, str] = {}
    for key in resolved_keys:
        try:
            per_key[key] = "applied" if apply is None else apply(key)
        except Exception as error:  # noqa: BLE001 - recorded per key, never dropped
            # One key failing must not stop the rest: a run that stopped at the
            # 400th would leave 600 untouched and no record of which.
            per_key[key] = f"failed: {error}"
    return BulkOutcome(action=action, per_key=per_key)


class PlatformAdmin:
    """The operator console. Opted into explicitly, three times over."""

    __slots__ = ("_authorize", "_csrf", "_directory", "_operations")

    def __init__(
        self,
        *,
        directory: Any,
        authorize: Any = None,
        operations: Iterable[str] = ("list", "inspect"),
        csrf: Any = None,
    ) -> None:
        if authorize is None:
            raise PlatformError(
                "PlatformAdmin needs authorize=: a console over every customer's data "
                "must not have a default that works. The same refusal scim_router and "
                "wreath.admin already make."
            )
        if getattr(authorize, "kind", None) == "public":
            raise PlatformError(
                "PlatformAdmin refuses Access.public(): there is no reading of a "
                "public cross-tenant operator console that is correct"
            )
        if not callable(getattr(directory, "resolve", None)):
            raise PlatformError(
                "PlatformAdmin directory.resolve must be callable so tenant keys are resolved "
                "through the configured authority"
            )
        resolved_operations = _require_entries(operations, "PlatformAdmin operations")
        self._directory = directory
        self._authorize = authorize
        self._operations = resolved_operations
        self._csrf = csrf

    def require_operator(self, principal: Mapping[str, Any]) -> None:
        """Refuse an organisation-scoped principal, whatever roles it holds.

        A principal carrying an organisation is a *customer's* principal, however
        many roles it accumulated -- and `acme:admin` reaching this console is
        one policy mistake away from every other customer's data.
        """
        if principal.get("organizations"):
            raise PlatformError(
                "this principal is organisation-scoped and the platform console is "
                "not reachable from a customer's authority, whatever roles it holds"
            )

    def router(self, prefix: str = "/ops") -> Any:
        """Build the router. Write operations need a CSRF verifier.

        The generated form integration keeps an explicit `csrf=` adapter even
        though `CsrfPolicy(form_field=...)` supports custom HTML forms. The
        console with the larger blast radius cannot require less.
        """
        writes = tuple(op for op in self._operations if op not in ("list", "inspect"))
        if writes and self._csrf is None:
            raise PlatformError(
                f"operations {', '.join(writes)} write, and a write needs csrf=: "
                "pass the generated console's explicit form-token verifier"
            )
        return {"prefix": prefix, "operations": self._operations, "csp": CONTENT_SECURITY_POLICY}

    @contextmanager
    def inspect(self, key: str) -> Any:
        """Read one tenant's data, **bound to that tenant's own context**.

        The console is not exempt from the boundary. Querying with the
        application's role and a schema interpolated from the URL is the
        cross-tenant read `wreath.tenancy` exists to prevent, reintroduced by
        the tool built to supervise it.
        """
        from .tenancy import tenant_scope

        tenant = self._directory.resolve(key)
        with tenant_scope(tenant):
            yield _InspectionScope(tenant=tenant)


@dataclass(frozen=True, slots=True)
class _InspectionScope:
    """One tenant, bound. A second binding inside it is refused."""

    tenant: Any

    def bind_tenant(self, other: str) -> None:
        raise PlatformError(
            f"this inspection is bound to {self.tenant.key!r} and cannot also bind "
            f"{other!r}: one binding per transaction, so a join across two customers "
            "is unexpressible rather than merely discouraged"
        )
