"""Cedar authorization: the built-in engine's adapter, open to outside engines.

Wreath bundles its own Cedar engine —
`CedarPolicies`, written in-house with no
dependencies — and `CedarAuthorizer(engine=CedarPolicies(source))` is the
whole setup. The `CedarEngine` protocol stays public so a different
evaluator can be swapped in without touching the adapter.

The default mappers model the common case: the principal is the authenticated
identity (`User::"alice"` from `Identity.type`/`id`), the action is
`Action::"<name>"`, the resource passes through to the engine, the identity's
roles become `Role::"..."` parents so `principal in Role::"admin"` works
out of the box, and the context carries the request method and path — plus
`second_factor_age` when the identity has proved a second factor, so a policy
can insist on a *recent* one. Every one of them can be overridden individually.
"""

from __future__ import annotations

import inspect
import time
import warnings
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

from .._native import _core
from .._pure.authz import normalize_authorization_decision as _pure_normalize_decision
from ..request import Request
from .cedar_engine import CedarEntity, EntityUid
from .facts import EMPTY, SetFact
from .models import AuthorizationDecision, Identity
from .principal import NO_LIMITS, Limits
from .requirements import PolicyRequirement, second_factor_age


def _default_principal(identity: Identity) -> object:
    return EntityUid(identity.type, identity.id)


def _default_action(action: str, request: Request) -> object:
    # The action name from @authorize(action=...) is the id, verbatim, of an
    # `Action::` entity — never parsed, never split. An application whose
    # actions are entities of another type overrides this mapper.
    return EntityUid("Action", action)


def _default_resource(resource: object, request: Request) -> object:
    return resource


def _default_entities(request: Request) -> object:
    identity = request.identity
    if identity is None:
        return ()
    uid = EntityUid(identity.type, identity.id)
    parents = tuple(EntityUid("Role", role) for role in sorted(identity.roles))
    return (CedarEntity(uid, parents=parents),)


#: Where one request's resolved flag set lives for the rest of that request.
#: The same slot `SetFact` derives, so the compatibility function below and the
#: fact share one cache rather than resolving twice for one decision.
_FLAGS_SLOT = "_cedar_fact_flags"

#: The same, for the geofence region set.
_REGIONS_SLOT = "_cedar_fact_regions"

#: The answer for an application with no provider. Shared rather than rebuilt,
#: and *always supplied* -- see `request_flags` for why absence is not an option.
_NO_FLAGS: frozenset[str] = EMPTY


def request_flags(
    request: Request, provider: Any, vocabulary: frozenset[str] | None = frozenset()
) -> frozenset[str]:
    """The enabled feature flags for this request, as a set of names.

    **Resolved once per request and cached on `request.state`.** A route behind
    several policies asks the authorizer once per policy, and a percentage
    rollout re-bucketed per policy could answer differently within one request
    -- a `permit` and a `forbid` disagreeing about whether the same caller is in
    the same rollout is not a decision anybody wrote.

    The bucketing context is `{"id": identity.id}`, which is what
    `flags.flags_dependency` passes, so a percentage flag places a principal in
    the same bucket in a policy as in a handler. An anonymous request buckets
    against the empty string, exactly as it does everywhere else.

    A set of *enabled names*, never a map of name to bool. `context.flags["x"]
    == false` reads as "explicitly off" when it may mean "no such flag", and an
    authorization expression that cannot tell those apart eventually permits
    something by typo. Absent from the set is false, and deny is the safe
    direction.

    **Only the flags the policy set names are resolved.** `vocabulary` is that
    list, read off the engine once at startup; a provider holding fifty flags
    whose policies test three answers three questions. Measured on a served
    request against a fifty-flag provider, resolving all of them cost +21.7us
    (+119%) for on/off values and +56.5us (+310%) for percentages, against a
    0.36us noise floor -- so this is not a micro-optimisation, it is the
    difference between flags being free and being the most expensive thing in
    the authorization phase.

    `vocabulary` of `None` means the policy set reads flags in a shape whose
    names are not knowable (`isEmpty()`, a computed argument), and then every
    flag must be resolved for the answer to be right. That path asks `all()`
    when the provider offers it, and can do nothing when it does not: a
    provider that can neither enumerate nor be enumerated *against* has no
    answer to give, so the set is empty and the policy denies.
    """
    if provider is None:
        return _NO_FLAGS
    state = request.state
    cached = state.get(_FLAGS_SLOT)
    if cached is not None:
        return cast(frozenset[str], cached)
    resolved = _resolve_flags(request, provider, vocabulary)
    state.__setattr__(_FLAGS_SLOT, resolved)
    return resolved


