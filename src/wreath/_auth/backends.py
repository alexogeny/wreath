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
    """What `Wreath.configure_auth` asks of an authentication backend.

    A backend answers one question — *who is this request?* — with an
    `Identity` or with nothing. It never decides what that identity may then
    do; that is `AuthorizationProvider`, and they are two protocols because
    keeping the two questions apart is the point.

    The pipeline asks at most once per request and publishes the answer on
    `request.identity`. On a route the compiled table classified as protected
    the backend runs before route selection; on any other route carrying a
    requirement it runs on first need, in the authorization stage. A route
    declaring none of `authenticated`, `identify`, `roles`, `permissions`,
    `second_factor` or `authorize` is not protected at all, so the backend is
    never asked there and `request.identity` stays `None` even for a caller
    holding a perfectly good credential.

    **Refuse by returning `None`, not by raising.** `None` is the only refusal
    channel the pipeline understands: on a route that requires an identity it
    becomes a 401 carrying `challenge`, and on an `identify` route it is simply
    an anonymous caller. An exception is neither — it is not translated into a
    status, and on a protected route under the default `bitset` routing it
    leaves the application without passing the error boundary at all. A backend
    whose credential store is unreachable therefore has to *decide*, and `None`
    is the decision that leaves the route closed.

    Backends are duck-typed: `configure_auth` accepts any object carrying these
    two members and never checks `isinstance`. One convention lives outside the
    protocol and is read with `getattr`: a truthy `requires_session` attribute
    tells route compilation that this backend cannot run until something global
    has published `request.state.session`, and the compile refuses rather than
    answering 401 to a caller holding a valid cookie.

    `BearerTokenBackend`, `SessionIdentityBackend` and `CompositeBackend` ship
    as implementations.
    """

    async def authenticate(self, request: Request) -> Identity | None:
        """Identify `request`, or answer `None` when it carries no valid credential.

        Awaited once per request, before any second-factor, role, permission or
        policy check reads `request.identity`. `None` is a value — "nobody" —
        rather than a failure, and it is also the only way to refuse; see the
        class docstring.
        """
        ...

    def challenge(self, request: Request) -> str | None:
        """The `WWW-Authenticate` value for the 401, or `None` to send no header.

        Consulted only on the refusal path: when `authenticate` yielded nothing
        on a route that required an identity. Synchronous, and must stay so —
        the pipeline does not await it. `BearerTokenBackend` answers `"Bearer"`;
        `SessionIdentityBackend` answers `None`, because the remediation for a
        browser session is the login route rather than a header.
        """
        ...


class AuthorizationProvider(Protocol):
    """What `Wreath.configure_auth` asks of a policy authorizer.

    The other half of the decision. Authentication established *who*; this
    decides whether that principal may perform one declared action on one
    resource. It is consulted only for endpoints carrying `authorize`
    policies — `roles`, `permissions` and `second_factor` are checked by the
    framework itself and never reach a provider.

    Every `authorize` decorator on an endpoint contributes one
    `PolicyRequirement`, and they are asked **in declaration order, and every
    one of them must allow**. The first decision whose `allowed` is false ends
    the request with a 403 whose detail is that decision's `reason` (or
    `"Forbidden"` when it carries none), and the policies after it are not
    asked. There is no mode in which one policy admits a caller another refused.

    A provider runs only after authentication, the second-factor window, the
    role checks and the permission checks have all passed — `authorize` implies
    `authenticated`, so `request.identity` is never `None` by then.
    `CedarAuthorizer` denies an anonymous request anyway rather than relying on
    that.

    **An absent provider refuses, and so does a broken one.** An endpoint
    declaring a policy on an application configured without an authorizer
    raises `RuntimeError` when it is requested, and MCP refuses the call with an
    explanation naming the action; neither admits the caller because nothing was
    installed. A provider that *raises* is not a denial either — it becomes a
    500 through the ordinary error boundary — so a provider that cannot reach
    what it needs should return a denying `AuthorizationDecision` rather than
    let the exception out.
    """

    async def authorize(
        self, request: Request, requirement: PolicyRequirement
    ) -> AuthorizationDecision:
        """Decide `requirement.action` against `requirement.resource` for this request.

        `requirement.resource` is whatever the `authorize` decorator was handed,
        **unresolved**: when the decorator was given a callable it is still that
        callable, and calling it with the request — and awaiting the result if it
        is awaitable — is the provider's job. `CedarAuthorizer` does both.

        Only `allowed` gates the request. `reason` becomes the 403's `detail`,
        so it reaches the client and should be written for one; `diagnostics` is
        carried on the decision for logging and is not sent.
        """
        ...


