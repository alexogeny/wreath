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
import math
import time
import warnings
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .._awaitable import is_awaitable
from .._native import _core
from .._reqcache import resolve_once
from ..request import Request
from .cedar_engine import CedarEntity, EntityUid
from .facts import EMPTY, SetFact
from .models import AuthorizationDecision, Identity, qualified_identity_value
from .principal import NO_LIMITS, Limits
from .requirements import PolicyRequirement, second_factor_age


@dataclass(frozen=True, slots=True)
class _NativeRoutePlan:
    action_uid: tuple[str, str]
    resource_uid: tuple[str, str]
    context_attributes: frozenset[str] | None
    principal_entity: bool


def _identity_id(identity: Identity) -> str:
    return qualified_identity_value(identity.namespace, identity.id)


def _identity_uid(identity: Identity) -> EntityUid:
    return EntityUid(identity.type, _identity_id(identity))


def _default_principal(identity: Identity) -> object:
    return _identity_uid(identity)


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
    uid = _identity_uid(identity)
    parents = tuple(EntityUid("Role", role) for role in sorted(identity.authority_roles))
    return (CedarEntity(uid, attrs=identity.attributes, parents=parents),)


def _with_entities(
    entities: object,
    additions: object,
    *,
    protected: EntityUid | None = None,
    protected_action: EntityUid | None = None,
) -> object:
    """Add resolved Cedar entities, with the newest definition winning by uid."""
    if additions is None:
        return entities
    if isinstance(additions, CedarEntity):
        additions = (additions,)
    elif not isinstance(additions, Iterable) or isinstance(additions, str | Mapping):
        raise TypeError(
            "resource_entities must return a CedarEntity or an iterable of them; "
            f"got {type(additions).__name__}"
        )
    checked: list[CedarEntity] = []
    for item in additions:
        if not isinstance(item, CedarEntity):
            raise TypeError("resource_entities must contain only CedarEntity instances")
        if protected is not None and item.uid == protected:
            raise ValueError(
                f"resource entities cannot redefine authenticated principal {protected}"
            )
        if protected_action is not None and item.uid == protected_action:
            raise ValueError(
                f"resource entities cannot redefine authorized action {protected_action}"
            )
        checked.append(item)
    if entities is None:
        return tuple(checked)
    if isinstance(entities, CedarEntity):
        held = (entities,)
    elif isinstance(entities, Iterable) and not isinstance(entities, str | Mapping):
        held = tuple(entities)
    else:
        raise TypeError(
            "resolved Cedar resource entities require entities to be None, a CedarEntity, "
            f"or an iterable of CedarEntity instances; got {type(entities).__name__}"
        )
    replaced = {item.uid for item in checked}
    merged = [item for item in held if getattr(item, "uid", None) not in replaced]
    merged.extend(checked)
    return tuple(merged)


_FLAGS_SLOT = "_cedar_fact_flags"

_REGIONS_SLOT = "_cedar_fact_regions"

_NOW_SLOT = "_cedar_context_now"

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

    The bucketing context is `{"id": qualified identity id}`, which is what
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
    return resolve_once(
        request,
        _FLAGS_SLOT,
        lambda: _resolve_flags(request, provider, vocabulary),
    )


def _resolve_flags(
    request: Request, provider: Any, vocabulary: frozenset[str] | None
) -> frozenset[str]:
    """Resolve the flag set, with no caching and no short-circuits.

    The body of `request_flags`, split out so `SetFact` owns the caching rule
    for every fact and this one is not a second copy of it. `request_flags`
    stays as the function it was, sharing the same cache slot.
    """
    from ..flags import FeatureFlags, Flag, _flag_resolver

    provider = _flag_resolver(provider)
    identity = request.identity
    context: dict[str, Any] = {}
    if identity is not None:
        context["id"] = _identity_id(identity)
    if vocabulary is None:
        resolve_all = getattr(provider, "all", None)
        return (
            frozenset(name for name, on in resolve_all(context).items() if on)
            if callable(resolve_all)
            else _NO_FLAGS
        )
    if type(provider) is FeatureFlags:
        return frozenset(name for name in vocabulary if provider.enabled(name, context))
    return frozenset(name for name in vocabulary if provider.resolve(Flag(name, False), context))


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
    return resolve_once(
        request,
        _REGIONS_SLOT,
        lambda: _resolve_regions(request, regions, location, vocabulary),
    )


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
    unknown = sorted(name for name in referenced if name.rsplit(":", 1)[-1].lower() not in known)
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
    limits = getattr(request.identity, "limits", None)
    return NO_LIMITS if limits is None else limits


