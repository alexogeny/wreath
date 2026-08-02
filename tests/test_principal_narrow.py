"""`narrow` is an intersection, never a union.

The catastrophic bug in delegation does not look like a bug. It looks like a
permission that works: an agent acting for a user is allowed to do something,
the request succeeds, and nobody notices that the user themselves could not have
done it. Set arithmetic on scope strings cannot prevent that, because a scope
string cannot express "only rows this user owns" -- so the guarantee has to come
from somewhere else.

It comes from the shape of the evaluation. `CedarAuthorizer` evaluates the
**delegating principal's own decision as a conjunct**, so

    permitted(narrow(P, N)) = permitted(P) ∩ scope(N) ∩ unexpired(N)

for every policy set, including ones written later and ones that name
`context.delegated` explicitly. The property test in this file is what proves
the implementation matches that claim, and it is deliberately run over
*generated* policy sets rather than hand-picked ones -- including policy sets
written to exploit the union bug, which is the case a hand-written suite would
never think to include.
"""

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
from wreath.authorization import CedarAuthorizer, authorize
from wreath.testing import TestClient

# --- the algebra itself ------------------------------------------------------


def test_narrowing_a_narrowing_intersects_the_scopes() -> None:
    """A sub-agent's authority is derived from its parent's, never a copy."""
    principal = human(Identity("alice"))
    first = principal.narrow(actor="agent", scope={"read", "write"}, now=0.0)
    second = first.narrow(actor="subagent", scope={"write", "delete"}, now=0.0)

    assert second.narrowing is not None
    assert second.narrowing.scope == frozenset({"write"}), (
        "a sub-delegation widened its parent's scope"
    )
    assert second.narrowing.actor == "subagent"
    assert second.narrowing.on_behalf_of == "alice", (
        "the human at the bottom of the chain was lost"
    )
    assert second.narrowing.depth == 2


def test_a_sub_delegation_cannot_extend_the_expiry() -> None:
    principal = human(Identity("alice"))
    first = principal.narrow(actor="agent", scope=ANY_SCOPE, ttl=60, now=1000.0)
    second = first.narrow(actor="subagent", scope=ANY_SCOPE, ttl=6000, now=1000.0)

    assert second.narrowing is not None
    assert second.narrowing.expires_at == 1060.0, "a sub-delegation outlived its parent"


def test_an_untimed_narrowing_does_not_reset_a_timed_parent() -> None:
    """`None` means "carries no expiry of its own", not "expires at zero"...

    ...and also not "never expires". Read as a number it is zero, which would
    make every un-timed narrowing dead on arrival; read as "no bound" it would
    let a child escape its parent's deadline. Neither, so both directions get a
    test.
    """
    timed = human(Identity("alice")).narrow(actor="a", scope=ANY_SCOPE, ttl=60, now=1000.0)
    child = timed.narrow(actor="b", scope=ANY_SCOPE, now=1000.0)
    assert child.narrowing is not None
    assert child.narrowing.expires_at == 1060.0

    untimed = human(Identity("alice")).narrow(actor="a", scope=ANY_SCOPE, now=1000.0)
    grandchild = untimed.narrow(actor="b", scope=ANY_SCOPE, ttl=30, now=1000.0)
    assert grandchild.narrowing is not None
    assert grandchild.narrowing.expires_at == 1030.0


def test_an_empty_scope_permits_nothing() -> None:
    """`scope=set()` is a plausible spelling of "unrestricted" and means the
    opposite. `ANY_SCOPE` is how unrestricted is written, explicitly."""
    empty = Narrowing(actor="agent", scope=frozenset())
    assert not empty.permits("read")

    any_scope = Narrowing(actor="agent", scope=None)
    assert any_scope.permits("read")


def test_narrow_refuses_an_unnamed_actor() -> None:
    with pytest.raises(ValueError, match="non-empty actor"):
        human(Identity("alice")).narrow(actor="", scope=ANY_SCOPE)


def test_narrow_refuses_a_non_positive_ttl() -> None:
    """A zero or negative ttl is dead on arrival, which reads as a caller bug."""
    with pytest.raises(ValueError, match="ttl must be positive"):
        human(Identity("alice")).narrow(actor="a", scope=ANY_SCOPE, ttl=0)


def test_composition_intersects_limits_rather_than_unioning_them() -> None:
    composed = (
        human(Identity("alice"))
        | with_entitlements("export", "api")
        | with_entitlements("api")
    )
    assert composed.limits.entitlements == frozenset({"api"}), (
        "composing two entitlement limits widened the result"
    )


