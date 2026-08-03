"""Organisations, memberships, roles within them, and invitations.

Tenancy at the **identity** layer. `wreath.orm.TenantContext` is the other one —
it binds a schema and a role transaction-locally, which is tenancy at the *data*
layer — and the two are deliberately separate: a user may belong to two
organisations with different roles while every one of their requests reads the
same schema, and a single-tenant deployment may still want organisations.

Everything enterprise hangs off this. SCIM provisions into it, SAML asserts into
it, an audit record names the organisation a write happened in, and an admin
screen redacts a field per role within it. Building any of those first would
have produced a second membership model, so this lands first and they adapt onto
it.

**This is not an ORM model you must adopt.** `wreath.users` already made the
right call with a pluggable `UserStore`, and the same reasoning applies harder
here: organisations are the table an existing application is most likely to
already have, under a different name, with columns nobody wants to migrate.
`OrganizationStore` is the seam; `InMemoryOrganizationStore` is the reference
implementation and what the tests run against.

    store = InMemoryOrganizationStore(roles={"admin", "member", "billing"})
    await store.add_member("acme", "alice", roles={"admin"})

    app.configure_auth(backend, CedarAuthorizer(
        engine=CedarPolicies(POLICY),
        organizations=Memberships(store),
    ))

A policy then reads them as sets, the same shape `context.flags` and
`context.regions` already use:

    permit (principal, action == Action::"read", resource)
    when { context.organizations.contains(resource.owner) };

    permit (principal, action == Action::"invite", resource)
    when { context.org_roles.contains("acme:admin") };

Roles are namespaced by organisation (`"acme:admin"`) because a bare role name
cannot say *where* it applies, and an admin of one tenant is not an admin of
another. That is the whole cross-tenant leak in one sentence, so the spelling
that makes it impossible is the one that ships.

## Resolution is synchronous, on purpose

`Memberships` — the authorizer's seam — is **not** async, and that is a design
decision rather than a limitation. Authorization runs on the request path, which
the request-boundary baseline expects to stay cheap; a database round trip per
decision is the single easiest way to make Cedar the slowest thing in a request.
The membership set a decision needs is already known when the session was
established, so the shape that works is: load once where identity is
established, resolve synchronously where policy is evaluated.

`Memberships` therefore reads from a synchronous source — the in-memory store
directly, or a snapshot an authentication backend loaded. `load_into` is the
helper for the second, and `wreath.organizations` never opens a connection on
the authorization path.

## SCIM provisions into this, and adds nothing to it

`scim_router` is the RFC 7643/7644 adapter, and it is an adapter in the strict
sense: it has no user table, no group table and no membership table of its own.

    app.include_router(scim_router(app, users=users, organizations=store,
                                   organization="acme"))

A SCIM `User` is a `wreath.users` record, a SCIM `Group` is a **role** in this
store's declared vocabulary, and a SCIM group membership is this module's
`Membership`. That is what makes a directory de-provisioning take effect: the
row a directory removes is the row `context.org_roles` reads. It is also why a
group cannot be created over SCIM — the role vocabulary is configuration a Cedar
policy names by string, not data — and why every SCIM route is authorized by the
application's own Cedar policies rather than by anything SCIM decides.

Listing an organisation's members is the one question the authorization path
never asks, so it is a separate seam: `MemberDirectory`, which
`InMemoryOrganizationStore` implements and `scim_router` requires.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ._scim import scim_router

__all__ = [
    "Invitation",
    "InMemoryOrganizationStore",
    "Membership",
    "MemberDirectory",
    "Memberships",
    "Organization",
    "OrganizationStore",
    "active_organization",
    "load_into",
    "scim_router",
]

#: Where a loaded membership snapshot lives for the rest of a request.
_SNAPSHOT_SLOT = "_organization_memberships"

#: Where the active organisation lives on the session.
ACTIVE_ORGANIZATION_KEY = "org"


@dataclass(frozen=True, slots=True)
class Organization:
    """One tenant.

    Args:
        id: stable identifier, used verbatim in `context.organizations` and in
            the `"<org>:<role>"` spelling, so it must not contain a colon --
            checked, because a colon here silently forges a role in another
            organisation.
        name: display name
        metadata: anything the application wants to carry along
    """

    id: str
    name: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("an organization id must be a non-empty string")
        if ":" in self.id:
            raise ValueError(
                f"an organization id must not contain ':' (got {self.id!r}); "
                "roles are namespaced as '<org>:<role>' and a colon in the id "
                "would let one organization's id spell another's role"
            )


@dataclass(frozen=True, slots=True)
class Membership:
    """A user's place in one organisation.

    Args:
        organization: the organisation id
        user_id: the member
        roles: role names *within* that organisation, unqualified. The qualified
            `"<org>:<role>"` spelling is produced on the way into policy context
            and is never stored, so there is one place the namespacing rule
            lives.
    """

    organization: str
    user_id: str
    roles: frozenset[str] = frozenset()

    def qualified_roles(self) -> frozenset[str]:
        """This membership's roles, namespaced by organisation."""
        return frozenset(f"{self.organization}:{role}" for role in self.roles)