class BearerTokenBackend:
    """Authenticate `Authorization: Bearer <token>` through a verifier you supply.

    The backend owns the header, and only the header: it finds the field,
    checks the scheme, and hands the token to `verifier`. Deciding whether a
    token is real — a signature check, a database lookup, a call to an identity
    provider — is the verifier's, which is why the same backend serves a JWT
    (`JwtVerifier`), an OIDC provider (`OidcProvider`), and an opaque token in
    your own table.

    ```python
    async def verify(token: str) -> Identity | None:
        user = await lookup(token)
        return Identity(user.id, roles=frozenset(user.roles)) if user else None

    app.configure_auth(BearerTokenBackend(verify))
    ```

    `verifier` may be a coroutine function or a plain one, and either may return
    an awaitable; all three are awaited correctly. Which it is, is detected once
    in the constructor rather than per request. The verifier is handed the token
    and nothing else — a decision that needs the request cannot be made here.

    Every rejection is `None`, and there are four of them: no `authorization`
    header, **more than one**, a value with no space or an empty token, and a
    scheme that is not `Bearer` (matched case-insensitively, per RFC 9110).
    Refusing a repeated header is deliberate rather than defensive —
    `Authorization` is not a list-valued field, and a proxy and the application
    picking first, last or joined would authenticate different values from the
    same request.

    The token is the remainder of the header value after the first space,
    verbatim, and the value is decoded as latin-1 — the encoding ASGI gives
    header bytes, so no byte can fail to decode.
    """

    __slots__ = ("_verifier", "_verifier_is_async")

    def __init__(self, verifier: Verifier) -> None:
        self._verifier = verifier
        # Detected once so the per-request path skips inspect.isawaitable for
        # plain async verifiers (the overwhelmingly common shape).
        self._verifier_is_async = inspect.iscoroutinefunction(verifier)

    async def authenticate(self, request: Request) -> Identity | None:
        """The verifier's answer for this request's bearer token, or `None`.

        `None` for any of the four rejections listed on the class, and for a
        token the verifier itself declined. Whatever the verifier returns is
        returned unchanged — this backend adds no roles, permissions or claims
        of its own.
        """
        value_bytes: bytes | None = None
        for name, candidate in request.headers:
            # ASGI supplies lowercase field names; avoid re-normalizing every
            # header in this authentication scan.
            if name != b"authorization":
                continue
            if value_bytes is not None:
                # Authorization is not a list-valued field. Refusing ambiguity
                # prevents a proxy and the application authenticating different
                # values under first/last/combined interpretations.
                return None
            value_bytes = candidate
        if value_bytes is None:
            return None
        value = value_bytes.decode("latin-1")
        scheme, _separator, token = value.partition(" ")
        if not token or scheme.lower() != "bearer":
            return None
        result = self._verifier(token)
        if self._verifier_is_async:
            return await cast(Awaitable[Identity | None], result)
        if inspect.isawaitable(result):
            return cast(Identity | None, await result)
        return cast(Identity | None, result)

    def challenge(self, request: Request) -> str:
        """Always `"Bearer"` — the `WWW-Authenticate` value for the 401.

        The bare scheme, with no `realm` and no `error` parameter, and it does
        not vary with the request. An application that needs either wraps this
        backend or writes its own.
        """
        return "Bearer"
