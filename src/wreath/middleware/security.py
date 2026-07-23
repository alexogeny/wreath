"""First-party trusted-host and browser security-header middleware."""

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
    """Reject requests whose Host value does not match a compiled allowlist."""

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
        # Host validation is pure and synchronous: a before_sync hook so the
        # global pipeline runs it with no coroutine or await.
        value = request.header("host")
        if value is None or not _host_allowed(_normalize_host(value), self.allowed_hosts):
            return ProblemResponse(status=400, detail="Invalid Host header")
        return None


class SecurityHeadersMiddleware:
    """Append a precompiled set of browser security headers to responses."""

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
        additions = (
            self.https_headers if request.scope.get("scheme") == "https" else self.headers
        )
        append_missing_headers(response.headers, additions)
        return response


__all__ = ["SecurityHeadersMiddleware", "TrustedHostMiddleware"]
