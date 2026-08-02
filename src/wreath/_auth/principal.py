"""The composed principal: one identity, its limits, and delegation.

Cedar already answers *may this principal do this action to this resource*.
What could not be expressed was the shape a 2026 request actually arrives in:
*an agent acting for a user, who is a member of an organisation with a role, on
a plan with entitlements.* Six pending features each wanted to add one fact to
`{sub, type, roles}`, and six separately-added facts is six identity stories
and an authorization surface nobody can reason about.

This module is the composition, and it has exactly **one law**:

> **Composition never grants.**

Every operation here — `|`, `narrow`, and every facet — can only *reduce* what
a principal may do. A `Limits` set says "at most these"; it can never introduce
a membership the store does not hold or an entitlement the plan does not
include. A `Narrowing` says "at most these actions, until this moment"; the
delegator's own authority is still evaluated and still binds.

That law is what makes the dangerous operation provably safe. `narrow` is an
**intersection, never a union**, and it is an intersection *by construction*
rather than by careful set arithmetic: the authorizer evaluates the delegating
principal's decision as a conjunct, so

    permitted(narrow(P, N)) = permitted(P) ∩ scope(N) ∩ unexpired(N) ⊆ permitted(P)

holds for every policy set, including policy sets written later, including ones
that name `context.delegated` explicitly. A scope string cannot express "only
rows this user owns"; requiring the parent's decision can, because the parent's
decision already did.

The composed principal reaches the authorizer on `Identity`, which grew two
optional fields (`limits`, `narrowing`). Both default to absent, and absent
means "no restriction beyond what the providers say" — the ordinary,
un-delegated request pays nothing for any of this.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = [
    "ANY_SCOPE",
    "Limits",
    "Narrowing",
    "Principal",
    "human",
    "member_of",
    "on_plan",
    "with_entitlements",
]


class _AnyScope:
    """The explicit "this delegation does not restrict actions" sentinel.

    A sentinel rather than `None` as a default, because `scope` is a security
    parameter and a defaultable one gets defaulted. `narrow()` requires it to be
    passed, so "this agent may do anything the user may do" appears in the
    source as `scope=ANY_SCOPE` and survives code review as a decision, rather
    than as a missing argument.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "ANY_SCOPE"


ANY_SCOPE = _AnyScope()

#: What `narrow(scope=...)` accepts.
ScopeArg = Iterable[str] | _AnyScope


def _as_scope(scope: ScopeArg) -> frozenset[str] | None:
    """Normalise a scope argument; `None` is the internal spelling of ANY."""
    if isinstance(scope, _AnyScope):
        return None
    names = frozenset(str(name) for name in scope)
    return names


def _intersect(left: frozenset[str] | None, right: frozenset[str] | None) -> frozenset[str] | None:
    """Intersect two "at most" sets where `None` means "no restriction".

    The identity element is `None`, so an unrestricted side never widens the
    other. This is the only combining rule in this module, and every operation
    that combines two principals routes through it -- there is deliberately no
    union anywhere in this file, so a union cannot appear by accident.
    """
    if left is None:
        return right
    if right is None:
        return left
    return left & right