@dataclass(frozen=True, slots=True)
class Invitation:
    """An offer of membership that survives the invitee having no account yet.

    That is the whole reason this is a record rather than a call: the common
    case is inviting an email address belonging to nobody, and an invitation
    keyed on a user id cannot express it. The invitee is an **email address**
    until acceptance, and acceptance is what binds it to a user.

    Args:
        token: the secret in the invitation link. Compared in constant time on
            acceptance; single-use.
        organization: where the invitation is to
        email: who was invited, before they exist
        roles: what they get on acceptance
        expires_at: epoch seconds; an expired invitation is refused
        accepted_by: the user id that accepted, or `None`
    """

    token: str
    organization: str
    email: str
    roles: frozenset[str] = frozenset()
    expires_at: float | None = None
    accepted_by: str | None = None

    def expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


@runtime_checkable
class OrganizationStore(Protocol):
    """Where organisations, memberships and invitations live.

    Async, because a real one is a database. The authorization path does not use
    this protocol -- see the module docstring -- so nothing here is on the
    request's hot path.
    """

    async def organization(self, org_id: str) -> Organization | None:
        """The organisation, or `None`."""
        ...

    async def memberships(self, user_id: str) -> tuple[Membership, ...]:
        """Every membership `user_id` holds, in any organisation."""
        ...

    def roles(self) -> frozenset[str]:
        """The declared role vocabulary, unqualified.

        Synchronous and configuration rather than data: roles are a fixed
        vocabulary an application declares, which is what lets a policy naming
        `"acme:admni"` be refused at startup instead of denying forever.
        """
        ...


@runtime_checkable
class MemberDirectory(Protocol):
    """The other direction: who is *in* an organisation.

    Deliberately separate from `OrganizationStore`, because the two questions
    have opposite cost profiles and opposite callers. Authorization asks "what
    does this user hold", once per session, and never needs a list. A directory
    -- SCIM's `GET /Users`, an admin screen's member table -- asks "who is in
    this tenant", and cannot be answered by the first question at all.

    A store that only ever backs authorization does not have to implement this,
    and `scim_router` refuses at build time rather than discovering it missing
    on the first request.
    """

    async def members(self, org_id: str) -> tuple[Membership, ...]:
        """Every membership in `org_id`."""
        ...


