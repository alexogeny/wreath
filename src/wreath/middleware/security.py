"""First-party trusted-host and browser security-header middleware.

Two independent global middlewares. `TrustedHostMiddleware` refuses a request
whose `Host` is not one this application serves, before routing.
`SecurityHeadersMiddleware` adds the browser hardening headers to every response
that does not already declare them:

```python
app.add_middleware(TrustedHostMiddleware(["app.example", "*.app.example"]))
app.add_middleware(SecurityHeadersMiddleware(hsts_max_age=31_536_000,
                                             hsts_include_subdomains=True))
```
Both read the request scheme and `Host`, so behind a proxy both belong after
`ProxyHeadersMiddleware`.
"""

from __future__ import annotations

from collections.abc import Iterable

from .._native import _core
from .._webpolicy import append_missing_headers
from ..request import Request
from ..response import ProblemResponse


def _normalize_host(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end >= 0 else value
    return value.partition(":")[0]


if _core is None or not hasattr(_core, "host_allowed"):
    from .._pure.security import host_allowed as _host_allowed
else:
    _host_allowed = _core.host_allowed


class TrustedHostMiddleware:
    """Reject requests whose Host value does not match a compiled allowlist.

    Global middleware with a synchronous `before_sync` hook, so it is fused into
    the pipeline with no coroutine and no await. It runs before routing, which
    is the point -- a request for a host this application does not serve never
    reaches a handler.

    The check is on the host alone. Any port is stripped before comparison, an
    IPv6 literal keeps its brackets, and the comparison is case-insensitive. A
    pattern is either an exact host, `*` for any host, or `*.example.com`, which
    matches any subdomain of `example.com` but *not* the bare `example.com`; to
    accept both, list both. A `*` anywhere else in a pattern is rejected at
    construction rather than being treated as a literal.

    A request with no `Host` header, or one that matches nothing, is answered
    400 `application/problem+json` with detail `Invalid Host header`.

    Behind a proxy this must be mounted after `ProxyHeadersMiddleware`, which is
    what turns `X-Forwarded-Host` into the `Host` this reads.

    Args:
        allowed_hosts: Host patterns to accept. Must not be empty.

    Raises:
        ValueError: `allowed_hosts` is empty.
        ValueError: A pattern contains `*` other than as `*` or a leading `*.`.
    """

    global_scope = True
    __slots__ = ("allowed_hosts",)

    def __init__(self, allowed_hosts: Iterable[str]) -> None:
        patterns = tuple(_normalize_host(host) for host in allowed_hosts)
        if not patterns:
            raise ValueError("allowed_hosts must not be empty")
        for pattern in patterns:
            if "*" in pattern and not (pattern == "*" or pattern.startswith("*.")):
                raise ValueError(f"invalid trusted-host pattern: {pattern!r}")
        self.allowed_hosts = patterns

    def before_sync(self, request: Request):
        """Return None for an allowed host, or a 400 problem response otherwise."""
        # Host validation is pure and synchronous: a before_sync hook so the
        # global pipeline runs it with no coroutine or await.
        value = request.header("host")
        if value is None or not _host_allowed(_normalize_host(value), self.allowed_hosts):
            return ProblemResponse(status=400, detail="Invalid Host header")
        return None


class SecurityHeadersMiddleware:
    """Append a precompiled set of browser security headers to responses.

    Global middleware, so error responses and static files are covered too. The
    header list is built once in the constructor; the per-response work is one
    scheme comparison and an append of whatever is not already there.

    A header is added only when the response does not already carry one by that
    name, matched case-insensitively. A handler that set its own
    `Content-Security-Policy` keeps it -- this sets defaults, it does not
    enforce them.

    By default it emits `Content-Security-Policy: default-src 'self'`,
    `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, and
    `Referrer-Policy: strict-origin-when-cross-origin`. Pass None for any of
    those to omit it. `Permissions-Policy` is off unless configured.

    `Strict-Transport-Security` is emitted only on responses to requests whose
    scheme is `https`, and only when HSTS was configured at all. Behind a
    TLS-terminating proxy the scheme Wreath sees is `http`, so without
    `ProxyHeadersMiddleware` in front the header is silently never sent.
    Configure HSTS either structurally through `hsts_max_age` and its companions
    or verbatim through `strict_transport_security`, never both.

    Args:
        content_security_policy: The CSP value, or None to emit no CSP header.
        frame_options: The `X-Frame-Options` value, or None to omit it.
        content_type_options: Emit `X-Content-Type-Options: nosniff`.
        referrer_policy: The `Referrer-Policy` value, or None to omit it.
        permissions_policy: The `Permissions-Policy` value. None omits it.
        hsts_max_age: HSTS lifetime in seconds. None emits no HSTS header.
        hsts_include_subdomains: Add `includeSubDomains` to the HSTS value.
        hsts_preload: Add `preload`, which the preload list requires.
        strict_transport_security: A verbatim HSTS value instead of the structured ones.

    Raises:
        ValueError: Both `hsts_max_age` and `strict_transport_security` were given.
        ValueError: `hsts_max_age` is not a non-negative integer.
        ValueError: `hsts_preload` without `hsts_include_subdomains` and a year of max-age.
    """

    global_scope = True
    __slots__ = ("headers", "hsts_header", "https_headers")

    def __init__(
        self,
        *,
        content_security_policy: str | None = "default-src 'self'",
        frame_options: str | None = "DENY",
        content_type_options: bool = True,
        referrer_policy: str | None = "strict-origin-when-cross-origin",
        permissions_policy: str | None = None,
        hsts_max_age: int | None = None,
        hsts_include_subdomains: bool = False,
        hsts_preload: bool = False,
        strict_transport_security: str | None = None,
    ) -> None:
        if hsts_max_age is not None and strict_transport_security is not None:
            raise ValueError("structured and raw HSTS settings are mutually exclusive")
        if hsts_max_age is not None and (
            not isinstance(hsts_max_age, int) or isinstance(hsts_max_age, bool) or hsts_max_age < 0
        ):
            raise ValueError("hsts_max_age must be a non-negative integer")
        if hsts_preload and (
            not hsts_include_subdomains or hsts_max_age is None or hsts_max_age < 31_536_000
        ):
            raise ValueError("HSTS preload requires includeSubDomains and max-age >= 31536000")
        if hsts_max_age is not None:
            directives = [f"max-age={hsts_max_age}"]
            if hsts_include_subdomains:
                directives.append("includeSubDomains")
            if hsts_preload:
                directives.append("preload")
            strict_transport_security = "; ".join(directives)
        self.hsts_header = (
            (b"strict-transport-security", strict_transport_security.encode("latin-1"))
            if strict_transport_security is not None
            else None
        )
        headers: list[tuple[bytes, bytes]] = []
        if content_security_policy is not None:
            headers.append((b"content-security-policy", content_security_policy.encode("latin-1")))
        if frame_options is not None:
            headers.append((b"x-frame-options", frame_options.encode("latin-1")))
        if content_type_options:
            headers.append((b"x-content-type-options", b"nosniff"))
        if referrer_policy is not None:
            headers.append((b"referrer-policy", referrer_policy.encode("latin-1")))
        if permissions_policy is not None:
            headers.append((b"permissions-policy", permissions_policy.encode("latin-1")))
        self.headers = tuple(headers)
        self.https_headers = (
            (*self.headers, self.hsts_header) if self.hsts_header is not None else self.headers
        )

    async def after(self, request: Request, response):
        """Append every configured header the response does not already carry.

        The HSTS header is part of that set only when the request scheme is
        `https`.
        """
        # `request.scheme`, not `request.scope[...]`: this hook is global, so
        # reading the scope here materialized the lazy native scope dict on
        # every single response just to compare one string.
        additions = self.https_headers if request.scheme == "https" else self.headers
        append_missing_headers(response.headers, additions)
        return response


__all__ = ["SecurityHeadersMiddleware", "TrustedHostMiddleware"]