@dataclass(frozen=True, slots=True)
class Limits:
    """What a composed principal is restricted *to*, never what it is granted.

    Every field is `None` for "not restricted" or a set for "at most these". A
    provider decides what a principal actually has; these narrow that answer and
    can never extend it. An application that composes
    `human(user) | member_of("acme")` for a caller the organisation store does
    not place in `acme` gets an *empty* membership set, not a fabricated one.

    `plan` is the same rule in scalar form: claiming a plan the entitlement
    provider disagrees with yields no entitlements rather than the claimed
    plan's.
    """

    organizations: frozenset[str] | None = None
    org_roles: frozenset[str] | None = None
    entitlements: frozenset[str] | None = None
    active_organization: str | None = None
    plan: str | None = None

    def merge(self, other: Limits) -> Limits:
        """Combine two limits by intersection, never by union.

        `active_organization` and `plan` are scalars, so "combining" them is a
        last-writer-wins on the right-hand side -- composing
        `member_of("a") | member_of("b")` makes `b` active while leaving *both*
        in the membership restriction, which is the reading that cannot grant:
        the active organisation is a focus, and the restriction is the bound.
        """
        return Limits(
            organizations=_intersect(self.organizations, other.organizations),
            org_roles=_intersect(self.org_roles, other.org_roles),
            entitlements=_intersect(self.entitlements, other.entitlements),
            active_organization=(
                other.active_organization
                if other.active_organization is not None
                else self.active_organization
            ),
            plan=other.plan if other.plan is not None else self.plan,
        )

    def bound(self, attribute: str, resolved: frozenset[str]) -> frozenset[str]:
        """Apply the restriction for `attribute` to what a provider resolved.

        The single place a limit meets a provider's answer, so there is one
        place to read when asking whether composition can grant. It cannot: the
        result is a subset of `resolved` for every possible limit.
        """
        limit: frozenset[str] | None = getattr(self, attribute, None)
        return resolved if limit is None else resolved & limit


#: A principal with no restrictions declared. Shared, since it is the answer for
#: every ordinary request and is immutable.
NO_LIMITS = Limits()


@dataclass(frozen=True, slots=True)
class Narrowing:
    """A delegation: who is acting, what they may do, and until when.

    Produced by `Principal.narrow` and carried on `Identity.narrowing`. The
    authorizer refuses an expired or out-of-scope narrowing *before* consulting
    the engine, and evaluates the delegating principal's own decision as a
    conjunct, so this can only ever subtract.

    Args:
        actor: the agent doing the acting. Distinct from the subject, which
            stays the human -- an audit record that cannot name both has not
            recorded a delegation.
        scope: the action names this delegation permits, or `None` for the
            explicit ANY. Matched against the `action=` string a route declared,
            verbatim, because that string is already the id of the `Action::`
            entity and inventing a second spelling here would let the two drift.
        expires_at: epoch seconds after which every decision denies, or `None`
            when the delegation carries no expiry of its own.
        on_behalf_of: the subject at the moment of narrowing, kept so a chain of
            sub-delegations still names the human at the bottom of it.
        depth: how many times this has been narrowed. A sub-agent's token is
            derived from its parent's, never a copy, and the depth is what makes
            "how many hops from a person" answerable in a policy or an audit
            record.
    """

    actor: str
    scope: frozenset[str] | None = None
    expires_at: float | None = None
    on_behalf_of: str = ""
    depth: int = 1

    def expired(self, now: float) -> bool:
        """Whether this delegation has run out, at `now` (epoch seconds)."""
        return self.expires_at is not None and now >= self.expires_at

    def permits(self, action: str) -> bool:
        """Whether `action` is inside this delegation's scope.

        An *empty* scope permits nothing. That is the fail-closed reading and
        the one worth stating: `scope=set()` is a plausible spelling of "no
        restrictions" and it means the opposite here. `ANY_SCOPE` is how the
        unrestricted case is written.
        """
        return self.scope is None or action in self.scope

    def then(self, other: Narrowing) -> Narrowing:
        """Compose a further narrowing onto this one.

        Scope intersects, expiry takes the earlier of the two, the actor becomes
        the innermost one, and the human at the bottom is preserved. A sub-agent
        therefore receives authority derived from its parent's rather than a
        copy of it, which is the mitigation for privilege escalation through an
        agent hierarchy.
        """
        # Written as three branches rather than a `min` over a filtered pair,
        # because `None` here means "carries no expiry of its own" and any
        # arithmetic that reads it as a number reads it as *zero* -- an
        # already-expired delegation, which fails in the safe direction but
        # would make every un-timed narrowing useless and look like a bug in
        # the caller.
        if self.expires_at is None:
            expires = other.expires_at
        elif other.expires_at is None:
            expires = self.expires_at
        else:
            expires = min(self.expires_at, other.expires_at)
        return Narrowing(
            actor=other.actor,
            scope=_intersect(self.scope, other.scope),
            expires_at=expires,
            on_behalf_of=self.on_behalf_of or other.on_behalf_of,
            depth=self.depth + 1,
        )


