from __future__ import annotations

from types import SimpleNamespace

import pytest

from wreath.organizations import (
    InMemoryOrganizationStore,
    Invitation,
    Membership,
    Memberships,
    Organization,
    OrganizationFederation,
    OrganizationFederationError,
    OrganizationStore,
    PostgresOrganizationStore,
)


class _InstallationOrganizations:
    async def organization_for(self, provider: str, installation: str) -> str | None:
        if (provider, installation) == ("slack", "T1"):
            return "acme"
        return None


async def test_chat_federation_reads_the_scim_owned_membership_each_time() -> None:
    from wreath.auth import Identity
    from wreath.chat import ExternalIdentityKey, PrincipalBinding

    store = _store()
    await store.add_member("acme", "alice", roles={"member"})
    external = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    binding = PrincipalBinding(identity=Identity("alice"), external=external)
    federation = OrganizationFederation(store, _InstallationOrganizations())

    resolved = await federation.resolve(external, binding)
    assert resolved.tenant == "acme"

    await store.remove_member("acme", "alice")
    with pytest.raises(OrganizationFederationError, match="not.*member"):
        await federation.resolve(external, binding)


@pytest.mark.parametrize(
    "external",
    [
        SimpleNamespace(provider="", installation="T1"),
        SimpleNamespace(provider="slack", installation=""),
    ],
)
async def test_chat_federation_requires_both_installation_coordinates(external) -> None:
    federation = OrganizationFederation(_store(), _InstallationOrganizations())
    with pytest.raises(OrganizationFederationError, match="requires provider and installation"):
        await federation.resolve(external, SimpleNamespace(identity=SimpleNamespace(id="alice")))


async def test_chat_federation_requires_an_installation_mapping() -> None:
    federation = OrganizationFederation(_store(), _InstallationOrganizations())
    external = SimpleNamespace(provider="slack", installation="missing")
    with pytest.raises(OrganizationFederationError, match="no organization mapping"):
        await federation.resolve(external, SimpleNamespace(identity=SimpleNamespace(id="alice")))


@pytest.mark.parametrize("user_id", [None, "", 7])
async def test_chat_federation_requires_a_nonempty_string_user_id(user_id) -> None:
    federation = OrganizationFederation(_store(), _InstallationOrganizations())
    external = SimpleNamespace(provider="slack", installation="T1")
    binding = SimpleNamespace(identity=SimpleNamespace(id=user_id))
    with pytest.raises(OrganizationFederationError, match="non-empty Wreath identity id"):
        await federation.resolve(external, binding)


async def test_chat_federation_accepts_a_matching_tenant_and_refuses_another() -> None:
    store = _store()
    await store.add_member("acme", "alice", roles={"member"})
    federation = OrganizationFederation(store, _InstallationOrganizations())
    external = SimpleNamespace(provider="slack", installation="T1")
    identity = SimpleNamespace(id="alice")

    from wreath.chat import ExternalIdentityKey, PrincipalBinding

    key = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    matching = PrincipalBinding(identity=identity, external=key, tenant="acme")
    assert (await federation.resolve(external, matching)).tenant == "acme"
    mismatch = PrincipalBinding(identity=identity, external=key, tenant="other")
    with pytest.raises(OrganizationFederationError, match="does not match"):
        await federation.resolve(external, mismatch)

ROLES = frozenset({"admin", "member", "billing"})


def _store() -> InMemoryOrganizationStore:
    return InMemoryOrganizationStore(roles=ROLES)


def test_an_organization_id_may_not_contain_a_colon() -> None:
    with pytest.raises(ValueError, match="must not contain ':'"):
        Organization(id="acme:evil")


def test_an_organization_id_must_be_non_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Organization(id="")


@pytest.mark.asyncio
async def test_one_user_two_organizations_with_different_roles() -> None:
    store = _store()
    await store.add_member("acme", "alice", roles={"admin"})
    await store.add_member("globex", "alice", roles={"member"})

    memberships = await store.memberships("alice")
    assert memberships == (
        Membership("acme", "alice", frozenset({"admin"})),
        Membership("globex", "alice", frozenset({"member"})),
    )

    qualified = set()
    for membership in memberships:
        qualified |= membership.qualified_roles()
    assert qualified == {"acme:admin", "globex:member"}
    assert "globex:admin" not in qualified, (
        "an admin of one organization became an admin of another"
    )


