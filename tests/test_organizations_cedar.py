"""Organisation membership as Cedar context, and the facts machinery behind it.

`context.flags` and `context.regions` were two hand-written copies of one rule.
Four more facts were queued behind them, and six copies of a security-critical
caching rule is how the copies drift -- in the *permit* direction. `SetFact` is
the one implementation; these tests hold every fact to the same four properties
rather than testing the newest one and hoping.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath._auth.cedar_engine import CedarPolicies
from wreath._auth.principal import human, member_of, on_plan, with_entitlements
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, authorize
from wreath.organizations import (
    ACTIVE_ORGANIZATION_KEY,
    InMemoryOrganizationStore,
    Memberships,
    load_into,
)
from wreath.testing import TestClient

ROLES = frozenset({"admin", "member"})

ORG_POLICY = """
permit(principal, action == Action::"read", resource)
when { context.organizations.contains("acme") };
"""

ROLE_POLICY = """
permit(principal, action == Action::"read", resource)
when { context.org_roles.contains("acme:admin") };
"""

ACTIVE_ROLE_POLICY = """
permit(principal, action == Action::"read", resource)
when { context.org_roles.contains("admin") };
"""

ENTITLEMENT_POLICY = """
permit(principal, action == Action::"read", resource)
when { context.entitlements.contains("export") };
"""


class Entitlements:
    """A minimal entitlement provider: the duck type the authorizer accepts."""

    def __init__(self, held: dict[str, set[str]], plans: dict[str, str] | None = None):
        self._held = held
        self._plans = plans or {}
        self.calls = 0

    def entitlements(self, identity: Any) -> frozenset[str]:
        self.calls += 1
        return frozenset(self._held.get(identity.id, ()))

    def plan_for(self, identity: Any) -> str | None:
        return self._plans.get(identity.id)

    def names(self) -> frozenset[str]:
        return frozenset({"export", "api"})


async def _status(
    source: str,
    *,
    identity: Identity,
    organizations: Any = None,
    entitlements: Any = None,
) -> int:
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: identity),
        CedarAuthorizer(
            engine=CedarPolicies(source),
            organizations=organizations,
            entitlements=entitlements,
        ),
    )

    @app.get("/thing")
    @authorize(action="read", resource=lambda request: 'Doc::"d"')
    async def thing(request: Any) -> str:
        return "ok"

    async with TestClient(app) as client:
        response = await client.get("/thing", headers={"authorization": "Bearer t"})
    return response.status


# --- membership reaches policy ----------------------------------------------


@pytest.mark.asyncio
async def test_a_member_is_permitted_and_a_non_member_is_not() -> None:
    store = InMemoryOrganizationStore(roles=ROLES)
    await store.add_member("acme", "alice", roles={"admin"})
    provider = Memberships(store)

    assert (
        await _status(ORG_POLICY, identity=Identity("alice"), organizations=provider)
        == 200
    )
    assert (
        await _status(ORG_POLICY, identity=Identity("bob"), organizations=provider)
        == 403
    )


@pytest.mark.asyncio
async def test_a_role_in_one_organization_is_not_a_role_in_another() -> None:
    """The cross-tenant leak, in the one shape that produces it."""
    store = InMemoryOrganizationStore(roles=ROLES)
    await store.add_member("acme", "alice", roles={"member"})
    await store.add_member("globex", "alice", roles={"admin"})
    provider = Memberships(store)

    assert (
        await _status(ROLE_POLICY, identity=Identity("alice"), organizations=provider)
        == 403
    ), "an admin of globex was treated as an admin of acme"

    await store.add_member("acme", "alice", roles={"admin"})
    assert (
        await _status(ROLE_POLICY, identity=Identity("alice"), organizations=provider)
        == 200
    )


@pytest.mark.asyncio
async def test_no_provider_denies_rather_than_permitting() -> None:
    """The fail-closed default, for the fact as for every other one."""
    assert await _status(ORG_POLICY, identity=Identity("alice")) == 403
    assert await _status(ROLE_POLICY, identity=Identity("alice")) == 403


@pytest.mark.asyncio
async def test_an_unless_forbid_still_stands_with_no_provider() -> None:
    """An *absent* set key skips a forbid rather than standing it.

    The reason every set fact is supplied even when switched off. Verified
    against the engine here rather than assumed to transfer from `flags`.
    """
    source = (
        'permit(principal, action, resource);\n'
        "forbid(principal, action, resource)\n"
        'unless { context.organizations.contains("acme") };'
    )
    assert await _status(source, identity=Identity("alice")) == 403


# --- limits narrow, never grant ---------------------------------------------


@pytest.mark.asyncio
async def test_a_claimed_membership_the_store_denies_grants_nothing() -> None:
    """Composition never grants. `member_of` is a restriction, not an assertion."""
    store = InMemoryOrganizationStore(roles=ROLES)
    provider = Memberships(store)
    identity = (human(Identity("alice")) | member_of("acme", role="admin")).bind()

    assert await _status(ORG_POLICY, identity=identity, organizations=provider) == 403, (
        "a composed membership the store does not hold was treated as real"
    )


@pytest.mark.asyncio
async def test_a_claimed_membership_narrows_a_real_one() -> None:
    store = InMemoryOrganizationStore(roles=ROLES)
    await store.add_member("acme", "alice", roles={"admin"})
    await store.add_member("globex", "alice", roles={"admin"})
    provider = Memberships(store)

    unrestricted = Identity("alice")
    assert await _status(ORG_POLICY, identity=unrestricted, organizations=provider) == 200

    elsewhere = (human(Identity("alice")) | member_of("globex")).bind()
    assert await _status(ORG_POLICY, identity=elsewhere, organizations=provider) == 403, (
        "a principal restricted to globex still satisfied an acme policy"
    )


@pytest.mark.asyncio
async def test_a_claimed_plan_the_provider_disagrees_with_yields_nothing() -> None:
    provider = Entitlements({"alice": {"export"}}, plans={"alice": "free"})
    identity = (human(Identity("alice")) | on_plan("pro")).bind()
    assert (
        await _status(ENTITLEMENT_POLICY, identity=identity, entitlements=provider)
        == 403
    ), "claiming a plan granted that plan's entitlements"


@pytest.mark.asyncio
async def test_a_matching_plan_keeps_the_entitlements() -> None:
    provider = Entitlements({"alice": {"export"}}, plans={"alice": "pro"})
    identity = (human(Identity("alice")) | on_plan("pro")).bind()
    assert (
        await _status(ENTITLEMENT_POLICY, identity=identity, entitlements=provider)
        == 200
    )


@pytest.mark.asyncio
async def test_an_entitlement_limit_narrows_what_the_provider_grants() -> None:
    provider = Entitlements({"alice": {"export", "api"}})
    identity = (human(Identity("alice")) | with_entitlements("api")).bind()
    assert (
        await _status(ENTITLEMENT_POLICY, identity=identity, entitlements=provider)
        == 403
    )


# --- laziness: a fact nobody reads costs nothing -----------------------------


@pytest.mark.asyncio
async def test_a_policy_naming_no_entitlement_never_asks_the_provider() -> None:
    """The vocabulary walk is the laziness mechanism, not an optimisation.

    An entitlement or membership lookup can be a database round trip, so a fact
    no policy reads must not resolve at all -- otherwise every application pays
    for every fact any application might want.
    """
    provider = Entitlements({"alice": {"export"}})
    source = 'permit(principal, action == Action::"read", resource);'
    assert await _status(source, identity=Identity("alice"), entitlements=provider) == 200
    assert provider.calls == 0, (
        f"resolved entitlements {provider.calls} time(s) for a policy that reads none"
    )


@pytest.mark.asyncio
async def test_a_policy_reading_entitlements_asks_exactly_once() -> None:
    """Resolved once per request, however many policies evaluate.

    Two policies over one route still make one engine call, so the multi-policy
    shape that would expose a per-evaluation resolution is the manifest; this
    asserts the weaker but still meaningful property that one request resolves
    once, and the count is what would move.
    """
    provider = Entitlements({"alice": {"export"}})
    source = ENTITLEMENT_POLICY + (
        '\nforbid(principal, action == Action::"read", resource)\n'
        'unless { context.entitlements.contains("export") };'
    )
    assert await _status(source, identity=Identity("alice"), entitlements=provider) == 200
    assert provider.calls == 1, f"resolved {provider.calls} times in one request"


# --- startup validation ------------------------------------------------------


def test_a_policy_naming_an_undeclared_role_fails_at_startup() -> None:
    store = InMemoryOrganizationStore(roles=ROLES)
    source = (
        'permit(principal, action == Action::"read", resource)\n'
        'when { context.org_roles.contains("acme:amdin") };'
    )
    with pytest.raises(ValueError, match="amdin"):
        CedarAuthorizer(
            engine=CedarPolicies(source), organizations=Memberships(store)
        )


def test_the_startup_refusal_says_why_it_would_have_denied_forever() -> None:
    store = InMemoryOrganizationStore(roles=ROLES)
    source = (
        'permit(principal, action == Action::"read", resource)\n'
        'when { context.org_roles.contains("acme:amdin") };'
    )
    with pytest.raises(ValueError) as caught:
        CedarAuthorizer(
            engine=CedarPolicies(source), organizations=Memberships(store)
        )
    assert "deny forever" in str(caught.value)


def test_a_correct_role_boots() -> None:
    store = InMemoryOrganizationStore(roles=ROLES)
    CedarAuthorizer(engine=CedarPolicies(ROLE_POLICY), organizations=Memberships(store))


def test_an_organization_id_is_data_and_is_not_refused() -> None:
    """An organisation that does not exist yet must not stop the process booting.

    The asymmetry with roles is deliberate and is the reason `org_roles` gets
    its own validator: a role is configuration and can be enumerated, an
    organisation id is a row and cannot.
    """
    store = InMemoryOrganizationStore(roles=ROLES)
    source = (
        'permit(principal, action == Action::"read", resource)\n'
        'when { context.organizations.contains("not-created-yet") };'
    )
    CedarAuthorizer(engine=CedarPolicies(source), organizations=Memberships(store))


def test_a_policy_naming_an_undeclared_entitlement_fails_at_startup() -> None:
    with pytest.raises(ValueError, match="premium"):
        CedarAuthorizer(
            engine=CedarPolicies(
                'permit(principal, action == Action::"read", resource)\n'
                'when { context.entitlements.contains("premium") };'
            ),
            entitlements=Entitlements({}),
        )


def test_no_provider_is_not_refused() -> None:
    """Switching a capability off is a decision, not a misconfiguration."""
    CedarAuthorizer(engine=CedarPolicies(ROLE_POLICY))
    CedarAuthorizer(engine=CedarPolicies(ENTITLEMENT_POLICY))


# --- the snapshot path -------------------------------------------------------


class AsyncOnlyStore:
    """A store with no synchronous read -- what a database-backed one looks like."""

    def roles(self) -> frozenset[str]:
        return ROLES


class FakeState:
    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __setattr__(self, key: str, value: Any) -> None:
        object.__setattr__(self, key, value)


class FakeRequest:
    method = "GET"
    path = "/x"

    def __init__(self, identity: Any = None) -> None:
        self.identity = identity
        self.state = FakeState()


def test_a_loaded_snapshot_is_used_without_a_synchronous_source() -> None:
    """How a database-backed store participates without querying on the
    authorization path: whatever established identity loads the snapshot."""
    from wreath.organizations import Membership

    provider = Memberships(AsyncOnlyStore())
    request = FakeRequest(Identity("alice"))
    load_into(request, [Membership("acme", "alice", frozenset({"admin"}))])

    assert provider.for_request(request) == (
        Membership("acme", "alice", frozenset({"admin"})),
    )


def test_no_snapshot_and_no_synchronous_source_resolves_to_nothing() -> None:
    """And nothing denies. There is deliberately no async fallback: one that
    sometimes queried would make the authorization path's cost depend on whether
    a snapshot happened to be loaded."""
    provider = Memberships(AsyncOnlyStore())
    assert provider.for_request(FakeRequest(Identity("alice"))) == ()


def test_an_anonymous_request_resolves_to_nothing() -> None:
    assert Memberships(AsyncOnlyStore()).for_request(FakeRequest(None)) == ()


class SessionRequest(FakeRequest):
    """A request carrying a session, which is where the active organisation lives."""

    def __init__(self, identity: Any, session: dict[str, Any]) -> None:
        super().__init__(identity)
        self.session = session


@pytest.mark.asyncio
async def test_the_active_organization_supplies_unqualified_roles() -> None:
    """A policy written without thinking about tenancy must still be tenant-safe.

    `context.org_roles.contains("admin")` is the reading a policy author reaches
    for first. It means "admin of the organisation this request is acting in",
    never "admin of anything" -- so the unqualified names come only from the
    active organisation.
    """
    from wreath._auth.requirements import PolicyRequirement
    from wreath.organizations import Membership

    authorizer = CedarAuthorizer(
        engine=CedarPolicies(ACTIVE_ROLE_POLICY),
        organizations=Memberships(AsyncOnlyStore()),
    )
    memberships = [
        Membership("acme", "alice", frozenset({"member"})),
        Membership("globex", "alice", frozenset({"admin"})),
    ]

    acting_in_acme = SessionRequest(Identity("alice"), {ACTIVE_ORGANIZATION_KEY: "acme"})
    load_into(acting_in_acme, memberships)
    decision = await authorizer.authorize(
        acting_in_acme, PolicyRequirement(action="read", resource='Doc::"d"')
    )
    assert decision.allowed is False, (
        "an admin of globex was admin here while acting in acme"
    )

    acting_in_globex = SessionRequest(
        Identity("alice"), {ACTIVE_ORGANIZATION_KEY: "globex"}
    )
    load_into(acting_in_globex, memberships)
    decision = await authorizer.authorize(
        acting_in_globex, PolicyRequirement(action="read", resource='Doc::"d"')
    )
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_no_active_organization_supplies_no_unqualified_roles() -> None:
    """Fail-closed: without a chosen organisation, "admin" names nothing."""
    from wreath._auth.requirements import PolicyRequirement
    from wreath.organizations import Membership

    authorizer = CedarAuthorizer(
        engine=CedarPolicies(ACTIVE_ROLE_POLICY),
        organizations=Memberships(AsyncOnlyStore()),
    )
    request = FakeRequest(Identity("alice"))
    load_into(request, [Membership("acme", "alice", frozenset({"admin"}))])
    decision = await authorizer.authorize(
        request, PolicyRequirement(action="read", resource='Doc::"d"')
    )
    assert decision.allowed is False


@pytest.mark.asyncio
async def test_a_snapshot_reaches_policy() -> None:
    """End to end, through the real authorizer rather than the provider alone."""
    from wreath._auth.requirements import PolicyRequirement
    from wreath.organizations import Membership

    authorizer = CedarAuthorizer(
        engine=CedarPolicies(ORG_POLICY), organizations=Memberships(AsyncOnlyStore())
    )
    request = FakeRequest(Identity("alice"))
    load_into(request, [Membership("acme", "alice", frozenset({"admin"}))])

    decision = await authorizer.authorize(
        request, PolicyRequirement(action="read", resource='Doc::"d"')
    )
    assert decision.allowed is True


# --- every fact is held to the same rules ------------------------------------


def test_every_declared_fact_shares_one_implementation() -> None:
    """The dedupe, asserted rather than assumed.

    If a future fact is added by copying the flags path instead of declaring a
    `SetFact`, this is what notices. The register in `tests/test_cedar.py` pins
    the context keys; this pins that they all come from the same machinery.
    """
    from wreath._auth.facts import SetFact

    authorizer = CedarAuthorizer(engine=CedarPolicies(ORG_POLICY))
    facts = authorizer._facts
    assert {fact.attribute for fact in facts} == {
        "flags",
        "regions",
        "organizations",
        "org_roles",
        "entitlements",
        "quota",
    }
    assert all(isinstance(fact, SetFact) for fact in facts)


@pytest.mark.asyncio
async def test_facts_for_enumerates_what_a_manifest_must_tag() -> None:
    """A manifest tagged with only some facts outlives the others."""
    store = InMemoryOrganizationStore(roles=ROLES)
    await store.add_member("acme", "alice", roles={"admin"})
    authorizer = CedarAuthorizer(
        engine=CedarPolicies(ORG_POLICY), organizations=Memberships(store)
    )

    class FakeState:
        def get(self, key: str, default: Any = None) -> Any:
            return getattr(self, key, default)

        def __setattr__(self, key: str, value: Any) -> None:
            object.__setattr__(self, key, value)

    class FakeRequest:
        method = "GET"
        path = "/x"

        def __init__(self) -> None:
            self.identity = Identity("alice")
            self.state = FakeState()

    facts = authorizer.facts_for(FakeRequest())
    assert set(facts) == {
        "flags",
        "regions",
        "organizations",
        "org_roles",
        "entitlements",
        "quota",
    }
    assert facts["organizations"] == frozenset({"acme"})


# --- cases the mutation sweep named ------------------------------------------


def test_an_anonymous_request_resolves_to_nothing_against_a_sync_store() -> None:
    """The anonymous guard, exercised where it can actually be observed.

    Against a store with no synchronous read the answer is `()` either way, so
    the earlier anonymous test could not tell the guard from its absence.
    """
    store = InMemoryOrganizationStore(roles=ROLES)

    class Sync:
        def roles(self) -> frozenset[str]:
            return ROLES

        def memberships_for(self, user_id: str) -> tuple[Any, ...]:
            raise AssertionError("an anonymous request must not be looked up")

    assert Memberships(Sync()).for_request(FakeRequest(None)) == ()
    assert Memberships(store).for_request(FakeRequest(None)) == ()


def test_a_provider_over_a_store_with_no_roles_enumerates_nothing() -> None:
    """`names()` degrades rather than raising, and an empty vocabulary means a
    policy naming a role is refused at startup rather than silently trusted."""

    class Roleless:
        pass

    assert Memberships(Roleless()).names() == frozenset()


def test_the_active_organization_falls_back_to_the_principal_limits() -> None:
    """A composed principal names its own active organisation; a request with no
    session must still honour it."""
    from wreath.organizations import active_organization

    identity = (human(Identity("alice")) | member_of("acme", role="admin")).bind()
    assert active_organization(FakeRequest(identity)) == "acme"


def test_an_empty_session_value_is_not_an_active_organization() -> None:
    """`session["org"] = ""` must not select an organisation named empty."""
    from wreath.organizations import active_organization

    request = SessionRequest(Identity("alice"), {ACTIVE_ORGANIZATION_KEY: ""})
    assert active_organization(request) is None


def test_a_session_organization_wins_over_the_principal_limits() -> None:
    from wreath.organizations import active_organization

    identity = (human(Identity("alice")) | member_of("acme")).bind()
    request = SessionRequest(identity, {ACTIVE_ORGANIZATION_KEY: "globex"})
    assert active_organization(request) == "globex"


def test_no_session_and_no_limits_is_no_active_organization() -> None:
    from wreath.organizations import active_organization

    assert active_organization(FakeRequest(Identity("alice"))) is None
    assert active_organization(FakeRequest(None)) is None
