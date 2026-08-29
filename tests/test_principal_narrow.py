from __future__ import annotations

import itertools
from typing import Any

import pytest

from wreath import Wreath
from wreath._auth.cedar_engine import CedarPolicies
from wreath._auth.principal import (
    ANY_SCOPE,
    Limits,
    Narrowing,
    human,
    member_of,
    on_plan,
    with_entitlements,
)
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import AuthorizationDecision, CedarAuthorizer, authorize
from wreath.testing import TestClient


def test_narrowing_a_narrowing_intersects_the_scopes() -> None:
    principal = human(Identity("alice"))
    first = principal.narrow(actor="agent", scope={"read", "write"}, now=0.0)
    second = first.narrow(actor="subagent", scope={"write", "delete"}, now=0.0)

    assert second.narrowing is not None
    assert second.narrowing.scope == frozenset({"write"}), (
        "a sub-delegation widened its parent's scope"
    )
    assert second.narrowing.actor == "subagent"
    assert second.narrowing.on_behalf_of == "alice", "the human at the bottom of the chain was lost"
    assert second.narrowing.depth == 2


def test_a_sub_delegation_cannot_extend_the_expiry() -> None:
    principal = human(Identity("alice"))
    first = principal.narrow(actor="agent", scope=ANY_SCOPE, ttl=60, now=1000.0)
    second = first.narrow(actor="subagent", scope=ANY_SCOPE, ttl=6000, now=1000.0)

    assert second.narrowing is not None
    assert second.narrowing.expires_at == 1060.0, "a sub-delegation outlived its parent"


def test_an_untimed_narrowing_does_not_reset_a_timed_parent() -> None:
    timed = human(Identity("alice")).narrow(actor="a", scope=ANY_SCOPE, ttl=60, now=1000.0)
    child = timed.narrow(actor="b", scope=ANY_SCOPE, now=1000.0)
    assert child.narrowing is not None
    assert child.narrowing.expires_at == 1060.0

    untimed = human(Identity("alice")).narrow(actor="a", scope=ANY_SCOPE, now=1000.0)
    grandchild = untimed.narrow(actor="b", scope=ANY_SCOPE, ttl=30, now=1000.0)
    assert grandchild.narrowing is not None
    assert grandchild.narrowing.expires_at == 1030.0


def test_an_empty_scope_permits_nothing() -> None:
    empty = Narrowing(actor="agent", scope=frozenset())
    assert not empty.permits("read")

    any_scope = Narrowing(actor="agent", scope=None)
    assert any_scope.permits("read")


def test_narrow_refuses_an_unnamed_actor() -> None:
    with pytest.raises(ValueError, match="non-empty actor"):
        human(Identity("alice")).narrow(actor="", scope=ANY_SCOPE)


def test_narrow_refuses_a_non_positive_ttl() -> None:
    with pytest.raises(ValueError, match="ttl must be positive"):
        human(Identity("alice")).narrow(actor="a", scope=ANY_SCOPE, ttl=0)


def test_composition_intersects_limits_rather_than_unioning_them() -> None:
    composed = (
        human(Identity("alice")) | with_entitlements("export", "api") | with_entitlements("api")
    )
    assert composed.limits.entitlements == frozenset({"api"}), (
        "composing two entitlement limits widened the result"
    )


def test_membership_roles_are_namespaced_by_organization() -> None:
    composed = human(Identity("alice")) | member_of("acme", role="admin")
    assert composed.limits.org_roles == frozenset({"acme:admin"})
    assert composed.limits.active_organization == "acme"


def test_bind_carries_limits_and_narrowing_onto_the_identity() -> None:
    principal = (human(Identity("alice")) | on_plan("pro")).narrow(
        actor="agent", scope={"read"}, now=0.0
    )
    identity = principal.bind()
    assert identity.limits is not None and identity.limits.plan == "pro"
    assert identity.narrowing is not None and identity.narrowing.actor == "agent"


def test_an_ordinary_identity_carries_neither() -> None:
    identity = Identity("alice")
    assert identity.limits is None
    assert identity.narrowing is None


ACTIONS = ("read", "write", "delete")

#: Policy fragments, each parameterised by action. The last two exist to attack
#: the union bug directly: they permit *only* delegated callers, so an
#: implementation that substituted the delegated evaluation for the delegating
#: principal's would grant an agent something its human never had.
FRAGMENTS = (
    'permit(principal, action == Action::"{a}", resource);',
    'permit(principal in Role::"editor", action == Action::"{a}", resource);',
    'permit(principal, action == Action::"{a}", resource)\nwhen {{ context.method == "GET" }};',
    'permit(principal, action == Action::"{a}", resource)\nwhen {{ context.delegated }};',
    'permit(principal, action == Action::"{a}", resource)\nwhen {{ context.actor == "agent" }};',
    'forbid(principal, action == Action::"{a}", resource)\nunless {{ context.delegated }};',
    'permit(principal, action == Action::"{a}", resource);\n'
    'forbid(principal, action == Action::"{a}", resource)\n'
    "when {{ context.delegated }};",
)


