"""Authorization decorators that attach compile-time endpoint metadata."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .requirements import Mode, add_authenticated, add_permissions, add_policy, add_roles


def authenticated() -> Callable[[Any], Any]:
    return add_authenticated


def authorize(
    *, action: str, resource: object | Callable[[Any], object]
) -> Callable[[Any], Any]:
    if not action:
        raise ValueError("authorization action is required")

    def decorate(endpoint: Any) -> Any:
        return add_policy(endpoint, action, resource)

    return decorate


def roles(*values: str, mode: Mode = "all") -> Callable[[Any], Any]:
    requirement = _set(values, mode, "role")

    def decorate(endpoint: Any) -> Any:
        return add_roles(endpoint, requirement, mode)

    return decorate


def permissions(*values: str, mode: Mode = "all") -> Callable[[Any], Any]:
    requirement = _set(values, mode, "permission")

    def decorate(endpoint: Any) -> Any:
        return add_permissions(endpoint, requirement, mode)

    return decorate


def _set(values: tuple[str, ...], mode: Mode, kind: str) -> frozenset[str]:
    if mode not in ("all", "any"):
        raise ValueError(f"{kind} mode must be 'all' or 'any'")
    normalized = frozenset(value for value in values if value)
    if not normalized:
        raise ValueError(f"at least one {kind} is required")
    return normalized