@dataclass(frozen=True, slots=True)
class Principal:
    """An identity, what it is limited to, and any delegation it arrived under.

    Composed with `|` and narrowed with `narrow`; both return a new value and
    neither can widen. `identity` is what the auth stack carries, so a composed
    principal is installed by handing `principal.identity` to whatever sets
    `request.identity`.

    ```python
    principal = human(user) | member_of("acme", role="admin") | on_plan("pro")
    delegated = principal.narrow(actor=agent_id, scope={"photos:read"}, ttl=300)
    ```
    """

    identity: Any
    limits: Limits = field(default=NO_LIMITS)
    narrowing: Narrowing | None = None

    def __or__(self, facet: Limits) -> Principal:
        """Compose a facet onto this principal, intersecting its limits."""
        if not isinstance(facet, Limits):
            return NotImplemented
        return replace(self, limits=self.limits.merge(facet))

    def narrow(
        self,
        *,
        actor: str,
        scope: ScopeArg,
        ttl: float | None = None,
        now: float | None = None,
    ) -> Principal:
        """Delegate to `actor`, restricted to `scope` and expiring after `ttl`.

        Args:
            actor: the agent that will act. Required and non-empty: a delegation
                nobody can name is one an audit record cannot record.
            scope: the action names permitted, or `ANY_SCOPE`. Required, with no
                default, because a defaulted security parameter gets defaulted.
            ttl: seconds until this delegation expires. `None` means it carries
                no expiry of its own -- which is not the same as unbounded, since
                a narrowing composed onto an already-expiring one keeps the
                earlier expiry.
            now: epoch seconds the ttl is measured from; defaults to the wall
                clock. Injectable so a test can pin it without patching.

        Returns:
            A new `Principal` whose decisions are a subset of this one's.
        """
        if not actor:
            raise ValueError("narrow() requires a non-empty actor")
        if ttl is not None and ttl <= 0:
            raise ValueError(f"narrow() ttl must be positive, got {ttl!r}")
        if now is None:
            import time

            now = time.time()
        subject = getattr(self.identity, "id", "")
        fresh = Narrowing(
            actor=str(actor),
            scope=_as_scope(scope),
            expires_at=None if ttl is None else now + ttl,
            on_behalf_of=str(subject),
            depth=1,
        )
        combined = fresh if self.narrowing is None else self.narrowing.then(fresh)
        return replace(self, narrowing=combined)

    def bind(self) -> Any:
        """The `Identity` to install on a request, carrying limits and narrowing."""
        return replace(self.identity, limits=self.limits, narrowing=self.narrowing)


def human(identity: Any) -> Principal:
    """Start a composition from an authenticated identity."""
    return Principal(identity=identity)


def member_of(organization: str, *, role: str | None = None) -> Limits:
    """Restrict to one organisation, optionally to one role within it.

    The role is namespaced by the organisation (`"acme:admin"`), because a role
    name alone cannot say *where* it applies and an admin of one tenant is not
    an admin of another. That spelling is what `context.org_roles` carries.
    """
    org = str(organization)
    return Limits(
        organizations=frozenset({org}),
        org_roles=None if role is None else frozenset({f"{org}:{role}"}),
        active_organization=org,
    )


def on_plan(name: str) -> Limits:
    """Restrict to the entitlements of one plan.

    Claiming a plan the entitlement provider disagrees with yields *no*
    entitlements rather than the claimed plan's, which is the composition law in
    its most tempting-to-get-wrong form.
    """
    return Limits(plan=str(name))


def with_entitlements(*names: str) -> Limits:
    """Restrict to at most these entitlements, whatever the plan grants."""
    return Limits(entitlements=frozenset(str(name) for name in names))