def test_membership_roles_are_namespaced_by_organization() -> None:
    """A bare role name cannot say where it applies; that is the cross-tenant leak."""
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
    """The un-delegated request pays nothing for any of this."""
    identity = Identity("alice")
    assert identity.limits is None
    assert identity.narrowing is None


# --- the property, over generated policy sets --------------------------------

ACTIONS = ("read", "write", "delete")

#: Policy fragments, each parameterised by action. The last two exist to attack
#: the union bug directly: they permit *only* delegated callers, so an
#: implementation that substituted the delegated evaluation for the delegating
#: principal's would grant an agent something its human never had.
FRAGMENTS = (
    'permit(principal, action == Action::"{a}", resource);',
    'permit(principal in Role::"editor", action == Action::"{a}", resource);',
    'permit(principal, action == Action::"{a}", resource)\n'
    "when {{ context.method == \"GET\" }};",
    'permit(principal, action == Action::"{a}", resource)\n'
    "when {{ context.delegated }};",
    'permit(principal, action == Action::"{a}", resource)\n'
    'when {{ context.actor == "agent" }};',
    'forbid(principal, action == Action::"{a}", resource)\n'
    "unless {{ context.delegated }};",
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


async def _decide(
    source: str, action: str, *, narrowing: Any = None, roles: frozenset[str] = frozenset()
) -> bool:
    """Whether one caller may do one action under one policy set."""
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
        response = await client.get(
            f"/{action}", headers={"authorization": "Bearer t"}
        )
    return response.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("source", _policy_sets())
async def test_a_narrowed_principal_never_exceeds_its_delegator(source: str) -> None:
    """permitted(narrow(P, N)) ⊆ permitted(P), for every generated policy set.

    The one test that makes delegation trustworthy. It is a *subset* assertion
    rather than an equality: a narrowing is allowed to take authority away, and
    is only ever wrong when it adds some.
    """
    roles = frozenset({"editor"})
    narrowing = Narrowing(actor="agent", scope=None, on_behalf_of="alice")

    for action in ACTIONS:
        parent = await _decide(source, action, roles=roles)
        child = await _decide(source, action, narrowing=narrowing, roles=roles)
        if child and not parent:
            raise AssertionError(
                f"narrow() granted {action!r} that the delegating principal "
                f"was denied, under:\n{source}"
            )


@pytest.mark.asyncio
async def test_the_property_test_has_teeth() -> None:
    """Falsify the harness: a policy set that permits only delegates must be in
    the corpus, and the corpus must actually reach the delegated branch.

    Without this, the subset assertion above would pass over a corpus where no
    policy could tell a delegate from a human -- proving nothing, and doing it
    silently.
    """
    source = (
        'permit(principal, action == Action::"read", resource)\n'
        "when { context.delegated };"
    )
    assert source in _policy_sets(), "the attack case left the corpus"

    narrowing = Narrowing(actor="agent", scope=None, on_behalf_of="alice")
    assert not await _decide(source, "read"), "the direct caller should be denied"
    assert not await _decide(source, "read", narrowing=narrowing), (
        "the delegate was granted what its delegator was denied -- narrow() "
        "unioned instead of intersecting"
    )


# --- scope and expiry, enforced before the engine ----------------------------


@pytest.mark.asyncio
async def test_an_action_outside_the_scope_is_refused() -> None:
    source = 'permit(principal, action, resource);'
    narrowing = Narrowing(actor="agent", scope=frozenset({"read"}))
    assert await _decide(source, "read", narrowing=narrowing)
    assert not await _decide(source, "write", narrowing=narrowing)


@pytest.mark.asyncio
async def test_an_expired_delegation_is_refused() -> None:
    source = 'permit(principal, action, resource);'
    expired = Narrowing(actor="agent", scope=None, expires_at=1.0)
    assert not await _decide(source, "read", narrowing=expired)


@pytest.mark.asyncio
async def test_scope_is_enforced_even_when_no_policy_mentions_it() -> None:
    """The bound must not depend on a policy author remembering to read it.

    `context.scope` is deliberately not a policy-readable key: scope is checked
    mechanically, before the engine, so a permissive policy set cannot widen it.
    """
    source = 'permit(principal, action, resource);'
    assert "scope" not in source
    narrowing = Narrowing(actor="agent", scope=frozenset({"read"}))
    assert not await _decide(source, "delete", narrowing=narrowing)


@pytest.mark.asyncio
async def test_the_refusal_names_the_reason_it_refused() -> None:
    """Every refusal mentions the action, so asserting on that proves nothing.

    Assert the distinct message text instead -- otherwise this test passes on
    whichever branch fired, including the fallthrough.
    """
    from wreath._auth.requirements import PolicyRequirement

    authorizer = CedarAuthorizer(engine=CedarPolicies('permit(principal, action, resource);'))

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

    out_of_scope = Identity(
        "alice", narrowing=Narrowing(actor="agent", scope=frozenset({"read"}))
    )
    decision = await authorizer.authorize(
        FakeRequest(out_of_scope), PolicyRequirement(action="delete", resource='Doc::"r"')
    )
    assert decision.allowed is False
    assert decision.reason == "delegation scope does not cover this action"

    expired = Identity(
        "alice", narrowing=Narrowing(actor="agent", scope=None, expires_at=1.0)
    )
    decision = await authorizer.authorize(
        FakeRequest(expired), PolicyRequirement(action="read", resource='Doc::"r"')
    )
    assert decision.allowed is False
    assert decision.reason == "delegation expired", (
        "expiry and scope must be distinguishable, or a test cannot tell which fired"
    )


@pytest.mark.asyncio
async def test_a_policy_blind_to_delegation_costs_one_evaluation() -> None:
    """The second pass is skipped when no policy can tell the two apart.

    Not an optimisation for its own sake: it is what keeps delegation free for
    the applications that do not write policies about it. The assertion is on
    the engine's call count, which is the thing that would double.
    """
    calls = []

    class CountingEngine(CedarPolicies):
        def is_authorized(self, **kwargs: Any) -> Any:
            calls.append(kwargs["context"].get("delegated"))
            return super().is_authorized(**kwargs)

    identity = Identity(
        "alice", narrowing=Narrowing(actor="agent", scope=None)
    )
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: identity),
        CedarAuthorizer(engine=CountingEngine('permit(principal, action, resource);')),
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
    """And the second one sees the delegation, or it would be the same query."""
    calls = []

    class CountingEngine(CedarPolicies):
        def is_authorized(self, **kwargs: Any) -> Any:
            calls.append(kwargs["context"].get("delegated"))
            return super().is_authorized(**kwargs)

    source = (
        'permit(principal, action, resource);\n'
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


# --- cases the mutation sweep named ------------------------------------------
#
# Every test below exists because `wreath mutant` removed a control and no test
# objected. They are grouped here rather than scattered so the next sweep's
# survivors are visibly *new* ones.


def test_a_timed_delegation_is_live_before_its_expiry() -> None:
    """The half of expiry that fails *open* if it breaks.

    `expires_at is not None and now >= expires_at` loses nothing visible when
    the second clause is dropped -- every timed delegation simply reads as
    expired, which denies, so 58 tests ran the line and none objected. The
    consequence is that timed delegation would be entirely broken and only a
    test of the *unexpired* case would notice.
    """
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
    """`principal | "acme"` is a mistake worth refusing rather than absorbing."""
    with pytest.raises(TypeError):
        human(Identity("alice")) | "acme"  # type: ignore[operator]


def test_narrow_defaults_its_clock_to_the_wall_clock() -> None:
    """`now=` is injectable for tests; leaving it out must still expire."""
    import time as _time

    before = _time.time()
    principal = human(Identity("alice")).narrow(actor="a", scope=ANY_SCOPE, ttl=60)
    assert principal.narrowing is not None
    assert principal.narrowing.expires_at is not None
    assert before + 59 <= principal.narrowing.expires_at <= _time.time() + 61


def test_a_chain_keeps_the_human_even_when_the_parent_did_not_name_one() -> None:
    """`on_behalf_of` is an `or` over both sides; either alone must carry."""
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
    """`ANY_SCOPE` is the identity element, not a reset.

    A sub-agent asking for "everything my parent has" must receive its parent's
    scope, not an unrestricted one. This is the union bug at its smallest: the
    combining rule's `right is None` branch is the only thing between
    `{"read"} ∩ ANY` and `ANY`, and no other test reached it.
    """
    parent = human(Identity("alice")).narrow(
        actor="agent", scope={"read"}, now=0.0
    )
    child = parent.narrow(actor="subagent", scope=ANY_SCOPE, now=0.0)

    assert child.narrowing is not None
    assert child.narrowing.scope == frozenset({"read"}), (
        "asking for ANY_SCOPE widened a scoped delegation"
    )
    assert not child.narrowing.permits("write")


def test_an_unlimited_side_never_widens_a_limited_one() -> None:
    """The same rule for `Limits`, in both argument orders."""
    limited = Limits(entitlements=frozenset({"api"}))
    assert limited.merge(Limits()).entitlements == frozenset({"api"})
    assert Limits().merge(limited).entitlements == frozenset({"api"})
