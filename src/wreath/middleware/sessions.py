"""Signed-cookie sessions, compiled into the middleware tape.

The session is a plain dict on ``request.state.session``. It is serialized to
JSON, signed with HMAC-SHA256, and stored client-side in a cookie — nothing
is kept on the server. Tampered or expired cookies yield a fresh empty
session; the cookie is only (re)written when the session content changed::

    app.add_middleware(SessionMiddleware(secret="…"))

    @app.get("/visit")
    async def visit(request):
        request.state.session["count"] = request.state.session.get("count", 0) + 1
        return {"visits": request.state.session["count"]}
"""

from __future__ import annotations

import base64
import hmac
import time
from secrets import token_urlsafe
from typing import Any

from .._json import dumps as _json_dumps
from .._json import loads as _json_loads
from ..request import Request


def rotate_session(request: Request) -> None:
    """Mint a fresh session id for this request, discarding the old one.

    Call this the moment a caller's privileges change -- on login, and on any
    step-up such as entering an admin area. An attacker who fixed a victim's
    session id beforehand then holds an id that no longer exists.

    A no-op for cookie-backed sessions, which carry no server-side id: there,
    the cookie is re-signed on every content change already.
    """
    request.state._session_rotate = True

#: The serialization of an absent/rejected session, so a request that never
#: touches it compares equal and writes no cookie.
_EMPTY_SESSION = _json_dumps({})


class SessionMiddleware:
    """Hook-based middleware (has ``before``/``after``; tape-fusible)."""

    __slots__ = (
        "_cookie", "_http_only", "_max_age", "_same_site", "_secret", "_secure",
        "_store",
    )

    def __init__(
        self,
        secret: str,
        *,
        cookie: str = "wreath_session",
        max_age: int = 14 * 24 * 3600,
        same_site: str = "lax",
        secure: bool = False,
        http_only: bool = True,
        store: Any = None,
    ) -> None:
        if not secret:
            raise ValueError("session secret must not be empty")
        self._secret = secret.encode("utf-8")
        self._cookie = cookie
        self._max_age = max_age
        self._same_site = same_site
        self._secure = secure
        self._http_only = http_only
        # With a store the cookie carries only a signed session id and the
        # contents live server-side, so a session becomes revocable and is no
        # longer bounded by the 4 KiB a cookie holds. Without one the whole
        # session stays in the cookie, exactly as before.
        self._store = store

    # --- signing -------------------------------------------------------------

    def _sign(self, payload: bytes, issued_at: int) -> str:
        body = base64.urlsafe_b64encode(payload).rstrip(b"=")
        stamp = str(issued_at).encode("ascii")
        mac = hmac.new(self._secret, body + b"." + stamp, "sha256").hexdigest()
        return f"{body.decode('ascii')}.{issued_at}.{mac}"

    def _load(self, value: str) -> tuple[dict[str, Any], bytes] | None:
        """The session and the exact payload bytes it was decoded from.

        Returning the payload lets `before` skip re-serializing what it just
        decoded: those bytes *are* what `_json_dumps` produced when this cookie
        was minted, so `after` can diff against them directly. A cookie whose
        bytes do not round-trip (one minted by another encoder) simply looks
        changed and is reissued -- same content, fresh signature.
        """
        try:
            body, stamp, mac = value.split(".")
            expected = hmac.new(
                self._secret, f"{body}.{stamp}".encode("ascii"), "sha256"
            ).hexdigest()
            if not hmac.compare_digest(mac, expected):
                return None
            if int(stamp) + self._max_age < int(time.time()):
                return None
            padded = body + "=" * (-len(body) % 4)
            payload = base64.urlsafe_b64decode(padded)
            data = _json_loads(payload)
        except (ValueError, TypeError):
            return None
        return (data, payload) if isinstance(data, dict) else None

    # --- hooks ----------------------------------------------------------------

    async def before(self, request: Request) -> None:
        if self._store is not None:
            await self._before_stored(request)
            return None
        loaded = None
        raw = request.cookies.get(self._cookie)
        if raw is not None:
            loaded = self._load(raw)
        if loaded is None:
            session: dict[str, Any] = {}
            baseline = _EMPTY_SESSION
        else:
            # The decoded payload is the serialization to diff against, so a
            # request that does not touch the session pays one JSON pass, not
            # two. `after` still serializes once to detect a change.
            session, baseline = loaded
        request.state.session = session
        request.state._session_loaded = baseline
        return None

    # --- server-side storage --------------------------------------------------

    async def _before_stored(self, request: Request) -> None:
        sid: str | None = None
        session: dict[str, Any] = {}
        raw = request.cookies.get(self._cookie)
        if raw is not None:
            decoded = self._load_sid(raw)
            if decoded is not None:
                stored = await self._store.load(decoded)
                if stored is not None:
                    # Only a session the store still has counts. A deleted row
                    # is a revoked session, even with a valid signed cookie --
                    # that is the whole point of storing server-side.
                    sid, session = decoded, stored
        state = request.state
        state.session = session
        state._session_sid = sid
        state._session_loaded = _json_dumps(session)
        state._session_rotate = False

    async def _after_stored(self, request: Request, response: Any) -> Any:
        state = request.state
        session = getattr(state, "session", None)
        if session is None:
            return response
        sid = state.get("_session_sid")
        rotate = bool(state.get("_session_rotate"))

        if not session:
            # Emptied: drop the row and clear the cookie.
            if sid is not None:
                await self._store.delete(sid)
            if hasattr(response, "delete_cookie"):
                response.delete_cookie(self._cookie)
            return response

        changed = _json_dumps(session) != state._session_loaded
        if sid is not None and rotate:
            # Session fixation: the id a caller arrived with must not survive a
            # privilege change, so the old row goes and a new id is minted.
            await self._store.delete(sid)
            sid = None
        if sid is None:
            sid = token_urlsafe(32)
            changed = True
        if changed:
            await self._store.save(sid, session, self._max_age)
        if changed and hasattr(response, "set_cookie"):
            response.set_cookie(
                self._cookie,
                self._sign(sid.encode("ascii"), int(time.time())),
                max_age=self._max_age,
                httponly=self._http_only,
                secure=self._secure,
                samesite=self._same_site,
            )
        return response

    def _load_sid(self, value: str) -> str | None:
        """The signed session id from a cookie, or None if it does not verify."""
        try:
            body, stamp, mac = value.split(".")
            expected = hmac.new(
                self._secret, f"{body}.{stamp}".encode("ascii"), "sha256"
            ).hexdigest()
            if not hmac.compare_digest(mac, expected):
                return None
            if int(stamp) + self._max_age < int(time.time()):
                return None
            padded = body + "=" * (-len(body) % 4)
            return base64.urlsafe_b64decode(padded).decode("ascii")
        except (ValueError, TypeError, UnicodeDecodeError):
            return None

    # --- hooks ----------------------------------------------------------------

    async def after(self, request: Request, response: Any) -> Any:
        if self._store is not None:
            return await self._after_stored(request, response)
        state = request.state
        session = getattr(state, "session", None)
        if session is None or not hasattr(response, "set_cookie"):
            return response
        serialized = _json_dumps(session)
        if serialized == state._session_loaded:
            return response
        if not session:
            response.delete_cookie(self._cookie)
            return response
        response.set_cookie(
            self._cookie,
            self._sign(serialized, int(time.time())),
            max_age=self._max_age,
            httponly=self._http_only,
            secure=self._secure,
            samesite=self._same_site,
        )
        return response


__all__ = ["SessionMiddleware", "rotate_session"]