class InMemoryOrganizationStore:
    """The reference `OrganizationStore`, and what the tests run against.

    Args:
        roles: the declared role vocabulary. Required and non-empty: an
            application with no declared roles cannot have a policy naming one
            validated, and silently skipping that check is the failure this
            whole design is trying to avoid.
        organizations: optional initial organisations
    """

    __slots__ = ("_invitations", "_memberships", "_organizations", "_roles")

    def __init__(
        self,
        *,
        roles: Iterable[str],
        organizations: Iterable[Organization] = (),
    ) -> None:
        self._roles = frozenset(str(role) for role in roles)
        if not self._roles:
            raise ValueError(
                "InMemoryOrganizationStore requires a non-empty role vocabulary; "
                "without one a policy naming a role cannot be checked at startup"
            )
        self._organizations: dict[str, Organization] = {org.id: org for org in organizations}
        self._memberships: dict[str, dict[str, frozenset[str]]] = {}
        self._invitations: dict[str, Invitation] = {}

    def roles(self) -> frozenset[str]:
        """The declared role vocabulary."""
        return self._roles

    def _check_roles(self, roles: frozenset[str]) -> None:
        unknown = sorted(roles - self._roles)
        if unknown:
            raise ValueError(
                f"unknown role(s) {', '.join(unknown)}; declared roles are "
                f"{', '.join(sorted(self._roles))}"
            )

    async def organization(self, org_id: str) -> Organization | None:
        return self._organizations.get(org_id)

    async def create(self, organization: Organization) -> Organization:
        """Add an organisation; refuses to replace an existing id."""
        if organization.id in self._organizations:
            raise ValueError(f"organization {organization.id!r} already exists")
        self._organizations[organization.id] = organization
        return organization

    async def memberships(self, user_id: str) -> tuple[Membership, ...]:
        return self.memberships_for(user_id)

    def memberships_for(self, user_id: str) -> tuple[Membership, ...]:
        """The synchronous half, which `Memberships` reads on the request path.

        Present here because this store *is* in memory, so the query is a dict
        lookup and there is nothing to await. A database-backed store offers no
        such method and participates through `load_into` instead -- which is why
        `Memberships` probes for this rather than requiring it.
        """
        return tuple(
            Membership(organization=org, user_id=user_id, roles=roles)
            for org, roles in sorted(self._memberships.get(user_id, {}).items())
        )

    async def members(self, org_id: str) -> tuple[Membership, ...]:
        """Every membership in `org_id`, in user id order -- the `MemberDirectory` half.

        Scans the membership table rather than keeping a reverse index. The
        table is `user -> org -> roles` because that is the shape the
        authorization path reads, and a second index maintained beside it is
        one more thing that can disagree with the first; a store this size can
        afford the scan, and a database-backed one answers with a `WHERE`
        clause instead of either.
        """
        return tuple(
            Membership(organization=org_id, user_id=user_id, roles=held[org_id])
            for user_id, held in sorted(self._memberships.items())
            if org_id in held
        )

    async def add_member(
        self, org_id: str, user_id: str, *, roles: Iterable[str] = ()
    ) -> Membership:
        """Place `user_id` in `org_id` with `roles`, replacing any prior roles."""
        if org_id not in self._organizations:
            await self.create(Organization(id=org_id))
        wanted = frozenset(str(role) for role in roles)
        self._check_roles(wanted)
        self._memberships.setdefault(user_id, {})[org_id] = wanted
        return Membership(organization=org_id, user_id=user_id, roles=wanted)

    async def remove_member(self, org_id: str, user_id: str) -> bool:
        """Remove a membership. Returns whether there was one."""
        held = self._memberships.get(user_id)
        if not held or org_id not in held:
            return False
        del held[org_id]
        return True

    async def invite(
        self,
        org_id: str,
        email: str,
        *,
        roles: Iterable[str] = (),
        ttl: float | None = None,
        now: float | None = None,
    ) -> Invitation:
        """Offer membership to an email address that may belong to nobody yet."""
        wanted = frozenset(str(role) for role in roles)
        self._check_roles(wanted)
        moment = time.time() if now is None else now
        invitation = Invitation(
            token=secrets.token_urlsafe(32),
            organization=org_id,
            email=email,
            roles=wanted,
            expires_at=None if ttl is None else moment + ttl,
        )
        self._invitations[invitation.token] = invitation
        return invitation

    async def accept(self, token: str, user_id: str, *, now: float | None = None) -> Membership:
        """Bind an invitation to a user, once.

        Raises:
            ValueError: the token is unknown, already used, or expired. One
                message per branch, distinct, so a test asserting the refusal
                cannot pass on whichever branch happened to fire -- every
                refusal here would otherwise mention the token.
        """
        moment = time.time() if now is None else now
        invitation = None
        for held in self._invitations.values():
            if secrets.compare_digest(held.token, token):
                invitation = held
                break
        if invitation is None:
            raise ValueError("no such invitation")
        if invitation.accepted_by is not None:
            raise ValueError("invitation has already been accepted")
        if invitation.expired(moment):
            raise ValueError("invitation has expired")
        self._invitations[invitation.token] = Invitation(
            token=invitation.token,
            organization=invitation.organization,
            email=invitation.email,
            roles=invitation.roles,
            expires_at=invitation.expires_at,
            accepted_by=user_id,
        )
        return await self.add_member(invitation.organization, user_id, roles=invitation.roles)

    async def invitations(self, org_id: str) -> tuple[Invitation, ...]:
        """Every invitation issued for `org_id`, accepted or not."""
        return tuple(
            invitation
            for invitation in self._invitations.values()
            if invitation.organization == org_id
        )


