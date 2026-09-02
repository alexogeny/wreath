from __future__ import annotations

import warnings
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
from wreath.subscriptions import SubscriptionAccess
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


@pytest.mark.asyncio
async def test_a_member_is_permitted_and_a_non_member_is_not() -> None:
    store = InMemoryOrganizationStore(roles=ROLES)
    await store.add_member("acme", "alice", roles={"admin"})
    provider = Memberships(store)

    assert await _status(ORG_POLICY, identity=Identity("alice"), organizations=provider) == 200
    assert await _status(ORG_POLICY, identity=Identity("bob"), organizations=provider) == 403


@pytest.mark.asyncio
async def test_a_role_in_one_organization_is_not_a_role_in_another() -> None:
    store = InMemoryOrganizationStore(roles=ROLES)
    await store.add_member("acme", "alice", roles={"member"})
    await store.add_member("globex", "alice", roles={"admin"})
    provider = Memberships(store)

    assert await _status(ROLE_POLICY, identity=Identity("alice"), organizations=provider) == 403, (
        "an admin of globex was treated as an admin of acme"
    )

    await store.add_member("acme", "alice", roles={"admin"})
    assert await _status(ROLE_POLICY, identity=Identity("alice"), organizations=provider) == 200


@pytest.mark.asyncio
async def test_no_provider_denies_rather_than_permitting() -> None:
    assert await _status(ORG_POLICY, identity=Identity("alice")) == 403
    assert await _status(ROLE_POLICY, identity=Identity("alice")) == 403


@pytest.mark.asyncio
async def test_an_unless_forbid_still_stands_with_no_provider() -> None:
    source = (
        "permit(principal, action, resource);\n"
        "forbid(principal, action, resource)\n"
        'unless { context.organizations.contains("acme") };'
    )
    assert await _status(source, identity=Identity("alice")) == 403


@pytest.mark.asyncio
async def test_a_claimed_membership_the_store_denies_grants_nothing() -> None:
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
    assert await _status(ENTITLEMENT_POLICY, identity=identity, entitlements=provider) == 403, (
        "claiming a plan granted that plan's entitlements"
    )


@pytest.mark.asyncio
async def test_a_plan_limit_requires_an_atomic_entitlement_provider() -> None:
    provider = Entitlements({"alice": {"export"}}, plans={"alice": "pro"})
    identity = (human(Identity("alice")) | on_plan("pro")).bind()
    assert await _status(ENTITLEMENT_POLICY, identity=identity, entitlements=provider) == 403
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_an_async_atomic_entitlement_provider_is_awaited_once() -> None:
    class AsyncEntitlements:
        def __init__(self) -> None:
            self.calls = 0

        async def resolve(self, identity: Identity) -> SubscriptionAccess:
            self.calls += 1
            return SubscriptionAccess("pro", frozenset({"export"}))

        def names(self) -> frozenset[str]:
            return frozenset({"export"})

    provider = AsyncEntitlements()
    identity = (human(Identity("alice")) | on_plan("pro")).bind()

    assert await _status(ENTITLEMENT_POLICY, identity=identity, entitlements=provider) == 200
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_a_sync_entitlement_method_returning_an_awaitable_is_awaited_once() -> None:
    class AwaitableEntitlements:
        def __init__(self) -> None:
            self.calls = 0

        def resolve(self, identity: Identity) -> Any:
            async def resolved() -> SubscriptionAccess:
                self.calls += 1
                return SubscriptionAccess("pro", frozenset({"export"}))

            return resolved()

        def names(self) -> frozenset[str]:
            return frozenset({"export"})

    provider = AwaitableEntitlements()
    identity = (human(Identity("alice")) | on_plan("pro")).bind()

    assert await _status(ENTITLEMENT_POLICY, identity=identity, entitlements=provider) == 200
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_an_unrestricted_caller_keeps_what_the_provider_grants_whatever_the_plan() -> None:
    provider = Entitlements({"alice": {"export"}}, plans={"alice": "pro"})
    assert (
        await _status(ENTITLEMENT_POLICY, identity=Identity("alice"), entitlements=provider) == 200
    )


@pytest.mark.asyncio
async def test_a_claimed_plan_a_provider_cannot_confirm_yields_nothing() -> None:

    class PlanBlind:
        """An entitlement provider with no way to report a caller's plan."""

        def entitlements(self, identity: Any) -> frozenset[str]:
            return frozenset({"export"})

        def names(self) -> frozenset[str]:
            return frozenset({"export"})

    identity = (human(Identity("alice")) | on_plan("pro")).bind()
    assert await _status(ENTITLEMENT_POLICY, identity=identity, entitlements=PlanBlind()) == 403


def test_an_anonymous_request_resolves_every_fact_to_nothing() -> None:
    authorizer = CedarAuthorizer(
        engine=CedarPolicies(ENTITLEMENT_POLICY),
        entitlements=Entitlements({"alice": {"export"}}),
    )
    facts = authorizer.facts_for(FakeRequest(None))
    assert facts["entitlements"] == frozenset()


@pytest.mark.asyncio
async def test_an_entitlement_limit_narrows_what_the_provider_grants() -> None:
    provider = Entitlements({"alice": {"export", "api"}})
    identity = (human(Identity("alice")) | with_entitlements("api")).bind()
    assert await _status(ENTITLEMENT_POLICY, identity=identity, entitlements=provider) == 403


