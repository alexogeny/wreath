"""Optional Cedar authorization adapter; Wreath does not bundle a Cedar engine."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

from .._native import _core
from .._pure.authz import normalize_authorization_decision as _pure_normalize_decision
from ..request import Request
from .models import AuthorizationDecision, Identity
from .requirements import PolicyRequirement


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
        principal: Callable[[Identity], object],
        action: Callable[[str, Request], object],
        resource: Callable[[object, Request], object],
        entities: Callable[[Request], object],
        context: Callable[[Request], Mapping[str, object]],
    ) -> None:
        self._engine = engine
        self._principal = principal
        self._action = action
        self._resource = resource
        self._entities = entities
        self._context = context

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
