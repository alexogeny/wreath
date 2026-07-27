"""Compiled endpoint authorization requirements."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

Mode = Literal["all", "any"]
_METADATA = "__wreath_auth_requirement__"


@dataclass(frozen=True, slots=True)
class SetRequirement:
    values: frozenset[str]
    mode: Mode


@dataclass(frozen=True, slots=True)
class PolicyRequirement:
    action: str
    resource: object | Callable[[Any], object]


@dataclass(frozen=True, slots=True)
class AuthRequirement:
    authenticated: bool = False
    #: Run the backend and publish `request.identity`, but admit an anonymous
    #: caller. Deliberately *not* folded into `authenticated`: that flag means
    #: "refuse without an identity", and this one means "ask, and accept either
    #: answer". Collapsing them would make an identity-reading public route
    #: indistinguishable from a protected one, which is the whole distinction.
    identify: bool = False
    role_checks: tuple[SetRequirement, ...] = ()
    permission_checks: tuple[SetRequirement, ...] = ()
    policies: tuple[PolicyRequirement, ...] = ()

    @property
    def needs_backend(self) -> bool:
        """Whether the authentication backend has to run for this endpoint.

        Both requirements need the backend; only `authenticated` refuses when it
        yields nothing. Dispatch asks this rather than reading `authenticated`
        directly, because an `identify()` route that skipped the backend would
        publish `None` to a handler holding a perfectly good session -- the
        defect this flag exists to fix.
        """
        return self.authenticated or self.identify

    @property
    def access_level(self) -> int:
        if any(check.mode == "all" and check.values == {"admin"} for check in self.role_checks):
            return 2
        if self.authenticated or self.role_checks or self.permission_checks or self.policies:
            return 1
        return 0


def requirement_for(endpoint: Any) -> AuthRequirement:
    return getattr(endpoint, _METADATA, AuthRequirement())


def merge_requirements(*requirements: AuthRequirement) -> AuthRequirement:
    """Combine inherited requirements without allowing a child to weaken a parent."""
    return AuthRequirement(
        authenticated=any(requirement.authenticated for requirement in requirements),
        identify=any(requirement.identify for requirement in requirements),
        role_checks=tuple(
            check for requirement in requirements for check in requirement.role_checks
        ),
        permission_checks=tuple(
            check for requirement in requirements for check in requirement.permission_checks
        ),
        policies=tuple(
            policy for requirement in requirements for policy in requirement.policies
        ),
    )


def set_requirement(endpoint: Any, requirement: AuthRequirement) -> Any:
    setattr(endpoint, _METADATA, requirement)
    return endpoint


def add_authenticated(endpoint: Any) -> Any:
    return set_requirement(endpoint, replace(requirement_for(endpoint), authenticated=True))


def add_identify(endpoint: Any) -> Any:
    return set_requirement(endpoint, replace(requirement_for(endpoint), identify=True))


def add_roles(endpoint: Any, values: frozenset[str], mode: Mode) -> Any:
    current = requirement_for(endpoint)
    return set_requirement(
        endpoint,
        replace(
            current,
            authenticated=True,
            role_checks=current.role_checks + (SetRequirement(values, mode),),
        ),
    )


def add_policy(
    endpoint: Any, action: str, resource: object | Callable[[Any], object]
) -> Any:
    current = requirement_for(endpoint)
    return set_requirement(
        endpoint,
        replace(
            current,
            authenticated=True,
            policies=current.policies + (PolicyRequirement(action, resource),),
        ),
    )


def add_permissions(endpoint: Any, values: frozenset[str], mode: Mode) -> Any:
    current = requirement_for(endpoint)
    return set_requirement(
        endpoint,
        replace(
            current,
            authenticated=True,
            permission_checks=current.permission_checks + (SetRequirement(values, mode),),
        ),
    )