def _policy_sets() -> list[str]:
    """Every one- and two-fragment policy set over the fragments and actions."""
    singles = [f.format(a=action) for f in FRAGMENTS for action in ACTIONS]
    pairs = [
        f"{left}\n{right}"
        for left, right in itertools.islice(itertools.combinations(singles, 2), 0, 400, 7)
    ]
    return singles + pairs


async def _decisions(
    source: str, *, narrowing: Any = None, roles: frozenset[str] = frozenset()
) -> dict[str, bool]:
    """What one caller may do, for every action, under one policy set.

    All three actions through one app and one client, because the app already
    carries a route for each and the only thing that varies between the two
    calls the property makes is the *caller* -- fixed when the backend is
    configured, so it needs an app of its own and the actions do not. Asking
    per action instead cost six app constructions, six policy compilations and
    six client lifecycles for every parameter set, which is what made this the
    heaviest non-tooling file in the suite at a 187ms median.
    """
    identity = Identity("alice", roles=roles, narrowing=narrowing)
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: identity),
        CedarAuthorizer(engine=CedarPolicies(source)),
    )

    for name in ACTIONS:

        def make(action_name: str) -> Any:
            @authorize(action=action_name, resource=lambda request: 'Doc::"r"')
            async def handler(request: Any) -> str:
                return "ok"

            return handler

        app.get(f"/{name}")(make(name))

    async with TestClient(app) as client:
        return {
            name: (await client.get(f"/{name}", headers={"authorization": "Bearer t"})).status
            == 200
            for name in ACTIONS
        }


async def _decide(
    source: str, action: str, *, narrowing: Any = None, roles: frozenset[str] = frozenset()
) -> bool:
    """Whether one caller may do one action under one policy set."""
    return (await _decisions(source, narrowing=narrowing, roles=roles))[action]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", _policy_sets())
async def test_a_narrowed_principal_never_exceeds_its_delegator(source: str) -> None:
    roles = frozenset({"editor"})
    narrowing = Narrowing(actor="agent", scope=None, on_behalf_of="alice")

    parent_decisions = await _decisions(source, roles=roles)
    child_decisions = await _decisions(source, narrowing=narrowing, roles=roles)

    for action in ACTIONS:
        parent = parent_decisions[action]
        child = child_decisions[action]
        if child and not parent:
            raise AssertionError(
                f"narrow() granted {action!r} that the delegating principal "
                f"was denied, under:\n{source}"
            )


@pytest.mark.asyncio
async def test_the_property_test_has_teeth() -> None:
    source = 'permit(principal, action == Action::"read", resource)\nwhen { context.delegated };'
    assert source in _policy_sets(), "the attack case left the corpus"

    narrowing = Narrowing(actor="agent", scope=None, on_behalf_of="alice")
    assert not await _decide(source, "read"), "the direct caller should be denied"
    assert not await _decide(source, "read", narrowing=narrowing), (
        "the delegate was granted what its delegator was denied -- narrow() "
        "unioned instead of intersecting"
    )


@pytest.mark.asyncio
async def test_an_action_outside_the_scope_is_refused() -> None:
    source = "permit(principal, action, resource);"
    narrowing = Narrowing(actor="agent", scope=frozenset({"read"}))
    assert await _decide(source, "read", narrowing=narrowing)
    assert not await _decide(source, "write", narrowing=narrowing)


@pytest.mark.asyncio
async def test_an_expired_delegation_is_refused() -> None:
    source = "permit(principal, action, resource);"
    expired = Narrowing(actor="agent", scope=None, expires_at=1.0)
    assert not await _decide(source, "read", narrowing=expired)


@pytest.mark.asyncio
async def test_scope_is_enforced_even_when_no_policy_mentions_it() -> None:
    source = "permit(principal, action, resource);"
    assert "scope" not in source
    narrowing = Narrowing(actor="agent", scope=frozenset({"read"}))
    assert not await _decide(source, "delete", narrowing=narrowing)