def _resolve_flags(
    request: Request, provider: Any, vocabulary: frozenset[str] | None
) -> frozenset[str]:
    """Resolve the flag set, with no caching and no short-circuits.

    The body of `request_flags`, split out so `SetFact` owns the caching rule
    for every fact and this one is not a second copy of it. `request_flags`
    stays as the function it was, sharing the same cache slot.
    """
    identity = request.identity
    context: dict[str, Any] = {}
    if identity is not None:
        context["id"] = identity.id
    if vocabulary is None:
        resolve_all = getattr(provider, "all", None)
        return (
            frozenset(name for name, on in resolve_all(context).items() if on)
            if callable(resolve_all)
            else _NO_FLAGS
        )
    return frozenset(name for name in vocabulary if provider.enabled(name, context))


def _default_location(request: Request) -> object:
    """Where the caller is, for an application that has not said.

    `None`, deliberately. A position is something the deployment knows and the
    framework cannot guess -- a device fix on the request, a field on the
    identity, an IP lookup -- and inventing one would put a policy's geofence
    on evidence nobody chose. With no location every region set is empty, and
    an empty set denies in both the `when` and the `unless` shape.
    """
    return None


def request_regions(
    request: Request,
    regions: Any,
    location: Callable[[Request], object],
    vocabulary: frozenset[str] | None = frozenset(),
) -> frozenset[str]:
    """The declared regions containing this caller, as a set of names.

    **Resolved once per request and cached on `request.state`**, for the reason
    `request_flags` is: a route behind several policies asks the authorizer once
    per policy, and a caller who moved between two of them would have two
    positions inside one decision. One request, one position, one answer.

    A set of *names*, matching `context.flags`, so a geofence is written with
    the set operations Cedar already has:

        permit (principal in Role::"ranger", action == Action::"read", resource)
        when { context.regions.contains(resource.site) };

    `vocabulary` is the region names the policy set actually tests, read off the
    engine once at startup; `None` means they are not statically knowable and
    every declared region is resolved. That distinction costs more here than it
    does for flags -- a region test is a great-circle distance rather than a
    dictionary lookup -- and the shape above, testing a *resource attribute*,
    is exactly the unknowable case. Keeping the declared region set small is
    therefore the tuning knob, not the policy.
    """
    if regions is None:
        return _NO_FLAGS
    state = request.state
    cached = state.get(_REGIONS_SLOT)
    if cached is not None:
        return cast(frozenset[str], cached)
    resolved = _resolve_regions(request, regions, location, vocabulary)
    state.__setattr__(_REGIONS_SLOT, resolved)
    return resolved


def _resolve_regions(
    request: Request,
    regions: Any,
    location: Callable[[Request], object],
    vocabulary: frozenset[str] | None,
) -> frozenset[str]:
    """Measure the caller's position against the named regions, uncached."""
    where = location(request)
    return (
        _NO_FLAGS
        if where is None
        else regions.containing(where, None if vocabulary is None else vocabulary)
    )


def _validate_org_roles(referenced: frozenset[str] | None, provider: Any) -> None:
    """Refuse, at startup, a policy naming a role nobody declared.

    Split from the generic `validate_names` because the name has two halves with
    opposite natures. `"acme:admin"` carries an organisation id -- a **row**,
    which cannot be enumerated and must not be refused -- and a role -- **
    configuration**, which can be and must. Handing the qualified string to the
    generic validator would compare it against a vocabulary of bare role names
    and refuse every correct policy.

    So only the role half is checked, against the same enumeration surface every
    other fact uses. An unqualified name (the active-organisation reading) is
    checked whole, because that is exactly a bare role.
    """
    if not referenced or provider is None:
        return
    enumerate_names = getattr(provider, "names", None)
    if not callable(enumerate_names):
        warnings.warn(
            "cedar policies reference organization roles "
            f"({', '.join(sorted(referenced))}) but the organization provider "
            f"{type(provider).__name__} cannot enumerate its roles, so they "
            "cannot be checked; a misspelled role will deny silently",
            RuntimeWarning,
            stacklevel=3,
        )
        return
    known = {str(name).lower() for name in enumerate_names()}
    unknown = sorted(
        name for name in referenced if name.rsplit(":", 1)[-1].lower() not in known
    )
    if unknown:
        raise ValueError(
            "cedar policies reference organization roles the provider does not "
            f"declare: {', '.join(unknown)}. A role absent from the provider is "
            "absent from context.org_roles, so the policy would deny forever."
        )


