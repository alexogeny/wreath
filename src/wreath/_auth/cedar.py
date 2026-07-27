"""Cedar authorization: the built-in engine's adapter, open to outside engines.

Wreath bundles its own Cedar engine —
:class:`~wreath._auth.cedar_engine.CedarPolicies`, written in-house with no
dependencies — and ``CedarAuthorizer(engine=CedarPolicies(source))`` is the
whole setup. The :class:`CedarEngine` protocol stays public so a different
evaluator can be swapped in without touching the adapter.

The default mappers model the common case: the principal is the authenticated
identity (``User::"alice"`` from ``Identity.type``/``id``), the action is
``Action::"<name>"``, the resource passes through to the engine, the identity's
roles become ``Role::"..."`` parents so ``principal in Role::"admin"`` works
out of the box, and the context carries the request method and path. Every one
of them can be overridden individually.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

from .._native import _core
from .._pure.authz import normalize_authorization_decision as _pure_normalize_decision
from ..request import Request
from .cedar_engine import CedarEntity, EntityUid
from .models import AuthorizationDecision, Identity
from .requirements import PolicyRequirement


def _default_principal(identity: Identity) -> object:
    return EntityUid(identity.type, identity.id)


def _default_action(action: str, request: Request) -> object:
    # The action name from @authorize(action=...) is the id, verbatim, of an
    # ``Action::`` entity — never parsed, never split. An application whose
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
    return {"method": request.method, "path": request.path}


class CedarEngine(Protocol):
    def is_authorized(
        self,
        *,
        principal: object,
        action: object,
        resource: object,
        context: Mapping[str, object],
        entities: object,
    ) -> object: ...


class CedarAuthorizer:
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
        engine: Any = self._engine
        return engine.fingerprint

    @property
    def source(self) -> object:
        engine: Any = self._engine
        return engine.source

    @property
    def policies(self) -> object:
        engine: Any = self._engine
        return engine.policies

    async def authorize(
        self, request: Request, requirement: PolicyRequirement
    ) -> AuthorizationDecision:
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
