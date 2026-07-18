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
    role_checks: tuple[SetRequirement, ...] = ()
    permission_checks: tuple[SetRequirement, ...] = ()
    policies: tuple[PolicyRequirement, ...] = ()

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