def _limits_of(request: Request) -> Limits:
    """The composed principal's limits, or the shared "no restriction" value.

    Absent means unrestricted, which is what every ordinary request carries, so
    this is a single attribute read on the common path.
    """
    identity = request.identity
    limits = None if identity is None else getattr(identity, "limits", None)
    return NO_LIMITS if limits is None else limits


def _resolve_organizations(
    request: Request, provider: Any, vocabulary: frozenset[str] | None
) -> frozenset[str]:
    """The organisations this caller belongs to, bounded by any claimed limit."""
    resolved = frozenset(
        member.organization for member in provider.for_request(request)
    )
    return _limits_of(request).bound("organizations", resolved)


def _resolve_org_roles(
    request: Request, provider: Any, vocabulary: frozenset[str] | None
) -> frozenset[str]:
    """This caller's roles within their organisations, qualified and active.

    Every membership contributes its **qualified** roles (`"acme:admin"`), which
    is the spelling that cannot leak across tenants. The *active* organisation
    additionally contributes its roles unqualified, so a policy written for a
    single-tenant reading (`context.org_roles.contains("admin")`) means "admin
    of the organisation this request is acting in" rather than "admin of
    anything" -- the reading that is safe if a policy author never thinks about
    tenancy at all.
    """
    from ..organizations import active_organization

    memberships = provider.for_request(request)
    active = active_organization(request)
    names: set[str] = set()
    for member in memberships:
        names |= member.qualified_roles()
        if active is not None and member.organization == active:
            names |= member.roles
    return _limits_of(request).bound("org_roles", frozenset(names))


def _resolve_entitlements(
    request: Request, provider: Any, vocabulary: frozenset[str] | None
) -> frozenset[str]:
    """What this caller's plan entitles them to, bounded by any claimed limit.

    A claimed plan the provider disagrees with yields **nothing**, rather than
    the claimed plan's entitlements. That is the composition law at its most
    tempting to get wrong: `on_plan("pro")` is a restriction to the pro plan's
    entitlements *if the provider agrees the caller is on it*, never an
    assertion that they are.
    """
    identity = request.identity
    if identity is None:
        return EMPTY
    limits = _limits_of(request)
    if limits.plan is not None:
        plan_of = getattr(provider, "plan_for", None)
        if not callable(plan_of) or plan_of(identity) != limits.plan:
            return EMPTY
    resolved = frozenset(str(name) for name in provider.entitlements(identity))
    return limits.bound("entitlements", resolved)


def _resolve_quota(
    request: Request, provider: Any, vocabulary: frozenset[str] | None
) -> frozenset[str]:
    """The degraded states declared for this caller.

    **Never bounded by `Limits`**, and that is the one thing this resolver must
    get right. Every other set fact here is a grant -- an entitlement, a
    membership, a role -- so intersecting it with a delegation's limits can only
    subtract, which is the composition law. These are the opposite shape: a
    policy reads them to *forbid*, so subtracting one grants. A delegated agent
    that could narrow `read_only` out of its own context would have used
    composition to gain a permission its delegator does not have, which is
    exactly what `Limits` exists to make impossible.
    """
    identity = request.identity
    if identity is None:
        return EMPTY
    return provider.for_identity(identity)


