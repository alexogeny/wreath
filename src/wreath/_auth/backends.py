"""Dependency-free authentication backend contracts and adapters."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Protocol, cast

from ..request import Request
from .models import AuthorizationDecision, Identity
from .requirements import PolicyRequirement

Verifier = Callable[[str], Identity | None | Awaitable[Identity | None]]


class AuthenticationBackend(Protocol):
    async def authenticate(self, request: Request) -> Identity | None: ...

    def challenge(self, request: Request) -> str | None: ...


class AuthorizationProvider(Protocol):
    async def authorize(
        self, request: Request, requirement: PolicyRequirement
    ) -> AuthorizationDecision: ...


class BearerTokenBackend:
    __slots__ = ("_verifier", "_verifier_is_async")

    def __init__(self, verifier: Verifier) -> None:
        self._verifier = verifier
        # Detected once so the per-request path skips inspect.isawaitable for
        # plain async verifiers (the overwhelmingly common shape).
        self._verifier_is_async = inspect.iscoroutinefunction(verifier)

    async def authenticate(self, request: Request) -> Identity | None:
        value = request.header("authorization")
        if value is None:
            return None
        scheme, separator, token = value.partition(" ")
        if not separator or not token or (
            scheme != "Bearer" and scheme.lower() != "bearer"
        ):
            return None
        result = self._verifier(token)
        if self._verifier_is_async:
            return await cast(Awaitable[Identity | None], result)
        if inspect.isawaitable(result):
            return cast(Identity | None, await result)
        return cast(Identity | None, result)

    def challenge(self, request: Request) -> str:
        return "Bearer"
