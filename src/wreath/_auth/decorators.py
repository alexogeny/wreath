"""Authorization decorators that attach compile-time endpoint metadata."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .requirements import Mode, add_authenticated, add_permissions, add_policy, add_roles


def authenticated() -> Callable[[Any], Any]:
    """Require an identity, without requiring anything of it.

    Marks the endpoint protected, so the compiled route table classifies its
    path as needing authentication and the backend runs before route selection.
    A request that yields no identity becomes a 401 carrying the backend's
    `challenge()`; one that yields any identity reaches the handler.

    **This is also what populates `request.identity`.** An endpoint with no
    authentication or authorization requirement is not protected, so the backend
    is never asked, and `request.identity` is `None` there even for a caller
    holding a perfectly good token or session. A handler that reads the identity
    must say so -- with this decorator, or with `roles`, `permissions` or
    `authorize`, each of which implies it.

    Returns:
        A decorator that records the requirement on the endpoint.
    """
    return add_authenticated


def authorize(
    *, action: str, resource: object | Callable[[Any], object]
) -> Callable[[Any], Any]:
    """Require a policy decision from the configured authorizer.

    Implies `authenticated()`: the decision needs a principal, so the endpoint is
    protected and a caller with no identity is refused before the policy is ever
    consulted. A policy that denies is a 403, not a 401 -- the caller was
    identified and found wanting, which is a different remediation.

    `action` and the resource type are also the vocabulary `permissions_router`
    and `wreath typegen` read off the routes, so the strings here are the ones a
    UI asks about. There is no second list to keep in step.

    Args:
        action: The action name the policy is asked about, e.g. `"read"`.
        resource: The resource the action applies to. A callable receives the
            request and returns the resource, for a target that is only known
            per request; anything else is used as-is.

    Returns:
        A decorator that records the requirement on the endpoint.

    Raises:
        ValueError: `action` is empty.
    """
    if not action:
        raise ValueError("authorization action is required")

    def decorate(endpoint: Any) -> Any:
        return add_policy(endpoint, action, resource)

    return decorate


def roles(*values: str, mode: Mode = "all") -> Callable[[Any], Any]:
    """Require the identity to carry named roles.

    Implies `authenticated()`. A caller with no identity is a 401; a caller whose
    identity lacks the roles is a 403.

    Args:
        values: Role names. Empty strings are discarded.
        mode: `"all"` (the default) demands every role; `"any"` demands one.

    Returns:
        A decorator that records the requirement on the endpoint.

    Raises:
        ValueError: `mode` is not `"all"` or `"any"`, or no non-empty role was
            given -- a requirement satisfied by everyone is a mistake, not a
            permissive default.
    """
    requirement = _set(values, mode, "role")

    def decorate(endpoint: Any) -> Any:
        return add_roles(endpoint, requirement, mode)

    return decorate


def permissions(*values: str, mode: Mode = "all") -> Callable[[Any], Any]:
    """Require the identity to carry named permissions.

    Implies `authenticated()`. The same 401/403 split as `roles`: no identity is
    a 401, an identity without the permissions is a 403.

    An SSO identity carries permissions as well as roles, so a route guarded this
    way admits the same person whether they arrived with a bearer token or a
    browser session.

    Args:
        values: Permission names. Empty strings are discarded.
        mode: `"all"` (the default) demands every permission; `"any"` demands one.

    Returns:
        A decorator that records the requirement on the endpoint.

    Raises:
        ValueError: `mode` is not `"all"` or `"any"`, or no non-empty permission
            was given.
    """
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