@pytest.mark.asyncio
async def test_a_policy_naming_no_entitlement_never_asks_the_provider() -> None:
    provider = Entitlements({"alice": {"export"}})
    source = 'permit(principal, action == Action::"read", resource);'
    assert await _status(source, identity=Identity("alice"), entitlements=provider) == 200
    assert provider.calls == 0, (
        f"resolved entitlements {provider.calls} time(s) for a policy that reads none"
    )


@pytest.mark.asyncio
async def test_a_policy_reading_entitlements_asks_exactly_once() -> None:
    provider = Entitlements({"alice": {"export"}})
    source = ENTITLEMENT_POLICY + (
        '\nforbid(principal, action == Action::"read", resource)\n'
        'unless { context.entitlements.contains("export") };'
    )
    assert await _status(source, identity=Identity("alice"), entitlements=provider) == 200
    assert provider.calls == 1, f"resolved {provider.calls} times in one request"


def test_a_policy_naming_an_undeclared_role_fails_at_startup() -> None:
    store = InMemoryOrganizationStore(roles=ROLES)
    source = (
        'permit(principal, action == Action::"read", resource)\n'
        'when { context.org_roles.contains("acme:amdin") };'
    )
    with pytest.raises(ValueError, match="amdin"):
        CedarAuthorizer(engine=CedarPolicies(source), organizations=Memberships(store))


def test_the_startup_refusal_says_why_it_would_have_denied_forever() -> None:
    store = InMemoryOrganizationStore(roles=ROLES)
    source = (
        'permit(principal, action == Action::"read", resource)\n'
        'when { context.org_roles.contains("acme:amdin") };'
    )
    with pytest.raises(ValueError) as caught:
        CedarAuthorizer(engine=CedarPolicies(source), organizations=Memberships(store))
    assert "deny forever" in str(caught.value)


def test_a_correct_role_boots() -> None:
    store = InMemoryOrganizationStore(roles=ROLES)
    CedarAuthorizer(engine=CedarPolicies(ROLE_POLICY), organizations=Memberships(store))


def test_an_organization_id_is_data_and_is_not_refused() -> None:
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
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        CedarAuthorizer(engine=CedarPolicies(ROLE_POLICY))
        CedarAuthorizer(engine=CedarPolicies(ENTITLEMENT_POLICY))


class Unenumerable:
    """An organisation provider that resolves memberships but declares no roles.

    The shape `_validate_org_roles` cannot check: `names` is what it enumerates
    against, and a provider without one leaves a misspelled role undetectable
    until it denies in production.
    """

    def for_request(self, request: Any) -> tuple[Any, ...]:
        return ()


def test_a_provider_that_cannot_enumerate_its_roles_says_so() -> None:
    with pytest.warns(RuntimeWarning, match="cannot enumerate its roles"):
        CedarAuthorizer(engine=CedarPolicies(ROLE_POLICY), organizations=Unenumerable())


def test_a_provider_that_cannot_enumerate_is_silent_when_no_policy_names_a_role() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        CedarAuthorizer(engine=CedarPolicies(ORG_POLICY), organizations=Unenumerable())


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
    from wreath.organizations import Membership

    provider = Memberships(AsyncOnlyStore())
    request = FakeRequest(Identity("alice"))
    load_into(request, [Membership("acme", "alice", frozenset({"admin"}))])

    assert provider.for_request(request) == (Membership("acme", "alice", frozenset({"admin"})),)


def test_no_snapshot_and_no_synchronous_source_resolves_to_nothing() -> None:
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
    assert decision.allowed is False, "an admin of globex was admin here while acting in acme"

    acting_in_globex = SessionRequest(Identity("alice"), {ACTIVE_ORGANIZATION_KEY: "globex"})
    load_into(acting_in_globex, memberships)
    decision = await authorizer.authorize(
        acting_in_globex, PolicyRequirement(action="read", resource='Doc::"d"')
    )
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_no_active_organization_supplies_no_unqualified_roles() -> None:
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


def test_every_declared_fact_shares_one_implementation() -> None:
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
    store = InMemoryOrganizationStore(roles=ROLES)
    await store.add_member("acme", "alice", roles={"admin"})
    authorizer = CedarAuthorizer(engine=CedarPolicies(ORG_POLICY), organizations=Memberships(store))

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


def test_an_anonymous_request_resolves_to_nothing_against_a_sync_store() -> None:
    store = InMemoryOrganizationStore(roles=ROLES)

    class Sync:
        def roles(self) -> frozenset[str]:
            return ROLES

        def memberships_for(self, user_id: str) -> tuple[Any, ...]:
            raise AssertionError("an anonymous request must not be looked up")

    assert Memberships(Sync()).for_request(FakeRequest(None)) == ()
    assert Memberships(store).for_request(FakeRequest(None)) == ()


def test_a_provider_over_a_store_with_no_roles_enumerates_nothing() -> None:

    class Roleless:
        pass

    assert Memberships(Roleless()).names() == frozenset()


def test_the_active_organization_falls_back_to_the_principal_limits() -> None:
    from wreath.organizations import active_organization

    identity = (human(Identity("alice")) | member_of("acme", role="admin")).bind()
    assert active_organization(FakeRequest(identity)) == "acme"


def test_an_empty_session_value_is_not_an_active_organization() -> None:
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