def _resolve_organizations(
    request: Request, provider: Any, vocabulary: frozenset[str] | None
) -> frozenset[str]:
    """The organisations this caller belongs to, bounded by any claimed limit."""
    resolved = frozenset(member.organization for member in provider.for_request(request))
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
        if member.organization == active:
            names |= member.roles
    return _limits_of(request).bound("org_roles", frozenset(names))


def _resolve_entitlements(
    request: Request, provider: Any, vocabulary: frozenset[str] | None
) -> Any:
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
    resolve_request = getattr(provider, "resolve_request", None)
    resolve = getattr(provider, "resolve", None)
    if callable(resolve_request) or callable(resolve):
        if callable(resolve_request):
            resolution = resolve_request(request)
        elif callable(resolve):
            resolution = resolve(identity)
        else:
            raise RuntimeError("entitlement resolution method disappeared")
        if is_awaitable(resolution):

            async def await_resolution() -> frozenset[str]:
                return _bound_entitlement_resolution(await resolution, limits)

            return await_resolution()
        return _bound_entitlement_resolution(resolution, limits)
    else:
        if limits.plan is not None:
            return EMPTY
        for_request = getattr(provider, "for_request", None)
        held = for_request(request) if callable(for_request) else provider.entitlements(identity)
    resolved = frozenset(str(name) for name in held)
    return limits.bound("entitlements", resolved)


def _bound_entitlement_resolution(resolution: Any, limits: Limits) -> frozenset[str]:
    plan = getattr(resolution, "plan", None)
    if limits.plan is not None and plan != limits.plan:
        return EMPTY
    held = getattr(resolution, "entitlements", EMPTY)
    resolved = frozenset(str(name) for name in held)
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
    return frozenset(provider.for_identity(identity))


def _default_context(request: Request) -> Mapping[str, object]:
    context: dict[str, object] = {"method": request.method, "path": request.path}
    from ..policy.traffic import traffic_class

    context["client_class"] = traffic_class(request)
    # Step-up, expressed where a policy can read it:
    #     permit(principal, action == Action::"delete", resource)
    #     when { context has second_factor_age && context.second_factor_age <= 300 };
    # The key is **absent** rather than a sentinel when the identity has never
    # proved a factor, which is what makes both shapes of policy fail closed: a
    # `when` clause guarded by `has` is false, and an `unless` clause guarded by
    # `has` leaves the forbid standing. A large number would work for the first
    # and quietly invert the second.
    # Seconds as an integer because Cedar has i64 longs and no floats, and *age*
    # rather than the timestamp so a policy is a comparison against a duration
    # the author chose rather than arithmetic against a clock they cannot see.
    age = second_factor_age(request.identity, time.time())
    if age is not None:
        context["second_factor_age"] = age
    return context


def _request_now(request: Any, clock: Callable[[], object]) -> int:
    """The clock's Unix-second value, resolved once for this request."""

    def resolve() -> int:
        value = clock()
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(
                "a Cedar authorization clock must return Unix seconds as an int or float; "
                f"got {type(value).__name__}"
            )
        if not math.isfinite(value):
            raise ValueError("a Cedar authorization clock must return finite Unix seconds")
        return int(value)

    return resolve_once(request, _NOW_SLOT, resolve)


