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
`OrganizationStore` is the seam. `InMemoryOrganizationStore` is the bounded
development/reference implementation; `PostgresOrganizationStore` is the
durable implementation shared by API workers and process restarts.

    store = PostgresOrganizationStore(
        app.postgres("main"), roles={"admin", "member", "billing"}
    )
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
both shipped stores implement and `scim_router` requires.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, cast, runtime_checkable

from ._scim import scim_router
from .store import _Statements, rows_affected, sql_identifier


def _invitation_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()

__all__ = [
    "Invitation",
    "InMemoryOrganizationStore",
    "Membership",
    "MemberDirectory",
    "Memberships",
    "Organization",
    "OrganizationFederation",
    "OrganizationFederationError",
    "OrganizationStore",
    "PostgresOrganizationStore",
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


class OrganizationFederationError(LookupError):
    pass


class OrganizationFederation:
    __slots__ = ("installations", "organizations")

    def __init__(self, organizations: OrganizationStore, installations: Any) -> None:
        if not isinstance(organizations, OrganizationStore):
            raise TypeError("organization federation requires an OrganizationStore")
        if not callable(getattr(installations, "organization_for", None)):
            raise TypeError(
                "organization federation installation map must provide "
                "organization_for(provider, installation)"
            )
        self.organizations = organizations
        self.installations = installations

    async def resolve(self, external: Any, binding: Any) -> Any:
        provider = getattr(external, "provider", None)
        installation = getattr(external, "installation", None)
        if not provider or not installation:
            raise OrganizationFederationError(
                "organization federation requires provider and installation"
            )
        organization = await self.installations.organization_for(provider, installation)
        if not organization:
            raise OrganizationFederationError("chat installation has no organization mapping")
        user_id = getattr(getattr(binding, "identity", None), "id", None)
        if not isinstance(user_id, str) or not user_id:
            raise OrganizationFederationError(
                "organization federation requires a non-empty Wreath identity id"
            )
        memberships = await self.organizations.memberships(user_id)
        if not any(item.organization == organization for item in memberships):
            raise OrganizationFederationError(
                f"user {user_id!r} is not a member of organization {organization!r}"
            )
        tenant = getattr(binding, "tenant", None)
        if tenant is not None and tenant != organization:
            raise OrganizationFederationError(
                f"identity tenant {tenant!r} does not match organization {organization!r}"
            )
        return replace(binding, tenant=organization)


@dataclass(frozen=True, slots=True)
class Invitation:
    """An offer of membership that survives the invitee having no account yet.

    That is the whole reason this is a record rather than a call: the common
    case is inviting an email address belonging to nobody, and an invitation
    keyed on a user id cannot express it. The invitee is an **email address**
    until acceptance, and acceptance is what binds it to a user.

    Args:
        token: the secret in the invitation link. The in-memory store compares
            it in constant time; the PostgreSQL store indexes its fixed-width
            SHA-256 digest. Single-use in both.
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
        self._invitations: dict[bytes, Invitation] = {}

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
        self._invitations[_invitation_digest(invitation.token)] = invitation
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
        token_digest = _invitation_digest(token)
        invitation = self._invitations.get(token_digest)
        if invitation is None:
            raise ValueError("no such invitation")
        if invitation.accepted_by is not None:
            raise ValueError("invitation has already been accepted")
        if invitation.expired(moment):
            raise ValueError("invitation has expired")
        self._invitations[token_digest] = Invitation(
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


class _OrganizationStatements(_Statements):
    """Lazy prepared statements for the relational organisation store."""

    __slots__ = ("_database", "_defined", "_prefix", "_prepare_lock")

    _statement_owner = "organization store"

    def __init__(self, database: Any, prefix: str) -> None:
        self._database = database
        self._prefix = sql_identifier(prefix, what="prefix")
        self._init_statements()

    def _statement_name(self, name: str) -> str:
        return f"{self._prefix}_{name}"


class PostgresOrganizationStore:
    """Durable organisations, memberships, and invitations in PostgreSQL.

    Every instance is stateless apart from lazily prepared statements. Building
    a new instance after a process restart therefore reads the same rows, and
    two workers share invitation consumption rather than each accepting its own
    in-memory copy.

    Invitation acceptance is one PostgreSQL statement: it conditionally marks
    the invitation accepted and upserts the membership from the row it marked.
    The returned membership *is* proof that this caller consumed the token.
    Concurrent acceptance can return at most one membership.

    Tokens are looked up by a fixed-width SHA-256 digest. The raw token remains
    in the row so an operator can list or resend invitations, but it is never
    used in an index comparison and a presented token is never interpolated
    into SQL.

    Args:
        database: the application database shared by every API worker.
        roles: the non-empty, declared role vocabulary.
        table: base table name. `_membership` and `_invitation` are
            appended for the related tables; all three are plain identifiers.
        prefix: prepared-statement prefix. Defaults to `table`.
    """

    __slots__ = (
        "_database",
        "_invitations_table",
        "_memberships_table",
        "_organizations_table",
        "_roles",
        "_statements",
    )

    def __init__(
        self,
        database: Any,
        *,
        roles: Iterable[str],
        table: str = "wreath_organization",
        prefix: str | None = None,
    ) -> None:
        self._roles = frozenset(str(role) for role in roles)
        if not self._roles:
            raise ValueError(
                "PostgresOrganizationStore requires a non-empty role vocabulary; "
                "without one a policy naming a role cannot be checked at startup"
            )
        self._database = database
        self._organizations_table = sql_identifier(table)
        self._memberships_table = sql_identifier(f"{table}_membership")
        self._invitations_table = sql_identifier(f"{table}_invitation")
        statements = _OrganizationStatements(database, table if prefix is None else prefix)
        self._statements = statements

        organizations = self._organizations_table
        memberships = self._memberships_table
        invitations = self._invitations_table
        statements.define(
            "organization",
            f"SELECT id, name, metadata FROM {organizations} WHERE id = $1::text",
            workload="read",
        )
        statements.define(
            "create",
            f"INSERT INTO {organizations} (id, name, metadata) "
            "VALUES ($1::text, $2::text, $3::jsonb) "
            "ON CONFLICT (id) DO NOTHING RETURNING id",
        )
        statements.define(
            "memberships",
            f"SELECT organization_id, user_id, roles FROM {memberships} "
            "WHERE user_id = $1::text ORDER BY organization_id",
            workload="read",
        )
        statements.define(
            "members",
            f"SELECT organization_id, user_id, roles FROM {memberships} "
            "WHERE organization_id = $1::text ORDER BY user_id",
            workload="read",
        )
        statements.define(
            "add_member",
            f"WITH ensured AS (\n"
            f"  INSERT INTO {organizations} (id, name, metadata)\n"
            "  VALUES ($1::text, '', '{}'::jsonb)\n"
            "  ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id\n"
            "  RETURNING id\n"
            ")\n"
            f"INSERT INTO {memberships} (organization_id, user_id, roles)\n"
            "SELECT id, $2::text, $3::jsonb FROM ensured\n"
            "ON CONFLICT (organization_id, user_id) DO UPDATE SET roles = EXCLUDED.roles\n"
            "RETURNING organization_id, user_id, roles",
        )
        statements.define(
            "remove_member",
            f"DELETE FROM {memberships} WHERE organization_id = $1::text AND user_id = $2::text",
        )

        invite_head = (
            f"WITH ensured AS (\n"
            f"  INSERT INTO {organizations} (id, name, metadata)\n"
            "  VALUES ($2::text, '', '{}'::jsonb)\n"
            "  ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id\n"
            "  RETURNING id\n"
            ")\n"
            f"INSERT INTO {invitations} "
            "(token_hash, token, organization_id, email, roles, expires_at)\n"
        )
        invite_tail = (
            "\nRETURNING organization_id, email, roles, extract(epoch FROM expires_at)::float8"
        )
        statements.define(
            "invite_forever",
            invite_head
            + "SELECT $1::bytea, $5::text, id, $3::text, $4::jsonb, NULL FROM ensured"
            + invite_tail,
        )
        statements.define(
            "invite_ttl",
            invite_head + "SELECT $1::bytea, $5::text, id, $3::text, $4::jsonb, "
            "clock_timestamp() + make_interval(secs => $6::float8) FROM ensured" + invite_tail,
        )
        statements.define(
            "invite_at",
            invite_head + "SELECT $1::bytea, $5::text, id, $3::text, $4::jsonb, "
            "to_timestamp($6::float8) FROM ensured" + invite_tail,
        )

        accept_head = (
            f"WITH accepted AS (\n"
            f"  UPDATE {invitations} SET accepted_by = $2::text, "
            "accepted_at = clock_timestamp()\n"
            "  WHERE token_hash = $1::bytea AND accepted_by IS NULL\n"
        )
        accept_tail = (
            "  RETURNING organization_id, roles\n"
            "), written AS (\n"
            f"  INSERT INTO {memberships} (organization_id, user_id, roles)\n"
            "  SELECT organization_id, $2::text, roles FROM accepted\n"
            "  ON CONFLICT (organization_id, user_id) DO UPDATE SET roles = EXCLUDED.roles\n"
            "  RETURNING organization_id, user_id, roles\n"
            ")\n"
            "SELECT organization_id, user_id, roles FROM written"
        )
        statements.define(
            "accept",
            accept_head
            + "    AND (expires_at IS NULL OR expires_at > clock_timestamp())\n"
            + accept_tail,
        )
        statements.define(
            "accept_at",
            accept_head
            + "    AND (expires_at IS NULL OR expires_at > to_timestamp($3::float8))\n"
            + accept_tail,
        )
        statements.define(
            "invitation_state",
            "SELECT accepted_by, "
            "(expires_at IS NOT NULL AND expires_at <= clock_timestamp()) "
            f"FROM {invitations} WHERE token_hash = $1::bytea",
        )
        statements.define(
            "invitation_state_at",
            "SELECT accepted_by, "
            "(expires_at IS NOT NULL AND expires_at <= to_timestamp($2::float8)) "
            f"FROM {invitations} WHERE token_hash = $1::bytea",
        )
        statements.define(
            "invitations",
            "SELECT token, organization_id, email, roles, "
            "extract(epoch FROM expires_at)::float8, accepted_by "
            f"FROM {invitations} WHERE organization_id = $1::text "
            "ORDER BY created_at, token_hash",
            workload="read",
        )

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

    @staticmethod
    def _json(value: object) -> object:
        if isinstance(value, (str, bytes)):
            from ._json import loads

            return loads(value)
        return value

    def _roles_from(self, value: object) -> frozenset[str]:
        decoded = self._json(value)
        if not isinstance(decoded, list) or any(not isinstance(role, str) for role in decoded):
            raise ValueError("organization roles must be a JSON array of strings")
        roles = frozenset(cast("list[str]", decoded))
        self._check_roles(roles)
        return roles

    @staticmethod
    def _token_hash(token: str) -> bytes:
        return _invitation_digest(token)

    @property
    def schema_database(self) -> Any:
        """The database that owns `component()`'s three tables."""
        return self._database

    def component(self) -> Any:
        """The durable store's additive, application-owned schema claim."""
        from .schema import Component, Step

        organizations = self._organizations_table
        memberships = self._memberships_table
        invitations = self._invitations_table
        return Component(
            name="organizations",
            schema="",
            relations=(organizations, memberships, invitations),
            steps=(
                Step(
                    version=1,
                    statements=(
                        f"CREATE TABLE IF NOT EXISTS {organizations} (\n"
                        "  id text PRIMARY KEY,\n"
                        "  name text NOT NULL DEFAULT '',\n"
                        "  metadata jsonb NOT NULL DEFAULT '{}'::jsonb\n"
                        ")",
                        f"CREATE TABLE IF NOT EXISTS {memberships} (\n"
                        f"  organization_id text NOT NULL REFERENCES {organizations}(id) "
                        "ON DELETE CASCADE,\n"
                        "  user_id text NOT NULL,\n"
                        "  roles jsonb NOT NULL DEFAULT '[]'::jsonb,\n"
                        "  PRIMARY KEY (organization_id, user_id),\n"
                        "  CHECK (jsonb_typeof(roles) = 'array')\n"
                        ")",
                        f"CREATE INDEX IF NOT EXISTS {memberships}_user_idx "
                        f"ON {memberships} (user_id)",
                        f"CREATE TABLE IF NOT EXISTS {invitations} (\n"
                        "  token_hash bytea PRIMARY KEY,\n"
                        "  token text NOT NULL,\n"
                        f"  organization_id text NOT NULL REFERENCES {organizations}(id) "
                        "ON DELETE CASCADE,\n"
                        "  email text NOT NULL,\n"
                        "  roles jsonb NOT NULL DEFAULT '[]'::jsonb,\n"
                        "  expires_at timestamptz,\n"
                        "  accepted_by text,\n"
                        "  accepted_at timestamptz,\n"
                        "  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
                        "  CHECK (jsonb_typeof(roles) = 'array')\n"
                        ")",
                        f"CREATE INDEX IF NOT EXISTS {invitations}_organization_idx "
                        f"ON {invitations} (organization_id, created_at)",
                    ),
                ),
            ),
        )

    def schema_sql(self) -> str:
        """DDL for all three durable tables, derived from `component()`."""
        return self.component().sql()

    async def organization(self, org_id: str) -> Organization | None:
        row = await self._statements.statement("organization").fetchrow(org_id)
        if row is None:
            return None
        metadata = self._json(row[2])
        clean_metadata = (
            {str(key): value for key, value in metadata.items()}
            if isinstance(metadata, Mapping)
            else {}
        )
        return Organization(
            id=str(row[0]),
            name=str(row[1]),
            metadata=clean_metadata,
        )

    async def create(self, organization: Organization) -> Organization:
        from ._json import dumps

        metadata = dumps(dict(organization.metadata)).decode("utf-8")
        row = await self._statements.statement("create").fetchrow(
            organization.id, organization.name, metadata
        )
        if row is None:
            raise ValueError(f"organization {organization.id!r} already exists")
        return organization

    def _membership(self, row: Any) -> Membership:
        return Membership(str(row[0]), str(row[1]), self._roles_from(row[2]))

    async def memberships(self, user_id: str) -> tuple[Membership, ...]:
        rows = await self._statements.statement("memberships").fetch(user_id)
        return tuple(self._membership(row) for row in rows)

    async def members(self, org_id: str) -> tuple[Membership, ...]:
        rows = await self._statements.statement("members").fetch(org_id)
        return tuple(self._membership(row) for row in rows)

    async def add_member(
        self, org_id: str, user_id: str, *, roles: Iterable[str] = ()
    ) -> Membership:
        from ._json import dumps

        wanted = frozenset(str(role) for role in roles)
        self._check_roles(wanted)
        encoded = dumps(sorted(wanted)).decode("utf-8")
        row = await self._statements.statement("add_member").fetchrow(org_id, user_id, encoded)
        if row is None:
            raise RuntimeError("PostgreSQL did not return the membership it wrote")
        return self._membership(row)

    async def remove_member(self, org_id: str, user_id: str) -> bool:
        status = await self._statements.statement("remove_member").execute(org_id, user_id)
        return rows_affected(status) == 1

    async def invite(
        self,
        org_id: str,
        email: str,
        *,
        roles: Iterable[str] = (),
        ttl: float | None = None,
        now: float | None = None,
    ) -> Invitation:
        from ._json import dumps

        wanted = frozenset(str(role) for role in roles)
        self._check_roles(wanted)
        token = secrets.token_urlsafe(32)
        args: tuple[Any, ...] = (
            self._token_hash(token),
            org_id,
            email,
            dumps(sorted(wanted)).decode("utf-8"),
            token,
        )
        if ttl is None:
            name = "invite_forever"
        elif now is None:
            name, args = "invite_ttl", (*args, float(ttl))
        else:
            name, args = "invite_at", (*args, float(now + ttl))
        row = await self._statements.statement(name).fetchrow(*args)
        if row is None:
            raise RuntimeError("PostgreSQL did not return the invitation it wrote")
        return Invitation(
            token=token,
            organization=str(row[0]),
            email=str(row[1]),
            roles=self._roles_from(row[2]),
            expires_at=None if row[3] is None else float(row[3]),
        )

    async def accept(self, token: str, user_id: str, *, now: float | None = None) -> Membership:
        digest = self._token_hash(token)
        if now is None:
            accept_name, accept_args = "accept", (digest, user_id)
        else:
            accept_name, accept_args = "accept_at", (digest, user_id, float(now))
        row = await self._statements.statement(accept_name).fetchrow(*accept_args)
        if row is not None:
            return self._membership(row)

        if now is None:
            state_name, state_args = "invitation_state", (digest,)
        else:
            state_name, state_args = "invitation_state_at", (digest, float(now))
        # This diagnostic must use the writer pool too: it immediately follows
        # the atomic UPDATE, and a replica may not yet contain the invitation or
        # its accepted_by value. A lagging read would misreport a spent token as
        # nonexistent (or, worse, still live).
        state = await self._statements.statement(state_name).fetchrow(*state_args)
        if state is None:
            raise ValueError("no such invitation")
        if state[0] is not None:
            raise ValueError("invitation has already been accepted")
        if bool(state[1]):
            raise ValueError("invitation has expired")
        # A valid, unaccepted row here means another transaction changed and
        # rolled it back between the two statements. Refuse rather than using a
        # read-then-write fallback that would break the single-use guarantee.
        raise RuntimeError("invitation changed while it was being accepted; retry")

    async def invitations(self, org_id: str) -> tuple[Invitation, ...]:
        rows = await self._statements.statement("invitations").fetch(org_id)
        return tuple(
            Invitation(
                token=str(row[0]),
                organization=str(row[1]),
                email=str(row[2]),
                roles=self._roles_from(row[3]),
                expires_at=None if row[4] is None else float(row[4]),
                accepted_by=None if row[5] is None else str(row[5]),
            )
            for row in rows
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

    @property
    def schema_owners(self) -> tuple[Any, ...]:
        """Durable stores whose schema the configured authorizer must own."""
        component = getattr(self._store, "component", None)
        return (self._store,) if callable(component) else ()

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