@pytest.mark.asyncio
async def test_an_undeclared_role_is_refused() -> None:
    store = _store()
    with pytest.raises(ValueError, match="unknown role"):
        await store.add_member("acme", "alice", roles={"amdin"})


def test_a_store_with_no_declared_roles_is_refused() -> None:
    with pytest.raises(ValueError, match="non-empty role vocabulary"):
        InMemoryOrganizationStore(roles=())


def test_a_postgres_store_with_no_declared_roles_is_refused() -> None:
    with pytest.raises(ValueError, match="non-empty role vocabulary"):
        PostgresOrganizationStore(object(), roles=())


@pytest.mark.asyncio
async def test_adding_a_member_replaces_their_roles_rather_than_adding() -> None:
    store = _store()
    await store.add_member("acme", "alice", roles={"admin", "billing"})
    await store.add_member("acme", "alice", roles={"member"})
    memberships = await store.memberships("alice")
    assert memberships[0].roles == frozenset({"member"}), (
        "roles accumulated; a demotion would not have taken effect"
    )


@pytest.mark.asyncio
async def test_removing_a_membership_reports_whether_there_was_one() -> None:
    store = _store()
    await store.add_member("acme", "alice", roles={"member"})
    assert await store.remove_member("acme", "alice") is True
    assert await store.remove_member("acme", "alice") is False
    assert await store.memberships("alice") == ()


@pytest.mark.asyncio
async def test_a_user_with_no_memberships_resolves_to_nothing() -> None:
    assert await _store().memberships("nobody") == ()


@pytest.mark.asyncio
async def test_adding_a_member_creates_the_organization_implicitly() -> None:
    store = _store()
    await store.add_member("fresh", "alice", roles={"admin"})
    assert await store.organization("fresh") == Organization(id="fresh")


@pytest.mark.asyncio
async def test_creating_a_duplicate_organization_is_refused() -> None:
    store = _store()
    await store.create(Organization(id="acme", name="Acme"))
    with pytest.raises(ValueError, match="already exists"):
        await store.create(Organization(id="acme"))


@pytest.mark.asyncio
async def test_an_invitation_survives_the_invitee_having_no_account() -> None:
    store = _store()
    invitation = await store.invite("acme", "new@example.com", roles={"member"})

    assert isinstance(invitation, Invitation)
    assert invitation.accepted_by is None
    assert await store.memberships("later-user-id") == ()

    membership = await store.accept(invitation.token, "later-user-id")
    assert membership == Membership("acme", "later-user-id", frozenset({"member"}))


@pytest.mark.asyncio
async def test_an_invitation_is_single_use() -> None:
    store = _store()
    invitation = await store.invite("acme", "new@example.com", roles={"member"})
    await store.accept(invitation.token, "alice")

    with pytest.raises(ValueError) as caught:
        await store.accept(invitation.token, "mallory")
    # The distinct text, not merely that it refused: every refusal here would
    # otherwise mention the invitation, so asserting on that would pass on
    # whichever branch fired, including the fallthrough.
    assert str(caught.value) == "invitation has already been accepted"


@pytest.mark.asyncio
async def test_an_expired_invitation_is_refused_distinctly() -> None:
    store = _store()
    invitation = await store.invite("acme", "new@example.com", roles={"member"}, ttl=60, now=1000.0)
    with pytest.raises(ValueError) as caught:
        await store.accept(invitation.token, "alice", now=1061.0)
    assert str(caught.value) == "invitation has expired"


@pytest.mark.asyncio
async def test_an_unknown_token_is_refused_distinctly() -> None:
    store = _store()
    with pytest.raises(ValueError) as caught:
        await store.accept("not-a-token", "alice")
    assert str(caught.value) == "no such invitation"


@pytest.mark.asyncio
async def test_an_invitation_may_not_grant_an_undeclared_role() -> None:
    store = _store()
    with pytest.raises(ValueError, match="unknown role"):
        await store.invite("acme", "new@example.com", roles={"root"})