def _default_context(request: Request) -> Mapping[str, object]:
    context: dict[str, object] = {"method": request.method, "path": request.path}
    # Step-up, expressed where a policy can read it:
    #
    #     permit(principal, action == Action::"delete", resource)
    #     when { context has second_factor_age && context.second_factor_age <= 300 };
    #
    # The key is **absent** rather than a sentinel when the identity has never
    # proved a factor, which is what makes both shapes of policy fail closed: a
    # `when` clause guarded by `has` is false, and an `unless` clause guarded by
    # `has` leaves the forbid standing. A large number would work for the first
    # and quietly invert the second.
    #
    # Seconds as an integer because Cedar has i64 longs and no floats, and *age*
    # rather than the timestamp so a policy is a comparison against a duration
    # the author chose rather than arithmetic against a clock they cannot see.
    age = second_factor_age(request.identity, time.time())
    if age is not None:
        context["second_factor_age"] = age
    return context


class CedarEngine(Protocol):
    """The policy evaluator `CedarAuthorizer` delegates to.

    One method, and it is a protocol rather than a base class so a different
    Cedar evaluator can be substituted without touching the adapter, the
    decorators, or anything that reads a decision. `CedarPolicies` — the
    built-in engine, parsed in Python and evaluated in C — satisfies it, and is
    what `CedarAuthorizer(engine=...)` is normally handed (ADR 0017).

    Three attributes are *optional* and are read off the authorizer, which
    delegates each straight through to its engine: `fingerprint`, `source`,
    then `policies`, in that order, the first that answers with `bytes` or
    `str` winning. They exist to tag a cached permission manifest by the
    content of the policy set, so a client can revalidate an `ETag` across
    workers and across a restart. An engine offering none of them is entirely
    valid; the manifest then falls back to a per-instance token, which still
    changes when the policy set is replaced but agrees with no other worker.
    """

    def is_authorized(
        self,
        *,
        principal: object,
        action: object,
        resource: object,
        context: Mapping[str, object],
        entities: object,
    ) -> object:
        """Decide one request. Every argument is keyword-only.

        `CedarAuthorizer` produces all five from the request through its
        mappers, awaiting any that come back awaitable, and awaits the result of
        this call too — so an engine may be synchronous or asynchronous.

        The return value is normalized rather than trusted: an
        `AuthorizationDecision` is used as-is; a plain `bool` becomes a decision
        with the reason `"cedar"`; anything else is read for `allowed` and
        `diagnostics` attributes, with `allowed` defaulting to **false** when
        absent. An unrecognized shape therefore denies, and a `reason` on a
        duck-typed result is dropped — only an `AuthorizationDecision` carries
        its reason through to the 403.
        """
        ...


