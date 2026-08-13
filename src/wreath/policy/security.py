"""First-class trusted-host and browser security policy.

Three independent global policies. `TrustedHostPolicy` refuses a request
whose `Host` is not one this application serves, before routing.
`WebSocketOriginPolicy` applies an exact browser-origin allowlist before a
WebSocket handshake. `SecurityHeadersPolicy` adds hardening headers to every response
that does not already declare them:

```python
app.configure_http_policy(HttpPolicy(
    trusted_host=TrustedHostPolicy(["app.example", "*.app.example"]),
    security_headers=SecurityHeadersPolicy(
        hsts_max_age=31_536_000,
        hsts_include_subdomains=True,
    ),
))
```
Both read the request scheme and `Host`, so behind a proxy both belong after
`ProxyPolicy`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .._native import _core
from .._webpolicy import append_missing_headers, normalize_origin, origin_matches
from ..request import Request
from ..response import ProblemResponse


def _normalize_host(value: str, *, pattern: bool = False) -> str | None:
    """Return the host in a valid Host authority, stripped of its optional port."""
    return _core.normalize_host(value, pattern)


#: `host_allowed(host, patterns)` -- whether a Host value matches the compiled
#: allowlist. A `*.` pattern stands for exactly one non-empty leftmost label.
_host_allowed: Callable[[str, tuple[str, ...]], bool] = _core.host_allowed


class TrustedHostPolicy:
    """Reject requests whose Host value does not match a compiled allowlist.

    Global policy with a synchronous `_ingress_sync` stage, so it is fused into
    the pipeline with no coroutine and no await. It runs before routing, which
    is the point -- a request for a host this application does not serve never
    reaches a handler.

    The check is on the host alone. A syntactically valid numeric port is
    stripped before comparison, an IPv6 literal keeps its brackets, and the
    comparison is case-insensitive. User information, junk after a bracketed
    literal, and malformed ports are rejected rather than truncated into an
    allowed host. A
    pattern is either an exact host, `*` for any host, or `*.example.com`, which
    matches any subdomain of `example.com` but *not* the bare `example.com`; to
    accept both, list both. A `*` anywhere else in a pattern is rejected at
    construction rather than being treated as a literal.

    A request with no `Host` header, or one that matches nothing, is answered
    400 `application/problem+json` with detail `Invalid Host header`.

    Behind a proxy this must be configured after `ProxyPolicy`, which is
    what turns `X-Forwarded-Host` into the `Host` this reads.

    Args:
        allowed_hosts: Host patterns to accept. Must not be empty.

    Raises:
        ValueError: `allowed_hosts` is empty or contains an invalid pattern.
    """

    __slots__ = ("allowed_hosts",)

    def __init__(self, allowed_hosts: Iterable[str]) -> None:
        raw_patterns = tuple(allowed_hosts)
        if not raw_patterns:
            raise ValueError("allowed_hosts must not be empty")
        patterns: list[str] = []
        for value in raw_patterns:
            pattern = _normalize_host(value, pattern=True)
            if pattern is None:
                raise ValueError(f"invalid trusted-host pattern: {value!r}")
            patterns.append(pattern)
        # Unreachable today, and kept deliberately. `_normalize_host(pattern=True)`
        # admits `*` only as the whole pattern or as a leading `*.` label -- every
        # other spelling fails its `_HOST_CHARS` check and is refused above -- so
        # nothing containing `*` can reach here and fail this. It stays because it
        # is the shape rule stated where a reader looks for it, and because the
        # thing it backstops is an allowlist: `*example.com` reads like a
        # subdomain rule and matches `evilexample.com`, and that mistake must not
        # survive a future loosening of `_normalize_host`. The coupling is pinned
        # by `test_normalize_host_is_the_gate_that_makes_the_shape_check_dead`,
        # which fails if that ever stops being true.
        for pattern in patterns:
            if "*" in pattern and not (pattern == "*" or pattern.startswith("*.")):
                raise ValueError(f"invalid trusted-host pattern: {pattern!r}")
        self.allowed_hosts = tuple(patterns)

    def describe(self):
        """The 400 an untrusted or duplicated `Host` gets."""
        from ..openapi import ResponseSpec
        from .base import PolicyContract

        return PolicyContract(
            responses=(
                (
                    400,
                    ResponseSpec(
                        description="The Host header is missing, duplicated, or untrusted.",
                        media_type="application/problem+json",
                    ),
                ),
            ),
        )

    def _ingress_sync(self, request: Request):
        """Return None for an allowed unique Host, or a 400 otherwise."""
        # Host is not list-valued. Reject duplicates so an upstream proxy and
        # Wreath cannot apply different first/last interpretations.
        value_bytes: bytes | None = None
        for name, candidate in request.headers:
            if name != b"host":
                continue
            if value_bytes is not None:
                return ProblemResponse(status=400, detail="Invalid Host header")
            value_bytes = candidate
        if value_bytes is None:
            return ProblemResponse(status=400, detail="Invalid Host header")
        value = value_bytes.decode("latin-1")
        host = _normalize_host(value)
        if host is None or not _host_allowed(host, self.allowed_hosts):
            return ProblemResponse(status=400, detail="Invalid Host header")
        return None


class WebSocketOriginPolicy:
    """Reject WebSocket handshakes outside an exact browser-origin allowlist."""

    __slots__ = ("allowed_origins",)

    def __init__(self, allowed_origins: Iterable[str]) -> None:
        origins = tuple(normalize_origin(origin, label="WebSocket") for origin in allowed_origins)
        if not origins:
            raise ValueError("allowed WebSocket origins must not be empty")
        self.allowed_origins = origins

    async def _ingress(self, request: Request):
        """Admit the handshake, or refuse it with a 403 problem response.

        `None` admits; a `ProblemResponse` refuses. **Every refusal is the same
        403 with the same detail**, so a caller learns that the origin was not
        accepted and nothing about which of the three reasons applied.

        The three: no `Origin` header at all, more than one `Origin` header, and
        an `Origin` that is not in the allowlist. A missing header is refused
        rather than admitted — this policy exists to gate browser clients,
        and admitting the header-less case would let any non-browser caller past
        by simply not sending one. A repeated header is refused for the same
        reason `Authorization` is: a proxy and the application reading first,
        last or joined would each check a different value.

        Both sides are normalized the same way before they are compared —
        scheme and host lower-cased, a default port dropped — and then matched
        exactly. There is no wildcard and no suffix match, and an opaque
        `Origin: null` can never match, because the constructor refuses `null`
        as an allowlist entry.
        """
        encoded: bytes | None = None
        for name, candidate in request.headers:
            if name != b"origin":
                continue
            if encoded is not None:
                return ProblemResponse(status=403, detail="WebSocket origin is not allowed")
            encoded = candidate
        if encoded is None or not origin_matches(encoded, self.allowed_origins):
            return ProblemResponse(status=403, detail="WebSocket origin is not allowed")
        return None


class SecurityHeadersPolicy:
    """Append a precompiled set of browser security headers to responses.

    Global policy, so error responses and static files are covered too. The
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
    `ProxyPolicy` in front the header is silently never sent.
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

    def describe(self):
        """Every configured header, with its value, read off the precompiled set.

        Derived from `self.https_headers` -- the widest set this instance can
        emit -- so a deployment that turned CSP off documents no CSP. HSTS is
        included because a caller over https receives it; over http it is
        simply absent, which the description says.
        """
        from .base import HeaderSpec, PolicyContract

        return PolicyContract(
            response_headers=tuple(
                (
                    None,
                    HeaderSpec(
                        name.decode("latin-1")
                        .replace("-", " ")
                        .title()
                        .replace(" ", "-"),
                        const=value.decode("latin-1"),
                    ),
                )
                for name, value in self.https_headers
            ),
        )

    def _egress_inplace(self, request: Request, response) -> None:
        """Append every configured header the response does not already carry.

        The HSTS header is part of that set only when the request scheme is
        `https`.
        """
        # `request.scheme`, not `request.scope[...]`: this stage is global, so
        # reading the scope here materialized the lazy native scope dict on
        # every single response just to compare one string.
        additions = self.https_headers if request.scheme == "https" else self.headers
        append_missing_headers(response.headers, additions)

    def _egress_sync(self, request: Request, response):
        """Reference executor transformer; compiled policy mutates in place."""
        self._egress_inplace(request, response)
        return response

    async def _egress(self, request: Request, response):
        """Reference executor wrapper; compiled policy mutates in place."""
        return self._egress_sync(request, response)


__all__ = [
    "SecurityHeadersPolicy",
    "TrustedHostPolicy",
    "WebSocketOriginPolicy",
]