@pytest.mark.asyncio
async def test_invitations_are_listed_per_organization() -> None:
    store = _store()
    await store.invite("acme", "a@example.com")
    await store.invite("globex", "b@example.com")
    acme = await store.invitations("acme")
    assert [invitation.email for invitation in acme] == ["a@example.com"]


@pytest.mark.asyncio
async def test_the_synchronous_read_agrees_with_the_asynchronous_one() -> None:
    store = _store()
    await store.add_member("acme", "alice", roles={"admin"})
    assert store.memberships_for("alice") == await store.memberships("alice")


@pytest.mark.asyncio
async def test_an_unexpired_invitation_is_accepted() -> None:
    store = _store()
    invitation = await store.invite("acme", "a@example.com", roles={"member"}, ttl=60, now=1000.0)
    membership = await store.accept(invitation.token, "alice", now=1059.0)
    assert membership.roles == frozenset({"member"})


@pytest.mark.asyncio
async def test_expiry_is_inclusive_at_the_boundary() -> None:
    store = _store()
    invitation = await store.invite("acme", "a@example.com", ttl=60, now=1000.0)
    with pytest.raises(ValueError, match="expired"):
        await store.accept(invitation.token, "alice", now=1060.0)


@pytest.mark.asyncio
async def test_a_token_matches_only_its_own_invitation() -> None:
    store = _store()
    await store.invite("acme", "a@example.com", roles={"admin"})
    second = await store.invite("globex", "b@example.com", roles={"member"})

    membership = await store.accept(second.token, "alice")
    assert membership.organization == "globex", (
        "presenting one token accepted a different invitation"
    )
    assert membership.roles == frozenset({"member"})

    remaining = await store.invitations("acme")
    assert remaining[0].accepted_by is None, "an unrelated invitation was consumed"


@pytest.mark.asyncio
async def test_an_unknown_token_is_refused_with_invitations_outstanding() -> None:
    store = _store()
    await store.invite("acme", "a@example.com")
    with pytest.raises(ValueError) as caught:
        await store.accept("not-a-token", "alice")
    assert str(caught.value) == "no such invitation"


@pytest.mark.asyncio
async def test_invitation_acceptance_does_not_walk_outstanding_invitations() -> None:
    class NoValueWalk(dict):
        def values(self):
            raise AssertionError("outstanding invitations were walked")

    store = _store()
    invitation = await store.invite("acme", "a@example.com")
    store._invitations = NoValueWalk(store._invitations)

    assert (await store.accept(invitation.token, "alice")).user_id == "alice"


@pytest.mark.asyncio
async def test_invite_and_accept_default_their_clock_to_the_wall_clock() -> None:
    store = _store()
    invitation = await store.invite("acme", "a@example.com", roles={"member"}, ttl=3600)
    assert invitation.expires_at is not None
    membership = await store.accept(invitation.token, "alice")
    assert membership.organization == "acme"


class _Statement:
    def __init__(self, database: _Database, name: str, sql: str, workload: str) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.database = database
        self.name = name
        self.sql = sql
        self.workload = workload

    async def fetchrow(self, *args: object) -> object:
        self.calls.append(args)
        return self.database.rows.get(self.name)

    async def fetch(self, *args: object) -> tuple[object, ...]:
        self.calls.append(args)
        result = self.database.rows.get(self.name, ())
        return result if isinstance(result, tuple) else ()

    async def execute(self, *args: object) -> str:
        self.calls.append(args)
        return self.database.statuses.get(self.name, "UPDATE 0")


class _Database:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}
        self.statements: dict[str, _Statement] = {}
        self.statuses: dict[str, str] = {}

    def statement(self, name: str, sql: str, *, workload: str) -> _Statement:
        prepared = _Statement(self, name, sql, workload)
        self.statements[name] = prepared
        return prepared


def _postgres(database: _Database | None = None) -> PostgresOrganizationStore:
    return PostgresOrganizationStore(database or _Database(), roles=ROLES)


def test_the_postgres_store_satisfies_the_public_protocol() -> None:
    assert isinstance(_postgres(), OrganizationStore)