@pytest.mark.asyncio
async def test_the_refusal_names_the_reason_it_refused() -> None:
    from wreath._auth.requirements import PolicyRequirement

    authorizer = CedarAuthorizer(engine=CedarPolicies("permit(principal, action, resource);"))

    class FakeState:
        def get(self, key: str, default: Any = None) -> Any:
            return default

        def __setattr__(self, key: str, value: Any) -> None:
            object.__setattr__(self, key, value)

    class FakeRequest:
        method = "GET"
        path = "/x"

        def __init__(self, identity: Any) -> None:
            self.identity = identity
            self.state = FakeState()

    out_of_scope = Identity("alice", narrowing=Narrowing(actor="agent", scope=frozenset({"read"})))
    decision = await authorizer.authorize(
        FakeRequest(out_of_scope), PolicyRequirement(action="delete", resource='Doc::"r"')
    )
    assert decision.allowed is False
    assert decision.reason == "delegation scope does not cover this action"

    expired = Identity("alice", narrowing=Narrowing(actor="agent", scope=None, expires_at=1.0))
    decision = await authorizer.authorize(
        FakeRequest(expired), PolicyRequirement(action="read", resource='Doc::"r"')
    )
    assert decision.allowed is False
    assert decision.reason == "delegation expired", (
        "expiry and scope must be distinguishable, or a test cannot tell which fired"
    )


@pytest.mark.asyncio
async def test_a_policy_blind_to_delegation_costs_one_evaluation() -> None:
    calls = []

    class CountingEngine(CedarPolicies):
        def is_authorized(self, **kwargs: Any) -> Any:
            calls.append(kwargs["context"].get("delegated"))
            return super().is_authorized(**kwargs)

    identity = Identity("alice", narrowing=Narrowing(actor="agent", scope=None))
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: identity),
        CedarAuthorizer(engine=CountingEngine("permit(principal, action, resource);")),
    )

    @app.get("/read")
    @authorize(action="read", resource=lambda request: 'Doc::"r"')
    async def handler(request: Any) -> str:
        return "ok"

    async with TestClient(app) as client:
        response = await client.get("/read", headers={"authorization": "Bearer t"})

    assert response.status == 200
    assert calls == [False], f"expected one blind evaluation, got {calls}"


@pytest.mark.asyncio
async def test_a_policy_that_reads_delegation_costs_two() -> None:
    calls = []

    class CountingEngine(CedarPolicies):
        def is_authorized(self, **kwargs: Any) -> Any:
            calls.append(kwargs["context"].get("delegated"))
            return super().is_authorized(**kwargs)

    source = (
        "permit(principal, action, resource);\n"
        'forbid(principal, action == Action::"read", resource)\n'
        "when { context.delegated };"
    )
    identity = Identity("alice", narrowing=Narrowing(actor="agent", scope=None))
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: identity),
        CedarAuthorizer(engine=CountingEngine(source)),
    )

    @app.get("/read")
    @authorize(action="read", resource=lambda request: 'Doc::"r"')
    async def handler(request: Any) -> str:
        return "ok"

    async with TestClient(app) as client:
        response = await client.get("/read", headers={"authorization": "Bearer t"})

    assert response.status == 403, "the delegate escaped a forbid written for it"
    assert calls == [False, True], f"expected both passes, got {calls}"


# Every test below exists because `wreath mutant` removed a control and no test
# objected. They are grouped here rather than scattered so the next sweep's
# survivors are visibly *new* ones.


def test_a_timed_delegation_is_live_before_its_expiry() -> None:
    live = Narrowing(actor="agent", scope=None, expires_at=1000.0)
    assert not live.expired(999.0)
    assert live.expired(1000.0), "expiry must be inclusive at the boundary"
    assert live.expired(1001.0)


def test_an_untimed_narrowing_never_expires() -> None:
    assert not Narrowing(actor="agent", scope=None).expired(1e12)


def test_member_of_without_a_role_restricts_only_the_organization() -> None:
    limits = member_of("acme")
    assert limits.organizations == frozenset({"acme"})
    assert limits.org_roles is None, (
        "a membership with no role named restricted the roles to nothing, "
        "which would deny every role test"
    )


def test_composing_a_non_facet_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        human(Identity("alice")) | "acme"  # type: ignore[operator]


def test_narrow_defaults_its_clock_to_the_wall_clock() -> None:
    import time as _time

    before = _time.time()
    principal = human(Identity("alice")).narrow(actor="a", scope=ANY_SCOPE, ttl=60)
    assert principal.narrowing is not None
    assert principal.narrowing.expires_at is not None
    assert before + 59 <= principal.narrowing.expires_at <= _time.time() + 61


def test_a_chain_keeps_the_human_even_when_the_parent_did_not_name_one() -> None:
    parent = Narrowing(actor="agent", scope=None, on_behalf_of="")
    child = parent.then(Narrowing(actor="sub", scope=None, on_behalf_of="alice"))
    assert child.on_behalf_of == "alice"

    parent = Narrowing(actor="agent", scope=None, on_behalf_of="alice")
    child = parent.then(Narrowing(actor="sub", scope=None, on_behalf_of=""))
    assert child.on_behalf_of == "alice"


