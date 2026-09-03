"""Authentication backends for the SSO session bridge.

After an OAuth2 login writes a principal into the signed session, the
`SessionIdentityBackend` turns it back into an `Identity`, so a
browser SSO session and an API bearer token converge on the same Identity shape
— and therefore the same Cedar authorization path. `CompositeBackend`
tries several backends in order (bearer first, then session).
"""

from __future__ import annotations

from collections.abc import Mapping

from ..request import Request
from .backends import AuthenticationBackend
from .models import Identity

__all__ = ["CompositeBackend", "SessionIdentityBackend"]


class SessionIdentityBackend:
    """Yield an Identity from a principal previously stored in the session.

    Reading the session during authentication means the session has to exist by
    then, which route middleware cannot promise. `Wreath` refuses the
    combination at route-compile time rather than answering 401 to a caller
    holding a valid cookie; `requires_session` is what it keys that refusal on.
    """

    #: This backend cannot authenticate until something has published
    #: `request.state.session`, so the session middleware must be global.
    requires_session = True

    __slots__ = ("_session_key",)

    def __init__(self, *, session_key: str = "principal") -> None:
        self._session_key = session_key

    async def authenticate(self, request: Request) -> Identity | None:
        """The session principal as an `Identity`, or `None` for anonymous.

        Reads `request.state.session[session_key]` — `"principal"` by default —
        and requires it to be a mapping carrying a non-empty string `sub`. That
        subject becomes `Identity.id`; `type` defaults to `"User"`; `roles` and
        `permissions` are taken from the principal and stringified into
        frozensets; and the whole principal mapping is copied onto
        `Identity.claims`, which is what carries `second_factor_at` to the
        step-up checks.

        Four things each yield `None` rather than an identity: no session
        published, no principal under the key, a principal with no usable `sub`,
        and a principal marked `pending` — a login still waiting on its second
        factor, which is authenticated but incomplete and must not be an
        identity anywhere. A principal carrying a numeric `exp` that has passed
        also yields `None`, so a signed-in session ends at whichever comes
        first — the provider's expiry or the session cookie's own `max_age` —
        rather than always lasting the cookie's.
        """
        session = getattr(request.state, "session", None)
        if not isinstance(session, Mapping):
            return None
        principal = session.get(self._session_key)
        if not isinstance(principal, Mapping):
            return None
        subject = principal.get("sub")
        if not isinstance(subject, str) or not subject:
            return None
        # A login that is waiting on a second factor is authenticated but
        # *incomplete*, and must not be an identity anywhere. `wreath.users`
        # keeps that marker under its own session key, so this is the second
        # line rather than the first -- but a pending payload that reaches the
        # principal key, by an application copying it or by a future flow
        # writing it in place, is refused here rather than admitting a caller
        # who never proved the second factor.
        if principal.get("pending"):
            return None
        expires = principal.get("exp")
        if isinstance(expires, (int, float)) and not isinstance(expires, bool):
            import time

            if expires <= time.time():
                return None
        roles = principal.get("roles") or ()
        # Permissions as well as roles: a bearer identity carries both, and an
        # SSO identity that dropped them made `@authorize(permissions=...)`
        # refuse the same person a token would have admitted.
        granted = principal.get("permissions") or ()
        return Identity(
            id=subject,
            type=str(principal.get("type", "User")),
            roles=frozenset(str(role) for role in roles),
            permissions=frozenset(str(item) for item in granted),
            claims=dict(principal),
            namespace=str(principal.get("iss") or ""),
        )

    def challenge(self, request: Request) -> str | None:
        """Always `None` — a session-authenticated 401 carries no challenge.

        There is no `WWW-Authenticate` scheme that means "go and log in through
        the browser", and the remediation is the login route rather than a
        header, so nothing is advertised. `Unauthorized(challenge=None)` omits
        the header entirely.
        """
        return None


class CompositeBackend:
    """Try each backend in order; the first Identity wins."""

    __slots__ = ("_backends",)

    def __init__(self, *backends: AuthenticationBackend) -> None:
        if not backends:
            raise ValueError("CompositeBackend requires at least one backend")
        self._backends = backends

    @property
    def requires_session(self) -> bool:
        """True when any wrapped backend needs the session to be published.

        A composite is usually bearer-then-session, and the session half is just
        as unable to run before the session exists as it would be alone -- so the
        requirement propagates rather than being hidden by the wrapper.
        """
        return any(getattr(item, "requires_session", False) for item in self._backends)

    async def authenticate(self, request: Request) -> Identity | None:
        """The first identity any wrapped backend yields, or `None` if none does.

        Each backend is awaited in constructor order and the walk stops at the
        first non-`None` answer, so the backends after it are never asked. A
        backend that raises stops the walk too — the exception propagates rather
        than being treated as that backend declining, because a bearer verifier
        that could not reach its store has not established that this caller is
        a session caller.
        """
        for backend in self._backends:
            identity = await backend.authenticate(request)
            if identity is not None:
                return identity
        return None

    def challenge(self, request: Request) -> str | None:
        """The first non-`None` challenge any wrapped backend offers.

        Asked in the same constructor order, and independently of which backend
        (if any) attempted the authentication — so a bearer-then-session
        composite advertises `Bearer`, which is the one of the two that a
        `WWW-Authenticate` header can express. `None` when no backend offers
        one, and then the 401 carries no challenge header.
        """
        for backend in self._backends:
            value = backend.challenge(request)
            if value is not None:
                return value
        return None
