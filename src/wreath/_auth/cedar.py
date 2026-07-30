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
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

from .._native import _core
from .._pure.authz import normalize_authorization_decision as _pure_normalize_decision
from ..request import Request
from .cedar_engine import CedarEntity, EntityUid
from .models import AuthorizationDecision, Identity
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

    __slots__ = ("_action", "_context", "_engine", "_entities", "_principal", "_resource")

    def __init__(
        self,
        *,
        engine: CedarEngine,
        principal: Callable[[Identity], object] = _default_principal,
        action: Callable[[str, Request], object] = _default_action,
        resource: Callable[[object, Request], object] = _default_resource,
        entities: Callable[[Request], object] = _default_entities,
        context: Callable[[Request], Mapping[str, object]] = _default_context,
    ) -> None:
        self._engine = engine
        self._principal = principal
        self._action = action
        self._resource = resource
        self._entities = entities
        self._context = context

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
        raw_resource = requirement.resource
        if callable(raw_resource):
            resolver = cast(Callable[[Request], object], raw_resource)
            raw_resource = await _resolve(resolver(request))
        principal = await _resolve(self._principal(identity))
        action = await _resolve(self._action(requirement.action, request))
        resource = await _resolve(self._resource(raw_resource, request))
        entities = await _resolve(self._entities(request))
        context = await _resolve(self._context(request))
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