def test_merging_limits_keeps_a_plan_the_right_side_does_not_name() -> None:
    merged = Limits(plan="pro").merge(Limits(organizations=frozenset({"acme"})))
    assert merged.plan == "pro", "merging dropped a plan nobody replaced"
    assert merged.organizations == frozenset({"acme"})

    replaced = Limits(plan="pro").merge(Limits(plan="free"))
    assert replaced.plan == "free"


def test_merging_keeps_an_active_organization_the_right_side_does_not_name() -> None:
    merged = Limits(active_organization="acme").merge(Limits(plan="pro"))
    assert merged.active_organization == "acme"


def test_narrowing_a_scoped_delegation_with_any_scope_keeps_the_bound() -> None:
    parent = human(Identity("alice")).narrow(actor="agent", scope={"read"}, now=0.0)
    child = parent.narrow(actor="subagent", scope=ANY_SCOPE, now=0.0)

    assert child.narrowing is not None
    assert child.narrowing.scope == frozenset({"read"}), (
        "asking for ANY_SCOPE widened a scoped delegation"
    )
    assert not child.narrowing.permits("write")


def test_an_unlimited_side_never_widens_a_limited_one() -> None:
    limited = Limits(entitlements=frozenset({"api"}))
    assert limited.merge(Limits()).entitlements == frozenset({"api"})
    assert Limits().merge(limited).entitlements == frozenset({"api"})


# Every delegated case above ends in a refusal, so `if delegated.allowed` and
# the reason it carries were never read on the arm that matters. These call the
# authorizer directly rather than through a client, because a `403` cannot
# distinguish which of the two passes produced it.


class _FakeState:
    def get(self, key: str, default: Any = None) -> Any:
        return default


class _FakeRequest:
    method = "GET"
    path = "/x"

    def __init__(self, identity: Any) -> None:
        self.identity = identity
        self.state = _FakeState()


async def _authorize(engine: Any, identity: Any, action: str = "read") -> Any:
    from wreath._auth.requirements import PolicyRequirement

    return await CedarAuthorizer(engine=engine).authorize(
        _FakeRequest(identity), PolicyRequirement(action=action, resource='Doc::"r"')
    )


_AGENT = Narrowing(actor="agent", scope=None, on_behalf_of="alice")


@pytest.mark.asyncio
async def test_an_anonymous_request_is_denied_without_consulting_the_engine() -> None:
    asked = []

    class RecordingEngine(CedarPolicies):
        def is_authorized(self, **kwargs: Any) -> Any:
            asked.append(kwargs)
            return super().is_authorized(**kwargs)

    decision = await _authorize(RecordingEngine("permit(principal, action, resource);"), None)
    assert decision.allowed is False
    assert decision.reason == "anonymous"
    assert asked == [], "an anonymous request was put to the engine"


@pytest.mark.asyncio
async def test_a_delegate_the_second_pass_permits_is_allowed() -> None:
    source = (
        "permit(principal, action, resource);\n"
        "permit(principal, action, resource)\nwhen { context.delegated };"
    )
    decision = await _authorize(CedarPolicies(source), Identity("alice", narrowing=_AGENT))
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_a_delegated_refusal_carries_the_reason_the_engine_gave() -> None:
    forbidden = await _authorize(
        CedarPolicies(
            "permit(principal, action, resource);\n"
            "forbid(principal, action, resource)\nwhen { context.delegated };"
        ),
        Identity("alice", narrowing=_AGENT),
    )
    assert forbidden.reason == "explicit forbid"

    unmatched = await _authorize(
        CedarPolicies("permit(principal, action, resource) unless { context.delegated };"),
        Identity("alice", narrowing=_AGENT),
    )
    assert unmatched.reason == "no permit policy matched"


@pytest.mark.asyncio
async def test_a_delegated_refusal_with_no_reason_gets_one() -> None:

    class BareEngine:
        def reads_context(self, key: str) -> bool:
            return key == "delegated"

        def is_authorized(self, **kwargs: Any) -> AuthorizationDecision:
            if kwargs["context"]["delegated"]:
                return AuthorizationDecision(False, None, ("policy-7",))
            return AuthorizationDecision(True, "ok")

    decision = await _authorize(BareEngine(), Identity("alice", narrowing=_AGENT))
    assert decision.allowed is False
    assert decision.reason == "denied to the delegated actor"
    assert decision.diagnostics == ()


@pytest.mark.asyncio
async def test_a_forbid_that_names_only_the_actor_is_not_escaped() -> None:
    source = (
        "permit(principal, action, resource);\n"
        'forbid(principal, action, resource)\nwhen { context.actor == "agent" };'
    )
    assert "delegated" not in source

    decision = await _authorize(CedarPolicies(source), Identity("alice", narrowing=_AGENT))
    assert decision.allowed is False, "the agent escaped a forbid that named it"

    direct = await _authorize(CedarPolicies(source), Identity("alice"))
    assert direct.allowed is True, "the human the forbid does not name was caught by it"