class CedarEngine(Protocol):
    """The policy evaluator `CedarAuthorizer` delegates to.

    One method, and it is a protocol rather than a base class so a different
    Cedar evaluator can be substituted without touching the adapter, the
    decorators, or anything that reads a decision. `CedarPolicies` — the
    built-in engine, parsed in Python and evaluated in C — satisfies it, and is
    what `CedarAuthorizer(engine=...)` is normally handed (Wreath implements Cedar itself).

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
        "_clock",
        "_compiled_route_uids",
        "_delegation_visible",
        "_engine",
        "_entities",
        "_facts",
        "_flag_names",
        "_flags",
        "_location",
        "_native_engine_authorize",
        "_native_engine_prepared",
        "_principal",
        "_region_names",
        "_regions",
        "_reads_now",
        "_resource",
        "_resource_entities",
        "_schema_owners",
    )

    def __init__(
        self,
        *,
        engine: CedarEngine,
        principal: Callable[[Identity], object] = _default_principal,
        action: Callable[[str, Request], object] = _default_action,
        resource: Callable[[object, Request], object] = _default_resource,
        resource_entities: Callable[[object, Request], object] | None = None,
        entities: Callable[[Request], object] = _default_entities,
        context: Callable[[Request], Mapping[str, object]] = _default_context,
        flags: Any = None,
        regions: Any = None,
        location: Callable[[Request], object] = _default_location,
        organizations: Any = None,
        entitlements: Any = None,
        quota: Any = None,
        clock: Callable[[], object] = time.time,
    ) -> None:
        self._engine = engine
        self._principal = principal
        self._action = action
        self._resource = resource
        self._resource_entities = resource_entities
        self._entities = entities
        self._context = context
        self._clock = clock
        self._compiled_route_uids: dict[PolicyRequirement, _NativeRoutePlan] = {}
        if flags is not None:
            from ..flags import _flag_resolver

            flags = _flag_resolver(flags)
        self._flags = flags
        self._regions = regions
        self._location = location
        native_engine_authorize = getattr(engine, "_route_denial", None)
        self._native_engine_authorize = (
            native_engine_authorize
            if callable(native_engine_authorize)
            and resource is _default_resource
            and resource_entities is None
            else None
        )
        native_engine_prepared = getattr(engine, "_route_denial_prepared", None)
        self._native_engine_prepared = (
            native_engine_prepared
            if self._native_engine_authorize is not None and callable(native_engine_prepared)
            else None
        )
        # Every set-valued context key, declared once. Adding a fact is adding a
        # row here -- the caching rule, the laziness rule, the fail-closed empty
        # and the startup validation are the `SetFact`'s, not a sixth copy of
        # four rules that must agree.
        self._facts: tuple[SetFact, ...] = (
            SetFact(
                "flags",
                engine=engine,
                provider=flags,
                resolve=lambda request, vocabulary: _resolve_flags(request, flags, vocabulary),
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
                async_resolve=any(
                    inspect.iscoroutinefunction(getattr(entitlements, name, None))
                    for name in ("resolve_request", "resolve")
                ),
                may_await=True,
            ),
            SetFact(
                "quota",
                engine=engine,
                provider=quota,
                resolve=lambda request, vocabulary: _resolve_quota(request, quota, vocabulary),
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
        self._schema_owners = tuple(getattr(organizations, "schema_owners", ()))
        # Kept for `flags_for`/`regions_for`, which the permission manifest
        # calls, and because the two vocabularies are read in tests.
        self._flag_names = self._facts[0].vocabulary
        self._region_names = self._facts[1].vocabulary
        # Whether any policy can tell a delegated request from a direct one. An
        # engine may prove that it reads none of the delegation fields; an
        # opaque engine cannot make that proof and therefore takes both passes.
        # A request with no delegation never reaches the second pass.
        reads = getattr(engine, "reads_context", None)
        self._delegation_visible = not callable(reads) or bool(
            reads("delegated") or reads("actor") or reads("delegation_depth")
        )
        self._reads_now = None if not callable(reads) else bool(reads("now"))
        _validate_org_roles(self._facts[3].vocabulary, organizations)

    @property
    def schema_owners(self) -> tuple[Any, ...]:
        """Stores delegated through the organisation fact provider."""
        return self._schema_owners

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

    async def _query_base(
        self,
        request: Request,
        identity: Identity,
        action_name: str,
        compiled: _NativeRoutePlan | None = None,
    ) -> tuple[object, object, object, dict[str, object]]:
        """Map the resource-independent half of one or more engine queries."""
        principal = (
            (identity.type, _identity_id(identity))
            if compiled is not None
            else _default_principal(identity)
            if self._principal is _default_principal
            else await _resolve(self._principal(identity))
        )
        action = (
            compiled.action_uid
            if compiled is not None
            else _default_action(action_name, request)
            if self._action is _default_action
            else await _resolve(self._action(action_name, request))
        )
        if compiled is None:
            action_attributes = getattr(self._engine, "context_attributes_for_action", None)
            needed = action_attributes(action) if callable(action_attributes) else None
            principal_entity = getattr(self._engine, "principal_entity_for_action", None)
            needs_principal_entity = not callable(principal_entity) or principal_entity(action)
        else:
            needed = compiled.context_attributes
            needs_principal_entity = compiled.principal_entity
        if self._entities is _default_entities:
            entities = _default_entities(request) if needs_principal_entity else None
        else:
            entities = await _resolve(self._entities(request))
        if self._context is _default_context and needed is not None:
            context: dict[str, object] = {}
            if "method" in needed:
                context["method"] = request.method
            if "path" in needed:
                context["path"] = request.path
            if "client_class" in needed:
                from ..policy.traffic import traffic_class

                context["client_class"] = traffic_class(request)
            if "second_factor_age" in needed:
                age = second_factor_age(identity, time.time())
                if age is not None:
                    context["second_factor_age"] = age
        else:
            context = dict(await _resolve(self._context(request)))
        needs_now = self._reads_now is not False if needed is None else "now" in needed
        if needs_now:
            context["now"] = _request_now(request, self._clock)
        for fact in self._facts:
            if needed is None or fact.attribute in needed:
                value = fact.for_authorization(request)
                context[fact.attribute] = await value if is_awaitable(value) else value
        if compiled is None or needed is None or "delegated" in needed:
            context["delegated"] = False
        return principal, action, entities, context

    async def _authorize_resources(
        self,
        request: Request,
        action_name: str,
        resources: tuple[object, ...],
        *,
        stop_on_denied: bool,
        native: bool = False,
    ) -> tuple[AuthorizationDecision, ...] | object:
        """Evaluate precompiled resources materialized at an internal boundary.

        GraphQL owns its selected resources and decision state natively. This
        method is where those immutable entity ids become Python objects for a
        configurable authorizer; it deliberately takes data rather than a
        GraphQL declaration or schema object.
        """
        if not resources:
            return ()
        identity = request.identity
        if identity is None:
            denied = AuthorizationDecision(False, "anonymous")
            return (denied,) if stop_on_denied else (denied,) * len(resources)
        narrowing = getattr(identity, "narrowing", None)
        if narrowing is not None:
            decisions: list[AuthorizationDecision] = []
            for resource in resources:
                decision = await self.authorize(request, PolicyRequirement(action_name, resource))
                decisions.append(decision)
                if stop_on_denied and not decision.allowed:
                    break
            return tuple(decisions)

        principal, action, base_entities, context = await self._query_base(
            request, identity, action_name
        )
        if (
            self._resource is _default_resource
            and self._resource_entities is None
            and not any(isinstance(resource, CedarEntity) for resource in resources)
        ):
            if native:
                authorize_native = getattr(self._engine, "_is_authorized_many_native", None)
                if callable(authorize_native):
                    return await _resolve(
                        authorize_native(
                            principal=principal,
                            action=action,
                            resources=resources,
                            context=context,
                            entities=base_entities,
                            stop_on_denied=stop_on_denied,
                        )
                    )
            authorize_many = getattr(self._engine, "_is_authorized_many", None)
            if callable(authorize_many):
                results = await _resolve(
                    authorize_many(
                        principal=principal,
                        action=action,
                        resources=resources,
                        context=context,
                        entities=base_entities,
                        stop_on_denied=stop_on_denied,
                    )
                )
                normalize = _core.normalize_authorization_decision
                return tuple(
                    cast(AuthorizationDecision, normalize(result, AuthorizationDecision))
                    for result in results
                )
        decisions = []
        for raw_resource in resources:
            resource_entity = raw_resource if isinstance(raw_resource, CedarEntity) else None
            resource_ref = resource_entity.uid if resource_entity is not None else raw_resource
            resource = await _resolve(self._resource(resource_ref, request))
            protected = principal if isinstance(principal, EntityUid) else None
            protected_action = action if isinstance(action, EntityUid) else None
            entities = _with_entities(
                base_entities,
                resource_entity,
                protected=protected,
                protected_action=protected_action,
            )
            if self._resource_entities is not None:
                additions = await _resolve(self._resource_entities(resource, request))
                entities = _with_entities(
                    entities,
                    additions,
                    protected=protected,
                    protected_action=protected_action,
                )
            decision = await self._evaluate(principal, action, resource, context, entities)
            decisions.append(decision)
            if stop_on_denied and not decision.allowed:
                break
        return tuple(decisions)

    async def _authorize_many(
        self,
        request: Request,
        requirements: tuple[PolicyRequirement, ...],
        *,
        stop_on_denied: bool,
    ) -> tuple[AuthorizationDecision, ...]:
        """Evaluate one action over several resources with one mapping pass."""
        if not requirements:
            return ()
        action_name = requirements[0].action
        if any(requirement.action != action_name for requirement in requirements) or any(
            callable(requirement.resource) for requirement in requirements
        ):
            decisions: list[AuthorizationDecision] = []
            for requirement in requirements:
                decision = await self.authorize(request, requirement)
                decisions.append(decision)
                if stop_on_denied and not decision.allowed:
                    break
            return tuple(decisions)
        return cast(
            tuple[AuthorizationDecision, ...],
            await self._authorize_resources(
                request,
                action_name,
                tuple(requirement.resource for requirement in requirements),
                stop_on_denied=stop_on_denied,
            ),
        )

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
                return AuthorizationDecision(False, "delegation scope does not cover this action")
        raw_resource = requirement.resource
        if callable(raw_resource):
            resolver = cast(Callable[[Request], object], raw_resource)
            raw_resource = await _resolve(resolver(request))
        resource_entity = raw_resource if isinstance(raw_resource, CedarEntity) else None
        if resource_entity is not None:
            raw_resource = resource_entity.uid
        resource = await _resolve(self._resource(raw_resource, request))
        principal, action, entities, context = await self._query_base(
            request, identity, requirement.action
        )
        protected = principal if isinstance(principal, EntityUid) else None
        protected_action = action if isinstance(action, EntityUid) else None
        entities = _with_entities(
            entities,
            resource_entity,
            protected=protected,
            protected_action=protected_action,
        )
        if self._resource_entities is not None:
            additions = await _resolve(self._resource_entities(resource, request))
            entities = _with_entities(
                entities,
                additions,
                protected=protected,
                protected_action=protected_action,
            )
        # Pass one: the delegating principal's own authority, evaluated exactly
        # as if they had made this request themselves.
        decision = await self._evaluate(principal, action, resource, context, entities)
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
        delegated = await self._evaluate(principal, action, resource, delegated_context, entities)
        if delegated.allowed:
            return delegated
        return AuthorizationDecision(False, delegated.reason or "denied to the delegated actor")

    async def _authorize_route(
        self, request: Request, requirement: PolicyRequirement
    ) -> str | None:
        """Return only a route denial, retaining an allowed decision natively."""
        identity = request.identity
        raw_resource = requirement.resource
        if (
            identity is None
            or getattr(identity, "narrowing", None) is not None
            or callable(raw_resource)
            or isinstance(raw_resource, CedarEntity)
            or (isinstance(raw_resource, str) and "::" not in raw_resource)
            or self._native_engine_authorize is None
        ):
            decision = await self.authorize(request, requirement)
            return None if decision.allowed else decision.reason or "Forbidden"
        compiled = self._compiled_route_uids.get(requirement)
        principal, action, entities, context = await self._query_base(
            request, identity, requirement.action, compiled
        )
        if compiled is not None:
            prepared = self._native_engine_prepared
            if prepared is None:
                raise RuntimeError("compiled Cedar route lost its native evaluator")
            return cast(
                str | None,
                await _resolve(
                    prepared(
                        principal=principal,
                        action=compiled.action_uid,
                        resource=compiled.resource_uid,
                        context=context,
                        entities=entities,
                    )
                ),
            )
        return cast(
            str | None,
            await _resolve(
                self._native_engine_authorize(
                    principal=principal,
                    action=action,
                    resource=raw_resource,
                    context=context,
                    entities=entities,
                )
            ),
        )

    def _compile_route_requirement(self, requirement: PolicyRequirement) -> PolicyRequirement:
        """Parse a static native resource once while the route table compiles."""
        resource = requirement.resource
        if (
            self._native_engine_prepared is None
            or callable(resource)
            or isinstance(resource, CedarEntity)
        ):
            return requirement
        compiled_requirement = requirement
        if isinstance(resource, str) and "::" in resource:
            resource = EntityUid.parse(resource)
            compiled_requirement = PolicyRequirement(requirement.action, resource)
        if isinstance(resource, str) and resource.isidentifier():
            return requirement
        if isinstance(resource, str):
            EntityUid.parse(resource)
            return requirement
        if not isinstance(resource, EntityUid):
            raise TypeError(
                "Cedar route resource must be an EntityUid, CedarEntity, "
                f"a 'Type::\"id\"' string, or a callable, got {type(resource).__name__!r}"
            )
        if self._principal is not _default_principal or self._action is not _default_action:
            return compiled_requirement
        action = EntityUid("Action", requirement.action)
        attributes_for = getattr(self._engine, "context_attributes_for_action", None)
        principal_for = getattr(self._engine, "principal_entity_for_action", None)
        plan = _NativeRoutePlan(
            (action.type, action.id),
            (resource.type, resource.id),
            attributes_for(action) if callable(attributes_for) else None,
            not callable(principal_for) or principal_for(action),
        )
        self._compiled_route_uids[compiled_requirement] = plan
        return compiled_requirement

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
        normalize = _core.normalize_authorization_decision
        return cast(AuthorizationDecision, normalize(result, AuthorizationDecision))


async def _resolve(value: Any) -> Any:
    if is_awaitable(value):
        return await value
    return value
