"""Signed double-submit CSRF protection for browser cookie authentication.

Needed wherever the browser attaches credentials on its own -- a session cookie,
HTTP basic auth -- because then a cross-site form post carries them too. An API
authenticated by a bearer token the client has to attach deliberately is not
exposed to this and does not need the middleware:

```python
app.add_middleware(CSRFMiddleware(secret=SECRET, trusted_hosts=["app.example"]))

@app.get("/whoami")
async def whoami(request):
    return {"csrf": csrf_token(request)}
```
The resubmitted token is read from a request *header* only -- `x-csrf-token` by
default. A plain HTML form post cannot carry one, so this suits a script client
that reads the cookie or calls `csrf_token` and sets the header itself.

Mount `ProxyHeadersMiddleware` ahead of this one behind a TLS-terminating proxy;
see `CSRFMiddleware` for why the origin check depends on it.
"""

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
    # `request.scheme` reads the native context member directly; going through
    # `request.scope` materialized the whole lazy scope dict per unsafe request.
    scheme = str(request.scheme).lower()
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
    """Return the CSRF token `CSRFMiddleware` prepared for this request.

    This is the value a client sends back in the configured request header,
    `x-csrf-token` by default. It is the same value that goes into the cookie,
    which is what makes the pair a double submit.

    A token is prepared for every safe-method request and for every unsafe one
    that passed validation. It is *not* prepared for a request the `exempt`
    predicate excused, which never ran validation and has nothing to hand back.

    Returns:
        The signed token string, shaped `v1.issued.nonce.mac`.

    Raises:
        RuntimeError: No token was prepared for this request.
    """

    token = request.state.get(_STATE_TOKEN)
    if token is None:
        raise RuntimeError("CSRFMiddleware has not prepared a token for this request")
    return token


