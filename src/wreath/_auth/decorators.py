"""Authorization decorators that attach compile-time endpoint metadata."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .requirements import (
    Mode,
    add_authenticated,
    add_identify,
    add_permissions,
    add_policy,
    add_roles,
)


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


def identify() -> Callable[[Any], Any]:
    """Publish `request.identity` without requiring one -- anonymous is a value.

    Runs the authentication backend and sets `request.identity` to whatever it
    yields, including `None`. The route stays **public**: no challenge, no 401,
    and no access clause in the compiled route table, so an anonymous caller
    reaches the handler exactly as it would on a route with no requirement at
    all.

    This is the answer to "who is this, if anyone?" -- the question every
    sign-in-aware page asks on load, and the one `authenticated()` cannot ask
    because it refuses the case it is asking about. Reach for it when a handler
    *renders differently* for a known caller rather than *refusing* an unknown
    one: a console deciding whether to show a sign-in form, a catalogue marking
    the rows you already own, a landing page greeting you by name.

    ```python
    @app.get("/session")
    @identify()
    async def whoami(request: Request) -> dict:
        identity = request.identity
        if identity is None:
            return {"signed_in": False}
        return {"signed_in": True, "subject": identity.subject}
    ```

    The distinction from `authenticated()` is the whole point and is worth
    stating plainly: `authenticated()` means *refuse without an identity*, and
    this means *ask, and accept either answer*. Do not reach for this to make a
    protected route lenient -- a route that needs an identity should say so, so
    that the framework can refuse before the handler runs.

    Returns:
        A decorator that records the requirement on the endpoint.
    """
    return add_identify


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
