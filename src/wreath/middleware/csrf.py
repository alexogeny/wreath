"""Signed double-submit CSRF protection for browser cookie authentication."""

from __future__ import annotations

import hmac
import re
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from .._native import _core
from .._webpolicy import origin_matches
from ..request import Request
from ..response import ProblemResponse

# Token minting and validation are accelerated, because they were the most
# expensive hook in the tape: ~11us per request, of which the HMAC itself was
# only ~2.5us. The rest was glue -- an f-string, `.encode()`, and two
# b64encode/rstrip/decode round trips -- and that is what moved into C. The
# digest still comes from `hmac.digest`; see security.c.
if _core is None:
    from .._pure.security import (
        csrf_new_token as _csrf_new_token,
    )
    from .._pure.security import (
        csrf_sign as _csrf_sign,
    )
    from .._pure.security import (
        csrf_validate as _csrf_validate,
    )
else:
    _csrf_sign: Any = _core.csrf_sign
    _csrf_new_token: Any = _core.csrf_new_token
    _csrf_validate: Any = _core.csrf_validate

_TOKEN_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_STATE_TOKEN = "_wreath_csrf_token"
_STATE_ISSUE = "_wreath_csrf_issue"


def _normalize_origin(value: str) -> bytes:
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"invalid trusted origin: {value!r}")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"invalid trusted origin: {value!r}") from error
    default = 80 if scheme == "http" else 443
    authority = host if port is None or port == default else f"{host}:{port}"
    return f"{scheme}://{authority}".encode("ascii")


def _referer_origin(value: str) -> bytes | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    default = 80 if scheme == "http" else 443
    authority = host if port is None or port == default else f"{host}:{port}"
    return f"{scheme}://{authority}".encode("ascii")


def _request_origin(request: Request, headers: dict[bytes, bytes]) -> bytes | None:
    scheme = str(request.scope.get("scheme", "http")).lower()
    if scheme not in {"http", "https"}:
        return None
    host = headers.get(b"host")
    if host is None:
        return None
    try:
        return scheme.encode("ascii") + b"://" + host
    except UnicodeEncodeError:
        return None


def csrf_token(request: Request) -> str:
    """Return the request token prepared by :class:`CSRFMiddleware`."""

    token = request.state.get(_STATE_TOKEN)
    if token is None:
        raise RuntimeError("CSRFMiddleware has not prepared a token for this request")
    return token


class CSRFMiddleware:
    """Protect unsafe browser requests with a signed double-submit token."""

    global_scope = True
    __slots__ = (
        "_cookie_name",
        "_exempt",
        "_header_name",
        "_header_name_bytes",
        "_max_age",
        "_same_site",
        "_secret",
        "_secure",
        "_trusted_origins",
    )

    def __init__(
        self,
        secret: str | bytes,
        *,
        cookie_name: str = "wreath_csrf",
        header_name: str = "x-csrf-token",
        max_age: int = 2 * 60 * 60,
        secure: bool = True,
        same_site: str = "lax",
        trusted_origins: Iterable[str] = (),
        exempt: Callable[[Request], bool] | None = None,
    ) -> None:
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if len(secret_bytes) < 32:
            raise ValueError("CSRF secret must contain at least 32 bytes")
        if not _TOKEN_NAME.fullmatch(cookie_name) or not _TOKEN_NAME.fullmatch(header_name):
            raise ValueError("CSRF cookie and header names must be valid HTTP tokens")
        if max_age <= 0:
            raise ValueError("CSRF max_age must be positive")
        normalized_same_site = same_site.lower()
        if normalized_same_site not in {"strict", "lax", "none"}:
            raise ValueError("same_site must be strict, lax, or none")
        if normalized_same_site == "none" and not secure:
            raise ValueError("SameSite=None requires secure=True")
        self._secret = secret_bytes
        self._cookie_name = cookie_name
        self._header_name = header_name
        self._header_name_bytes = header_name.encode("ascii")
        self._max_age = max_age
        self._secure = secure
        self._same_site = normalized_same_site
        self._trusted_origins = tuple(_normalize_origin(value) for value in trusted_origins)
        self._exempt = exempt

    def _sign(self, issued: int, nonce: str) -> str:
        return _csrf_sign(self._secret, issued, nonce)

    def _new_token(self, now: int) -> str:
        return _csrf_new_token(self._secret, now)

    def _validate(self, token: str, now: int) -> tuple[bool, int]:
        return _csrf_validate(self._secret, token, now, self._max_age)

    def _origin_valid(self, request: Request, headers: dict[bytes, bytes]) -> bool:
        expected = _request_origin(request, headers)
        if expected is None:
            return False
        allowed = (expected, *self._trusted_origins)
        origin = headers.get(b"origin")
        if origin is not None:
            return origin_matches(origin, allowed)
        referer = headers.get(b"referer")
        if referer is not None:
            try:
                referer_text = referer.decode("ascii")
            except UnicodeDecodeError:
                return False
            referer_origin = _referer_origin(referer_text)
            return referer_origin is not None and origin_matches(referer_origin, allowed)
        return not self._secure and request.scope.get("scheme", "http") == "http"

    async def before(self, request: Request):
        now = int(time.time())
        if request.method in _SAFE_METHODS:
            token = request.cookies.get(self._cookie_name)
            valid, issued = self._validate(token, now) if token is not None else (False, 0)
            renew = not valid or now - issued >= self._max_age * 3 // 4
            if renew:
                token = self._new_token(now)
            request.state.__setattr__(_STATE_TOKEN, token)
            request.state.__setattr__(_STATE_ISSUE, renew)
            return None

        try:
            if self._exempt is not None and self._exempt(request):
                return None
        except Exception:
            return ProblemResponse(status=403, title="Forbidden", detail="CSRF validation failed")

        headers = request._index_headers()
        cookie = request.cookies.get(self._cookie_name)
        submitted = headers.get(self._header_name_bytes)
        valid = False
        if (
            cookie is not None
            and submitted is not None
            and hmac.compare_digest(cookie.encode("ascii"), submitted)
        ):
            valid, _issued = self._validate(cookie, now)
        if not valid or not self._origin_valid(request, headers):
            return ProblemResponse(status=403, title="Forbidden", detail="CSRF validation failed")
        request.state.__setattr__(_STATE_TOKEN, cookie)
        request.state.__setattr__(_STATE_ISSUE, False)
        return None

    async def after(self, request: Request, response):
        if not request.state.get(_STATE_ISSUE, False):
            return response
        token = request.state.get(_STATE_TOKEN)
        if token is None:
            return response
        attributes = [
            f"{self._cookie_name}={token}",
            "Path=/",
            f"Max-Age={self._max_age}",
            f"SameSite={self._same_site.capitalize()}",
        ]
        if self._secure:
            attributes.append("Secure")
        response.headers.append((b"set-cookie", "; ".join(attributes).encode("latin-1")))
        return response


__all__ = ["CSRFMiddleware", "csrf_token"]