class CedarAuthorizer:
    """The `AuthorizationProvider` that turns a request into a Cedar query.

    This is the adapter, not the engine. It owns the five mappings from a
    Wreath request to Cedar's `(principal, action, resource, context,
    entities)` and hands them to whatever `CedarEngine` it was constructed
    with; the policy language, the evaluation order and the decision belong to
    the engine. Pair it with the built-in one and there is nothing else to
    wire:

    ```python
    engine = CedarPolicies(policy_text)
    app.configure_auth(BearerTokenBackend(verify), CedarAuthorizer(engine=engine))
    ```

    Each mapper is a constructor argument and can be replaced individually; the
    defaults model the common case, and are described in this module's own
    documentation. In short: the principal is `EntityUid(identity.type,
    identity.id)`; the action is `Action::"<name>"` with the decorator's action
    string used verbatim as the id, never parsed or split; the resource passes
    through untouched; the entity set is the caller as one entity whose parents
    are `Role::"..."` for each of its roles, sorted; and the context is the
    request method and path, plus `second_factor_age` in seconds **only when**
    the identity has proved a factor.

    A mapper may be synchronous or asynchronous — anything awaitable one
    returns is awaited before it is passed on.

    There is deliberately no public accessor for the engine. A caller holding it
    could call `is_authorized` directly and bypass every mapper above, which is
    the work this class exists to do; `fingerprint`, `source` and `policies`
    hand out the engine's identifying *values* without handing out the engine.
    """

    __slots__ = (
        "_action",
        "_context",
        "_delegation_visible",
        "_engine",
        "_entities",
        "_facts",
        "_flag_names",
        "_flags",
        "_location",
        "_principal",
        "_region_names",
        "_regions",
        "_resource",
    )

    def __init__(
        self,
        *,
        engine: CedarEngine,
        principal: Callable[[Identity], object] = _default_principal,
        action: Callable[[str, Request], object] = _default_action,
        resource: Callable[[object, Request], object] = _default_resource,
        entities: Callable[[Request], object] = _default_entities,
        context: Callable[[Request], Mapping[str, object]] = _default_context,
        flags: Any = None,
        regions: Any = None,
        location: Callable[[Request], object] = _default_location,
        organizations: Any = None,
        entitlements: Any = None,
        quota: Any = None,
    ) -> None:
        self._engine = engine
        self._principal = principal
        self._action = action
        self._resource = resource
        self._entities = entities
        self._context = context
        self._flags = flags
        self._regions = regions
        self._location = location
        # Every set-valued context key, declared once. Adding a fact is adding a
        # row here -- the caching rule, the laziness rule, the fail-closed empty
        # and the startup validation are the `SetFact`'s, not a sixth copy of
        # four rules that must agree.
        self._facts: tuple[SetFact, ...] = (
            SetFact(
                "flags",
                engine=engine,
                provider=flags,
                resolve=lambda request, vocabulary: _resolve_flags(
                    request, flags, vocabulary
                ),
                noun="feature flags",
                singular="flag",
            ),
            SetFact(
                "regions",
                engine=engine,
                provider=regions,
                resolve=lambda request, vocabulary: _resolve_regions(
                    request, regions, location, vocabulary
                ),
                noun="geofence regions",
                singular="region",
            ),
            SetFact(
                "organizations",
                engine=engine,
                provider=organizations,
                resolve=lambda request, vocabulary: _resolve_organizations(
                    request, organizations, vocabulary
                ),
                noun="organizations",
                singular="organization",
                # An organisation id is a row, not configuration. Refusing to
                # boot because a policy names an organisation that has not been
                # created yet would refuse a correct application.
                validate=False,
            ),
            SetFact(
                "org_roles",
                engine=engine,
                provider=organizations,
                resolve=lambda request, vocabulary: _resolve_org_roles(
                    request, organizations, vocabulary
                ),
                noun="organization roles",
                singular="role",
                validate=False,
            ),
            SetFact(
                "entitlements",
                engine=engine,
                provider=entitlements,
                resolve=lambda request, vocabulary: _resolve_entitlements(
                    request, entitlements, vocabulary
                ),
                noun="entitlements",
                singular="entitlement",
            ),
            SetFact(
                "quota",
                engine=engine,
                provider=quota,
                resolve=lambda request, vocabulary: _resolve_quota(
                    request, quota, vocabulary
                ),
                noun="quota states",
                singular="quota state",
                # The one refusal-shaped fact. A policy reads it to *forbid*, so
                # the empty set an absent provider supplies skips the forbid and
                # allows -- the exact inverse of every grant above, where the
                # empty set denies. `_resolve_quota` already carries this
                # polarity argument for `Limits`; the absent-provider path needs
                # it too, or switching quota off silently stops enforcing it.
                refusal=True,
            ),
        )
        # Kept for `flags_for`/`regions_for`, which the permission manifest
        # calls, and because the two vocabularies are read in tests.
        self._flag_names = self._facts[0].vocabulary
        self._region_names = self._facts[1].vocabulary
        # Whether any policy can tell a delegated request from a direct one. If
        # none can, the second evaluation below is provably identical to the
        # first and is skipped; a request with no delegation never makes it.
        reads = getattr(engine, "reads_context", None)
        self._delegation_visible = bool(
            callable(reads) and (reads("delegated") or reads("actor"))
        )
        _validate_org_roles(self._facts[3].vocabulary, organizations)

    # --- policy identity, delegated from the engine -------------------------
    #
    # A cached permission manifest is tagged by the policy set behind the
    # authorizer, and the tag is found by probing `fingerprint`, `source`, then
    # `policies` (`_auth/permissions.py::_policy_fingerprint`). Those live on
    # the engine, and the tag used to be found by reaching through
    # `authorizer._engine` from that module — a private name owned by this file
    # and read from another one, where a rename would not raise but would
    # quietly drop every ETag to a per-instance token and stop cross-worker
    # revalidation with no error at all.
    #
    # Delegating keeps the private name in the file that owns it, so a rename
    # moves the readers with it, and hands out only the *value*. Deliberately
    # not a public `engine` accessor: the five mappers above are the work this
    # class exists to do, and a caller holding the engine could call
    # `is_authorized` straight past all of them.
    #
    # Absence stays absent. An engine offering none of these lets `AttributeError`
    # out of the property, which `getattr(..., None)` reports as missing, so the
    # probe falls through to a per-instance token exactly as it does for a bare
    # engine. A property that always resolved and usually returned `None` would
    # promise something else — "there is a source, and it is nothing" rather
    # than "there is no source to offer". Each body is a single attribute
    # access, so there is no room for an unrelated `AttributeError` to be
    # swallowed here.

    def flags_for(self, request: Request) -> frozenset[str]:
        """This request's enabled flags, from the same resolution `authorize` uses.

        The permission manifest tags its answer with everything that can change
        it, and once a policy can read `context.flags` that includes the flags.
        Reading them back through the authorizer keeps the provider and the
        vocabulary private to this class, and shares the per-request cache, so
        tagging a manifest costs no extra resolution.
        """
        return self._facts[0].for_request(request)

    def regions_for(self, request: Request) -> frozenset[str]:
        """This request's containing regions, from the resolution `authorize` uses.

        The manifest's counterpart to `flags_for`: once a policy can read
        `context.regions`, a caller's answer changes when they move, and a tag
        that ignored position would let a stale manifest outlive the geofence
        that produced it.
        """
        return self._facts[1].for_request(request)

    def facts_for(self, request: Request) -> dict[str, frozenset[str]]:
        """Every declared set fact for this request, keyed by context attribute.

        What the permission manifest tags with. Adding a fact must change the
        `ETag`, or a manifest outlives the membership or entitlement that
        produced it -- and enumerating them here rather than naming them one by
        one is what stops the next fact from being forgotten.
        """
        return {fact.attribute: fact.for_request(request) for fact in self._facts}

    @property
    def fingerprint(self) -> object:
        """The engine's `fingerprint`, if it has one.

        First of the three tags the permission manifest probes for. Delegated
        verbatim — this class computes nothing.

        Raises:
            AttributeError: the engine offers no `fingerprint`. Absence stays
                absence: the probe reads these with `getattr(..., None)`, so a
                raise is reported as "not offered" and it falls through to the
                next tag. A property that always resolved and returned `None`
                would instead promise that there *is* a fingerprint and that it
                is nothing.
        """
        engine: Any = self._engine
        return engine.fingerprint

    @property
    def source(self) -> object:
        """The engine's `source`, if it has one.

        For `CedarPolicies` this is the policy text the set was parsed from,
        which is why the shipped configuration gets a content-derived manifest
        tag — the same on every worker and across a restart — rather than a
        per-instance one.

        Raises:
            AttributeError: the engine offers no `source`, on the same terms as
                `fingerprint`.
        """
        engine: Any = self._engine
        return engine.source

    @property
    def policies(self) -> object:
        """The engine's `policies`, if it has one.

        Last of the three tags the permission manifest probes for, and used only
        when it answers with `bytes` or `str`.

        Raises:
            AttributeError: the engine offers no `policies`, on the same terms
                as `fingerprint`.
        """
        engine: Any = self._engine
        return engine.policies

    async def authorize(
        self, request: Request, requirement: PolicyRequirement
    ) -> AuthorizationDecision:
        """Map this request into a Cedar query, evaluate it, and normalize the answer.

        **An anonymous request is denied without consulting the engine**, with
        the reason `"anonymous"`. The route pipeline already refuses one before
        reaching here, since `authorize` implies `authenticated`; this is the
        second line, for a caller invoking the authorizer directly.

        Otherwise: a callable `requirement.resource` is called with the request
        and awaited if need be, the five mappers run, and the engine's result
        goes through the shared normalizer — which denies any shape it does not
        recognize. See `CedarEngine.is_authorized` for what the shapes are.
        """
        identity = request.identity
        if identity is None:
            return AuthorizationDecision(False, "anonymous")
        # Delegation, refused before the engine is consulted. Both checks are
        # mechanical rather than policy-expressed, deliberately: a scope bound
        # that only holds when a policy author remembered to read
        # `context.scope` is not a bound. See `_auth.principal` for the law.
        narrowing = getattr(identity, "narrowing", None)
        if narrowing is not None:
            if narrowing.expired(time.time()):
                return AuthorizationDecision(False, "delegation expired")
            if not narrowing.permits(requirement.action):
                return AuthorizationDecision(
                    False, "delegation scope does not cover this action"
                )
        raw_resource = requirement.resource
        if callable(raw_resource):
            resolver = cast(Callable[[Request], object], raw_resource)
            raw_resource = await _resolve(resolver(request))
        principal = await _resolve(self._principal(identity))
        action = await _resolve(self._action(requirement.action, request))
        resource = await _resolve(self._resource(raw_resource, request))
        entities = await _resolve(self._entities(request))
        context = dict(await _resolve(self._context(request)))
        # The authorizer owns this key, and supplies it whether or not a
        # provider was configured. An *absent* `flags` is not a neutral
        # default: `forbid(...) unless { context.flags.contains("bypass") }`
        # against a context with no `flags` at all evaluates to allowed --
        # the forbid is skipped rather than standing -- so an application that
        # never configured a provider, or a custom context mapper that forgot
        # the key, would silently stop forbidding. An empty set denies in both
        # the `when` and the `unless` shape, which is the only fail-closed
        # answer. (`second_factor_age` above reaches the opposite conclusion
        # for the opposite reason: it is guarded by `has`, and a *number* has
        # no value that fails closed in both shapes.)
        context["flags"] = self._facts[0].for_request(request)
        # Supplied unconditionally for the reason `flags` is, and verified for
        # this key rather than assumed to transfer: against a context with no
        # `regions` at all, `forbid(...) unless { context.regions.contains(x) }`
        # evaluates to *allowed* -- the forbid is skipped rather than standing,
        # so an application that never configured regions, or a custom context
        # mapper that dropped the key, would silently stop geofencing. An empty
        # set denies in both the `when` and the `unless` shape.
        context["regions"] = self._facts[1].for_request(request)
        for fact in self._facts[2:]:
            context[fact.attribute] = fact.for_request(request)
        # Supplied as a *bool* rather than left absent, for the reason the empty
        # sets above are supplied: `forbid(...) unless { context.delegated }`
        # against a context with no `delegated` key skips the forbid rather than
        # standing it, so an agent would escape a rule written to catch it. A
        # literal `false` denies in both the `when` and the `unless` shape.
        context["delegated"] = False

        # Pass one: the delegating principal's own authority, evaluated exactly
        # as if they had made this request themselves.
        decision = await self._evaluate(
            principal, action, resource, context, entities
        )
        if narrowing is None or not decision.allowed:
            return decision
        if not self._delegation_visible:
            # No policy can tell the two passes apart, so pass two would put the
            # identical query to the engine and get the identical answer.
            return decision

        # Pass two: the same query, with the delegation visible. **The result is
        # ANDed with pass one, never substituted for it.** That conjunction is
        # what makes `narrow` an intersection for every policy set, including
        # ones written later and ones that permit only delegates -- a `permit`
        # guarded by `context.delegated` still cannot grant a delegate more than
        # the human it acts for, because the human's own decision already had to
        # allow it.
        delegated_context = dict(context)
        delegated_context["delegated"] = True
        delegated_context["actor"] = narrowing.actor
        delegated_context["delegation_depth"] = narrowing.depth
        delegated = await self._evaluate(
            principal, action, resource, delegated_context, entities
        )
        if delegated.allowed:
            return delegated
        return AuthorizationDecision(
            False, delegated.reason or "denied to the delegated actor"
        )

    async def _evaluate(
        self,
        principal: object,
        action: object,
        resource: object,
        context: Mapping[str, object],
        entities: object,
    ) -> AuthorizationDecision:
        """One engine query, normalized. The shape both delegation passes use."""
        result = await _resolve(
            self._engine.is_authorized(
                principal=principal,
                action=action,
                resource=resource,
                context=context,
                entities=entities,
            )
        )
        normalize = (
            _pure_normalize_decision
            if _core is None
            else _core.normalize_authorization_decision
        )
        return cast(AuthorizationDecision, normalize(result, AuthorizationDecision))


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
