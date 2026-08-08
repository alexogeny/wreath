"""First-class signed-cookie session policy.

The session is a plain dict on `request.state.session`. It is serialized to
JSON, signed with HMAC-SHA256, and stored client-side in a cookie — nothing
is kept on the server unless a `store` is given. Tampered or expired cookies
yield a fresh empty session; the cookie is only (re)written when the session
content changed:

```python
app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="…")))

@app.get("/visit")
async def visit(request):
    request.state.session["count"] = request.state.session.get("count", 0) + 1
    return {"visits": request.state.session["count"]}
```
Call `rotate_session(request)` whenever the caller's privileges change. Wreath
activates sessions after native ingress and before authentication, and persists
them in a fixed egress slot.
"""

from __future__ import annotations

import hmac
import time
from secrets import token_urlsafe
from typing import Any

from .._b64 import b64url_decode, b64url_encode
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

#: Minimum session-secret length, matching `CsrfPolicy`. 32 bytes is
#: HMAC-SHA256's digest length -- the point past which a longer key adds no
#: strength, and so the floor a shorter one falls below.
MIN_SECRET_BYTES = 32

#: The serialization of an absent/rejected session, so a request that never
#: touches it compares equal and writes no cookie.
_EMPTY_SESSION = _json_dumps({})


class SessionPolicy:
    """Load and persist a per-caller session held in a signed cookie.

    This is an `HttpPolicy` component, never middleware. Wreath loads it in the
    fixed activation slot after native ingress and before authentication,
    including WebSocket authentication. `before` publishes a plain dict on
    `request.state.session`; handlers mutate that dict and fixed egress calls
    `after` to decide what to persist.

    Without a `store` the whole session lives in the cookie. It is serialized to
    JSON, base64url-encoded, and signed with HMAC-SHA256 over the payload and
    its issue time; nothing is kept server-side. A cookie whose signature does
    not verify under any known secret, or whose issue time is more than
    `max_age` seconds old, yields a fresh empty session rather than an error --
    tampering is indistinguishable from a very old cookie, and both mean the
    same thing to the caller. Browsers cap a cookie at about 4 KiB, which is the
    real bound on how much a cookie session can hold; exceed it and the browser
    drops the cookie without telling anyone. Give a `store` to move past that.

    With a `store`, the cookie carries only a signed session id and the contents
    live server-side, which makes a session revocable and unbounded by cookie
    size. A signed id whose row the store no longer has is treated as no session
    at all, because a deleted row *is* a revocation.

    The cookie is written only when the serialized session differs from what
    arrived, so a request that reads without writing sends no `Set-Cookie` and
    does not defeat downstream caching. The comparison is byte-for-byte against
    the exact payload decoded from the cookie: a cookie minted by another
    encoder, or with its keys in another order, reads as changed and is reissued
    with the same content and a fresh signature. A session emptied to `{}` clears
    the cookie, and deletes the stored row when there is one. With a store, an
    unchanged live session has its expiry extended when the store offers a
    `touch`, so a session in constant use does not die at a fixed age.

    `previous_secrets` are accepted for verification but never used for signing,
    so a secret can be rotated without invalidating every live session at once.
    A cookie accepted under a previous secret is always re-signed with the
    current one, which is what lets the old secret eventually be retired.

    Args:
        secret: HMAC key, at least 32 bytes as UTF-8.
        cookie: Cookie name carrying the session or the session id.
        max_age: Session lifetime in seconds, and the cookie `Max-Age`.
        same_site: Cookie `SameSite`, one of strict, lax, or none.
        secure: Mark the cookie `Secure`. Pass False only for local plaintext development.
        http_only: Mark the cookie `HttpOnly`, keeping scripts out of it.
        store: A `SessionStore` moving contents server-side. None keeps them in the cookie.
        previous_secrets: Retired secrets a cookie may still verify under.

    Raises:
        ValueError: `secret` is shorter than 32 bytes.
    """

    __slots__ = (
        "_cookie", "_http_only", "_max_age", "_previous", "_same_site", "_secret",
        "_secure", "_store",
    )

    def __init__(
        self,
        secret: str,
        *,
        cookie: str = "wreath_session",
        max_age: int = 14 * 24 * 3600,
        same_site: str = "lax",
        secure: bool = True,
        http_only: bool = True,
        store: Any = None,
        previous_secrets: Any = (),
    ) -> None:
        if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
            # The same floor `CsrfPolicy` applies. This secret signs the
            # cookie that *is* the session, so a short one is a forgeable
            # session, and "not empty" was not a meaningful bar.
            raise ValueError(
                f"session secret must contain at least {MIN_SECRET_BYTES} bytes"
            )
        self._secret = secret.encode("utf-8")
        # Secrets a cookie may still *verify* under, though nothing is signed
        # with them any more. Without this, rotating the secret invalidated
        # every live session at once -- so the safe operation nobody could
        # afford to perform was the one that limits the damage of a leak.
        self._previous = tuple(
            item.encode("utf-8") if isinstance(item, str) else bytes(item)
            for item in previous_secrets
        )
        self._cookie = cookie
        self._max_age = max_age
        self._same_site = same_site
        self._secure = secure
        self._http_only = http_only
        # `secure` defaults to True, matching `CsrfPolicy`: this cookie *is*
        # the session, so the weaker default belonged to the less sensitive
        # cookie. Pass secure=False for local plaintext development.
        # With a store the cookie carries only a signed session id and the
        # contents live server-side, so a session becomes revocable and is no
        # longer bounded by the 4 KiB a cookie holds. Without one the whole
        # session stays in the cookie, exactly as before.
        self._store = store

    @property
    def schema_owners(self) -> tuple[Any, ...]:
        """The store this policy delegates its tables to, if it has one.

        It owns no tables itself, so it answers with the store it was given
        rather than forwarding a `component()`. Answering at all is the point:
        `Wreath.schema_components` walks policy and asks each holder this
        question, and this class used to expose neither it nor `component()`,
        so a `PostgresSessionStore`'s `wreath_session` table was emitted by
        `wreath schema sql` and created by nothing.

        Empty without a store, which is the cookie-only session and owns no
        tables at all -- not a claim that could not be attributed.
        """
        return () if self._store is None else (self._store,)

    # --- signing -------------------------------------------------------------

    def describe(self):
        """The session cookie, named as this instance configured it."""
        from .base import HeaderSpec, PolicyContract

        return PolicyContract(
            response_headers=(
                (
                    None,
                    HeaderSpec(
                        "Set-Cookie",
                        description=(
                            f"The `{self._cookie}` session cookie, when the "
                            "session changed during this request."
                        ),
                    ),
                ),
            ),
        )

    def _sign(self, payload: bytes, issued_at: int) -> str:
        # `b64url_encode` answers in `str` and the HMAC needs `bytes`, so this
        # trades the stdlib encode/rstrip/decode chain for a native call plus
        # one ascii encode of a short string. That is not obviously a win, so it
        # was measured over the whole `_sign` rather than over the encode:
        # against an A/A floor of 0.15-1.29%, three runs gave 3.6-4.9% on a
        # 94-byte payload, 5.9-7.1% on 142, 16.6-17.1% on 302 and 31.9-32.6% on
        # 1070. The extra encode is real and the chain it replaces is bigger.
        body = b64url_encode(payload)
        stamp = str(issued_at).encode("ascii")
        mac = hmac.new(self._secret, body.encode("ascii") + b"." + stamp, "sha256").hexdigest()
        return f"{body}.{issued_at}.{mac}"

    def _secrets(self) -> tuple[bytes, ...]:
        """Every secret a cookie may verify under, current one first."""
        return (self._secret, *self._previous)

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
            signed = f"{body}.{stamp}".encode("ascii")
            if not any(
                hmac.compare_digest(mac, hmac.new(secret, signed, "sha256").hexdigest())
                for secret in self._secrets()
            ):
                return None
            if int(stamp) + self._max_age < int(time.time()):
                return None
            payload = b64url_decode(body)
            data = _json_loads(payload)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        # A cookie accepted under a *previous* secret must not compare equal to
        # what `after` will serialize, or it would never be re-signed with the
        # current one and the old secret could never actually be retired.
        current = hmac.new(self._secret, signed, "sha256").hexdigest()
        if not hmac.compare_digest(mac, current):
            return (data, b"")
        return (data, payload)

    # --- hooks ----------------------------------------------------------------

    async def before(self, request: Request) -> None:
        """Publish `request.state.session` and the baseline `after` diffs against.

        Always returns None; this hook never short-circuits. A missing, tampered,
        or expired cookie produces an empty session, not an error.
        """
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
            #
            # Byte-for-byte, deliberately: a cookie whose bytes do not round-trip
            # -- one minted by another encoder, or with its keys in another order
            # -- looks changed and is reissued with the same content and a fresh
            # signature. `tests/test_client_sessions_forms.py` pins that.
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
        # Serialize before publishing anything. `_json_dumps` is the one call
        # here that can raise, and doing it first means the four assignments
        # below are plain stores with no failure point between them -- so
        # `after` can never observe a half-initialised session. It used to sit
        # third, which made "session is set but its baseline is not" reachable
        # rather than merely theoretical.
        baseline = _json_dumps(session)
        state = request.state
        state.session = session
        state._session_sid = sid
        state._session_loaded = baseline
        state._session_rotate = False

    async def _after_stored(self, request: Request, response: Any) -> Any:
        state = request.state
        session = state.get("session")
        loaded = state.get("_session_loaded")
        if session is None or loaded is None:
            # Read through `.get` uniformly: every field `before` publishes is
            # optional here, because an `after` whose `before` did not complete
            # must degrade rather than raise. Reading one of the four by
            # attribute made that case an unrelated `AttributeError` 500 instead
            # of a session that is simply not written. `_session_loaded` is
            # always bytes when present, so None is unambiguously "absent".
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

        changed = _json_dumps(session) != loaded
        if not changed and sid is not None and not rotate:
            # Unchanged but live. Saving again would rewrite the row for nothing,
            # but leaving it alone made expiry *absolute*: a session in constant
            # use died at `max_age` from when it was created. A store that can
            # extend a row without rewriting it is asked to; one that cannot is
            # left exactly as before.
            touch = getattr(self._store, "touch", None)
            if touch is not None:
                await touch(sid, self._max_age)
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
            signed = f"{body}.{stamp}".encode("ascii")
            if not any(
                hmac.compare_digest(mac, hmac.new(secret, signed, "sha256").hexdigest())
                for secret in self._secrets()
            ):
                return None
            if int(stamp) + self._max_age < int(time.time()):
                return None
            return b64url_decode(body).decode("ascii")
        except (ValueError, TypeError, UnicodeDecodeError):
            return None

    # --- hooks ----------------------------------------------------------------

    async def after(self, request: Request, response: Any) -> Any:
        """Write, clear, or leave the session cookie, according to what changed.

        Returns the response untouched when the session is byte-identical to
        what arrived, when `before` did not complete, or when the response type
        cannot carry a cookie. An emptied session clears the cookie instead of
        rewriting it.
        """
        if self._store is not None:
            return await self._after_stored(request, response)
        state = request.state
        session = state.get("session")
        loaded = state.get("_session_loaded")
        # Same rule as `_after_stored`: no baseline means `before` did not
        # finish, and a cookie signed against a baseline that does not exist
        # would be a guess about what the caller arrived with.
        if session is None or loaded is None or not hasattr(response, "set_cookie"):
            return response
        serialized = _json_dumps(session)
        if serialized == loaded:
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


__all__ = ["SessionPolicy", "rotate_session"]