def test_the_postgres_store_offers_all_three_durable_tables() -> None:
    store = _postgres()
    claim = store.component()
    assert claim.relations == (
        "wreath_organization",
        "wreath_organization_membership",
        "wreath_organization_invitation",
    )
    sql = store.schema_sql()
    assert "PRIMARY KEY (organization_id, user_id)" in sql
    assert "token_hash bytea PRIMARY KEY" in sql
    assert "accepted_by text" in sql
    assert "CREATE INDEX" in sql


async def test_postgres_statements_are_lazy_and_reads_use_the_read_pool() -> None:
    database = _Database()
    store = _postgres(database)
    assert database.statements == {}

    assert await store.organization("missing") is None

    statement = database.statements["wreath_organization_organization"]
    assert statement.workload == "read"
    assert statement.calls == [("missing",)]


async def test_postgres_statement_prefix_can_differ_from_the_table() -> None:
    database = _Database()
    store = PostgresOrganizationStore(
        database,
        roles=ROLES,
        table="org",
        prefix="custom",
    )
    assert await store.organization("missing") is None
    assert "custom_organization" in database.statements
    assert "org_organization" not in database.statements


async def test_acceptance_consumes_and_writes_membership_in_one_statement() -> None:
    database = _Database()
    database.rows["wreath_organization_accept"] = (
        "acme",
        "alice",
        '["member"]',
    )
    store = _postgres(database)

    membership = await store.accept("secret-token", "alice")

    assert membership == Membership("acme", "alice", frozenset({"member"}))
    statement = database.statements["wreath_organization_accept"]
    assert "UPDATE wreath_organization_invitation" in statement.sql
    assert "INSERT INTO wreath_organization_membership" in statement.sql
    assert "SELECT organization_id, $2::text, roles FROM accepted" in statement.sql
    digest, subject = statement.calls[0]
    assert digest != b"secret-token"
    assert isinstance(digest, bytes) and len(digest) == 32
    assert subject == "alice"


async def test_an_accepted_postgres_invitation_stays_consumed_for_a_new_store() -> None:
    database = _Database()
    database.rows["wreath_organization_invitation_state"] = ("alice", False)

    with pytest.raises(ValueError, match="already been accepted"):
        await _postgres(database).accept("spent", "mallory")

    statement = database.statements["wreath_organization_invitation_state"]
    assert statement.workload == "write"


async def test_postgres_accept_distinguishes_missing_and_changed_invitations() -> None:
    missing = _Database()
    with pytest.raises(ValueError, match="no such invitation"):
        await _postgres(missing).accept("missing", "alice")

    changed = _Database()
    changed.rows["wreath_organization_invitation_state"] = (None, False)
    with pytest.raises(RuntimeError, match="changed while it was being accepted"):
        await _postgres(changed).accept("raced", "alice")


def test_membership_schema_owners_require_a_component_provider() -> None:
    class Roleless:
        pass

    assert Memberships(Roleless()).schema_owners == ()
    store = _postgres()
    assert Memberships(store).schema_owners == (store,)


@pytest.mark.parametrize("roles", ["{}", '["member", 1]', '["undeclared"]'])
async def test_postgres_roles_refuse_malformed_or_undeclared_database_rows(
    roles: str,
) -> None:
    database = _Database()
    database.rows["wreath_organization_accept"] = ("acme", "alice", roles)

    with pytest.raises(ValueError, match="roles"):
        await _postgres(database).accept("secret-token", "alice")


def test_a_configured_authorizer_registers_the_durable_organization_schema() -> None:
    from wreath import Wreath
    from wreath.authorization import CedarAuthorizer, CedarPolicies
    from wreath.organizations import Memberships

    database = _Database()
    store = _postgres(database)
    app = Wreath()
    app.configure_auth(
        object(),
        CedarAuthorizer(
            engine=CedarPolicies(
                "permit (principal, action, resource) when { context.organizations "
                '.contains("acme") };'
            ),
            organizations=Memberships(store),
        ),
    )

    assert [claim.name for claim in app.schema_components()] == ["organizations"]
    grouped = app._components_by_database(app.schema_components())
    assert grouped == {database: [store.component()]}