class Memberships:
    """The authorizer's seam: this caller's organisations and roles, synchronously.

    Two sources, checked in order:

    1. a **snapshot** placed on the request by `load_into`, which is how a
       database-backed store participates without a query on the authorization
       path;
    2. a synchronous `memberships_for(user_id)` on the source, which
       `InMemoryOrganizationStore` gains through this class.

    A caller with neither resolves to nothing, and nothing denies. There is
    deliberately no third source and no fallback to an async query: a fallback
    that sometimes queried would make the authorization path's cost depend on
    whether a snapshot happened to be loaded, which is the kind of variance that
    shows up as a p99 nobody can explain.

    Args:
        store: the organisation store. Only `roles()` is used on the request
            path, for the startup vocabulary; membership data comes from the
            snapshot.
    """

    __slots__ = ("_store",)

    def __init__(self, store: Any) -> None:
        self._store = store

    def names(self) -> frozenset[str]:
        """The declared role vocabulary, qualified and unqualified.

        The enumeration half of the provider surface, the same shape
        `FeatureFlags.names()` offers, and read by `CedarAuthorizer` at startup
        to refuse a policy naming a role nobody declared.

        Unqualified names are included because a policy may test
        `context.org_roles.contains("admin")` against the *active* organisation's
        unqualified roles, which this provider also supplies. Organisation ids
        are **data**, not vocabulary, so the qualified form cannot be enumerated
        here -- and `context.organizations` is validated for that reason.
        """
        roles = getattr(self._store, "roles", None)
        return frozenset() if not callable(roles) else frozenset(roles())

    def for_request(self, request: Any) -> tuple[Membership, ...]:
        """This caller's memberships, from the snapshot or a synchronous source."""
        snapshot = request.state.get(_SNAPSHOT_SLOT)
        if snapshot is not None:
            return tuple(snapshot)
        identity = getattr(request, "identity", None)
        if identity is None:
            return ()
        source = getattr(self._store, "memberships_for", None)
        if not callable(source):
            return ()
        return tuple(source(identity.id))


def load_into(request: Any, memberships: Iterable[Membership]) -> None:
    """Place a membership snapshot on the request, for the authorizer to read.

    Called from wherever identity is established -- a session backend that has
    just read the user, a token backend that has just verified one. That is the
    query that was going to happen anyway, so authorization adds none.
    """
    request.state.__setattr__(_SNAPSHOT_SLOT, tuple(memberships))


def active_organization(request: Any) -> str | None:
    """Which organisation this request is acting in, or `None`.

    Read from the session under `ACTIVE_ORGANIZATION_KEY`, then from the
    identity's limits. A user in two organisations is acting in one of them at a
    time, and every answer that depends on *which* -- the unqualified roles in
    `context.org_roles`, an admin screen's scope, an audit record's tenant --
    reads it here rather than deriving it separately.
    """
    session = getattr(request, "session", None)
    if session is not None:
        chosen = session.get(ACTIVE_ORGANIZATION_KEY)
        if chosen:
            return str(chosen)
    identity = getattr(request, "identity", None)
    limits = getattr(identity, "limits", None)
    active = getattr(limits, "active_organization", None)
    return None if active is None else str(active)
