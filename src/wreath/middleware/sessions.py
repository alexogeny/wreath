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
from typing import Any

from .._json import dumps as _json_dumps
from .._json import loads as _json_loads
from ..request import Request


class SessionMiddleware:
    """Hook-based middleware (has ``before``/``after``; tape-fusible)."""

    __slots__ = ("_cookie", "_http_only", "_max_age", "_same_site", "_secret", "_secure")

    def __init__(
        self,
        secret: str,
        *,
        cookie: str = "wreath_session",
        max_age: int = 14 * 24 * 3600,
        same_site: str = "lax",
        secure: bool = False,
        http_only: bool = True,
    ) -> None:
        if not secret:
            raise ValueError("session secret must not be empty")
        self._secret = secret.encode("utf-8")
        self._cookie = cookie
        self._max_age = max_age
        self._same_site = same_site
        self._secure = secure
        self._http_only = http_only

    # --- signing -------------------------------------------------------------

    def _sign(self, payload: bytes, issued_at: int) -> str:
        body = base64.urlsafe_b64encode(payload).rstrip(b"=")
        stamp = str(issued_at).encode("ascii")
        mac = hmac.new(self._secret, body + b"." + stamp, "sha256").hexdigest()
        return f"{body.decode('ascii')}.{issued_at}.{mac}"

    def _load(self, value: str) -> dict[str, Any] | None:
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
            data = _json_loads(base64.urlsafe_b64decode(padded))
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    # --- hooks ----------------------------------------------------------------

    async def before(self, request: Request) -> None:
        session: dict[str, Any] | None = None
        raw = request.cookies.get(self._cookie)
        if raw is not None:
            session = self._load(raw)
        request.state.session = session if session is not None else {}
        request.state._session_loaded = _json_dumps(request.state.session)
        return None

    async def after(self, request: Request, response: Any) -> Any:
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


__all__ = ["SessionMiddleware"]
