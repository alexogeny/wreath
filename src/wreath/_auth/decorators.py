"""Authorization decorators that attach compile-time endpoint metadata."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import StrEnum
from math import isfinite
from typing import Any

from .requirements import (
    Mode,
    OAuthStepUpRequirement,
    add_authenticated,
    add_identify,
    add_oauth_step_up,
    add_permissions,
    add_policy,
    add_public,
    add_roles,
    add_second_factor,
)


def public() -> Callable[[Any], Any]:
    """Declare that an endpoint intentionally admits anonymous callers.

    This changes no dispatch behavior. It is the positive declaration consumed
    by `Wreath(require_access_declarations=True)` and the route manifest, so a
    reviewed public endpoint is distinguishable from a forgotten guard.
    """
    return add_public


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
        return {"signed_in": True, "subject": identity.id}
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


def second_factor(*, max_age: float = 300.0) -> Callable[[Any], Any]:
    """Require a second factor proved within the last `max_age` seconds -- step-up.

    Implies `authenticated()`. A caller with no identity is a 401; an identity
    that has not proved a factor recently enough is a **403** whose detail is
    `second_factor_required`, because they were identified and found wanting.
    The remediation is `POST /auth/2fa/verify`, which stamps the session and
    rotates its id, after which the same request succeeds.

    ```python
    @app.delete("/accounts/{account_id}")
    @second_factor(max_age=300)
    async def close_account(request: Request) -> dict:
        ...
    ```

    This is what makes a second factor worth having in a framework that already
    owns authorization, rather than a login-time formality. The freshness lives
    on the route, so a handler never threads a "did they step up?" flag through
    its arguments, and a route that forgets to ask is not a route that quietly
    accepts a session from eight hours ago.

    **An identity that carries no second-factor stamp never satisfies this.** A
    bearer token or an OIDC login has no such stamp, so a route guarded this way
    refuses it rather than treating an absent record as a fresh one. Guard only
    the actions that warrant re-prompting; a whole API behind this is a
    five-minute session with extra steps.

    Args:
        max_age: seconds. Five minutes is the default -- long enough to survive
            a confirmation dialogue, short enough that a walked-away-from
            browser is not still authorized to delete things.

    Returns:
        A decorator that records the requirement on the endpoint.

    Raises:
        ValueError: `max_age` is not positive and finite. A window of zero or
            less can never be satisfied, while a non-finite window cannot
            express recency.
    """
    if isinstance(max_age, bool) or not isinstance(max_age, int | float):
        raise TypeError("second-factor max_age must be a positive finite number")
    if not isfinite(max_age) or max_age <= 0:
        raise ValueError("second-factor max_age must be a positive finite number")

    def decorate(endpoint: Any) -> Any:
        return add_second_factor(endpoint, float(max_age))

    return decorate


def oauth_step_up(
    *,
    max_age: int | None = None,
    acr_values: Iterable[str] = (),
) -> Callable[[Any], Any]:
    """Require OAuth authentication recency or class and emit an RFC 9470 challenge.

    The identity must expose the access token's `auth_time` and `acr` claims in
    `Identity.claims`. A valid token that misses either declared requirement is
    answered with a 401 Bearer `insufficient_user_authentication` challenge,
    including the `max_age` and `acr_values` a client can carry into a new
    authorization request.

    This is deliberately separate from `second_factor()`. That decorator owns
    Wreath's browser-session verification flow and answers 403; this one owns an
    OAuth resource-server challenge and answers 401. Combining them is refused
    when the route is declared because their remediation paths conflict.

    Args:
        max_age: Non-negative whole seconds since the token's active
            authentication event.
        acr_values: Acceptable authentication context class references, in
            preference order. The token's `acr` must equal one of them.

    Returns:
        A decorator that records the OAuth step-up requirement on the endpoint.

    Raises:
        TypeError: `max_age` is not an integer.
        ValueError: neither requirement is present, a value is invalid, or the
            same authentication class appears twice.
    """
    requirement = OAuthStepUpRequirement(max_age=max_age, acr_values=tuple(acr_values))

    def decorate(endpoint: Any) -> Any:
        return add_oauth_step_up(endpoint, requirement)

    return decorate


def authorize(
    *, action: str | StrEnum, resource: object | Callable[[Any], object]
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
    value = action.value if isinstance(action, StrEnum) else action
    if not isinstance(value, str) or not value:
        raise ValueError("authorization action is required and must be a non-empty string")

    def decorate(endpoint: Any) -> Any:
        return add_policy(endpoint, value, resource)

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
    if any(not isinstance(value, str) for value in values):
        raise TypeError(f"{kind} values must be non-empty strings")
    normalized = frozenset(value for value in values if value)
    if not normalized:
        raise ValueError(f"at least one {kind} is required")
    return normalized