class CSRFMiddleware:
    """Protect unsafe browser requests with a signed double-submit token.

    Global middleware. `GET`, `HEAD`, and `OPTIONS` are treated as safe and only
    have a token prepared for them; every other method is validated, including
    ones a route does not implement.

    Validation is two independent checks, and both must pass. The double submit:
    the request carries the token in the configured header -- a header, never a
    form field, so a classic HTML form post cannot satisfy it -- and in the cookie,
    the two are byte-equal under a constant-time compare, and the cookie's own
    HMAC signature verifies and is within `max_age`. The origin check: the
    request's `Origin` header, or failing that the origin parsed out of
    `Referer`, matches the origin built from the request scheme and `Host`, or
    one of `trusted_origins`. A request with neither header is refused unless
    `allow_missing_origin` says otherwise.

    A failure is a 403 `application/problem+json` document titled `Forbidden`
    with detail `CSRF validation failed`. Nothing distinguishes which of the two
    checks failed, deliberately.

    The token reaches the application through `csrf_token(request)` and the
    browser through the cookie, which `after` writes whenever the token was
    minted or renewed. Renewal happens once a token is three quarters of the way
    through `max_age`, on unsafe methods as well as safe ones -- a client that
    only ever POSTs never takes the safe path, and would otherwise watch its
    token expire with nothing to refresh it.

    The cookie is written with `Path=/`, `Max-Age`, `SameSite`, and `Secure`
    when configured. It is deliberately **not** `HttpOnly`, because a script
    client has to read it to put it in the header; the signature, not
    inaccessibility, is what makes it unforgeable.

    The expected origin is built from the client-supplied `Host` header, so
    something must constrain that. Naming `trusted_hosts` here does it and keeps
    this middleware self-contained; leaving it empty means
    `TrustedHostMiddleware` must be mounted instead. Behind a TLS-terminating
    proxy, `ProxyHeadersMiddleware` must run first or the expected origin is
    built as `http://host` while the browser sends `https://host`, and every
    unsafe request is refused.

    `exempt_errors` counts the times the `exempt` predicate raised. Each of
    those requests was refused -- failing closed is the only defensible
    direction -- but a broken predicate refuses everything forever and looks
    exactly like a site under attack, so the count is how the two are told apart.

    Args:
        secret: HMAC key, at least 32 bytes. A str is encoded as UTF-8.
        cookie_name: Cookie the token is written to and read from.
        header_name: Request header the resubmitted token is read from.
        max_age: Token lifetime in seconds, and the cookie `Max-Age`.
        secure: Mark the cookie `Secure`. Leave True outside local plaintext development.
        same_site: Cookie `SameSite`, one of strict, lax, or none.
        trusted_origins: Extra origins accepted besides the request's own.
        trusted_hosts: Host values accepted. Empty defers to `TrustedHostMiddleware`.
        exempt: Predicate excusing an unsafe request. Raising refuses it.
        allow_missing_origin: Accept an unsafe request carrying no Origin and no Referer.

    Raises:
        ValueError: The secret is under 32 bytes.
        ValueError: `cookie_name` or `header_name` is not a valid HTTP token.
        ValueError: A `__Host-` or `__Secure-` cookie name was given without `secure`.
        ValueError: `max_age` is not positive.
        ValueError: `same_site` is not strict, lax, or none.
        ValueError: `same_site` is none without `secure`.
    """

    global_scope = True
    __slots__ = (
        "_cookie_name",
        "_exempt",
        "exempt_errors",
        "_header_name",
        "_header_name_bytes",
        "_max_age",
        "_same_site",
        "_secret",
        "_secure",
        "_trusted_hosts",
        "_trusted_origins",
        "_allow_missing_origin",
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
        trusted_hosts: Iterable[str] = (),
        exempt: Callable[[Request], bool] | None = None,
        allow_missing_origin: bool = False,
    ) -> None:
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if len(secret_bytes) < 32:
            raise ValueError("CSRF secret must contain at least 32 bytes")
        if not _TOKEN_NAME.fullmatch(cookie_name) or not _TOKEN_NAME.fullmatch(header_name):
            raise ValueError("CSRF cookie and header names must be valid HTTP tokens")
        # The browser enforces these prefixes by *dropping* a cookie that does
        # not meet them, so a mismatch here is a CSRF cookie that silently never
        # arrives (RFC 6265bis 4.1.3).
        if cookie_name.startswith("__Host-") and not secure:
            raise ValueError("a __Host- CSRF cookie must be Secure; pass secure=True")
        if cookie_name.startswith("__Secure-") and not secure:
            raise ValueError("a __Secure- CSRF cookie must be Secure; pass secure=True")
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
        #: Times the `exempt` predicate raised. Each one was refused, so this is
        #: not a security hole -- it is how you find out the predicate itself is
        #: broken, which otherwise looks exactly like traffic that deserved a
        #: 403 and is indistinguishable from working correctly.
        self.exempt_errors = 0
        self._allow_missing_origin = allow_missing_origin
        # The expected origin is built from the `Host` header, which is the
        # client's to set. That is safe only if something validates it, and the
        # something was `TrustedHostMiddleware` -- a dependency between two
        # middlewares that nothing stated and nothing enforced. Naming the hosts
        # here makes this middleware self-contained; leaving it empty keeps the
        # previous behaviour, and the guide says to mount TrustedHostMiddleware.
        self._trusted_hosts = frozenset(host.lower() for host in trusted_hosts)

    def _sign(self, issued: int, nonce: str) -> str:
        return _csrf_sign(self._secret, issued, nonce)

    def _new_token(self, now: int) -> str:
        return _csrf_new_token(self._secret, now)

    def _validate(self, token: str, now: int) -> tuple[bool, int]:
        return _csrf_validate(self._secret, token, now, self._max_age)

    def _origin_valid(self, request: Request, headers: dict[bytes, bytes]) -> bool:
        if self._trusted_hosts:
            host = headers.get(b"host")
            if host is None:
                return False
            try:
                if host.decode("ascii").lower() not in self._trusted_hosts:
                    return False
            except UnicodeDecodeError:
                return False
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
        # Neither header. Refused unless the application explicitly asked for
        # the fallback: it used to be inferred from `secure=False`, which is
        # what a TLS-terminating proxy leaves you with when ProxyHeaders is not
        # mounted -- so a deployment could lose the origin check as a side
        # effect of an unrelated flag it never connected to CSRF.
        return self._allow_missing_origin

    async def before(self, request: Request):
        """Prepare a token on a safe method, or validate one on an unsafe method.

        Returns None to let the request proceed, or a 403 problem+json response
        when the double-submit or origin check fails. On a safe method it always
        returns None, having recorded the token -- reused, or freshly minted when
        the cookie is absent, invalid, or three quarters expired -- for
        `csrf_token` and for `after` to write out.
        """
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
        except Exception:  # noqa: BLE001
            # `exempt` is application code standing on a security decision, so
            # failing closed is the only defensible direction and the broad
            # catch is deliberate: a predicate that raises must not become an
            # exemption. What it must not also be is invisible -- a typo in the
            # predicate refuses every unsafe request forever, and a wall of 403s
            # reads exactly like a site under attack rather than one that is
            # broken. Counting separates "this request was refused" from "the
            # check cannot run", which have different fixes.
            self.exempt_errors += 1
            return ProblemResponse(status=403, title="Forbidden", detail="CSRF validation failed")

        headers = request._index_headers()
        cookie = request.cookies.get(self._cookie_name)
        submitted = headers.get(self._header_name_bytes)
        valid, issued = False, now
        # `latin-1` cannot raise on a str that came out of header parsing,
        # where `ascii` could: a cookie value is attacker-controlled, and an
        # encode error there turned a would-be 403 into a 500.
        if (
            cookie is not None
            and submitted is not None
            and hmac.compare_digest(cookie.encode("latin-1", "replace"), submitted)
        ):
            valid, issued = self._validate(cookie, now)
        if not valid or not self._origin_valid(request, headers):
            return ProblemResponse(status=403, title="Forbidden", detail="CSRF validation failed")
        # Renewed here as well as on safe methods. A client that only ever POSTs
        # -- an SPA doing background writes, a form-less API caller -- never took
        # the safe-method path, so its token aged out and every write started
        # answering 403 with nothing to refresh it.
        renew = now - issued >= self._max_age * 3 // 4
        request.state.__setattr__(
            _STATE_TOKEN, self._new_token(now) if renew else cookie
        )
        request.state.__setattr__(_STATE_ISSUE, renew)
        return None

    async def after(self, request: Request, response):
        """Write the CSRF cookie when `before` minted or renewed the token.

        A request that reused a still-fresh token gets no `Set-Cookie` at all,
        so an unchanged token does not defeat downstream response caching.
        """
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
