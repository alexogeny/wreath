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
        session = getattr(request.state, "session", None)
        if not isinstance(session, Mapping):
            return None
        principal = session.get(self._session_key)
        if not isinstance(principal, Mapping):
            return None
        subject = principal.get("sub")
        if not isinstance(subject, str) or not subject:
            return None
        # An SSO session used to last the *cookie's* max_age -- 14 days by
        # default -- whatever the identity provider said about the token it was
        # minted from. When the login flow recorded an `exp`, honour it: the
        # provider's answer to "how long is this person signed in" should not be
        # overridden by a cookie setting nobody connected to it.
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
        )

    def challenge(self, request: Request) -> str | None:
        # A browser session has no bearer challenge; the login route is the
        # remediation, so nothing is advertised here.
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
        for backend in self._backends:
            identity = await backend.authenticate(request)
            if identity is not None:
                return identity
        return None

    def challenge(self, request: Request) -> str | None:
        for backend in self._backends:
            value = backend.challenge(request)
            if value is not None:
                return value
        return None
