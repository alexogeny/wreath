"""HTTP-compliance and web-security rules for the application a developer builds
on Wreath — so a downstream SaaS gets first-class compliance signal from
`wreath audit runtime` against its own live responses.

Two entry points, mirroring the perf rules:

`RESPONSE_SECURITY_RULES` — `(view: ResponseView) -> Iterator[Finding]`,
applied to every live response by the runtime auditor. These catch defects that
only show on the wire: cookie attribute flags, HSTS, the status-specific RFC
MUSTs (401→WWW-Authenticate, 405→Allow), and CORS misconfiguration. HTTP
compliance is inherently a property of the response, so it lives in the runtime
tier; the static tier's app-level checks stay in `perf.app_perf`.

References cite the governing RFC clause (or OWASP secure-header guidance) so a
finding is actionable and traceable, not just advisory.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from ..model import Finding, Severity

RESPONSE_SECURITY_RULES: list = []

#: HSTS max-age floors: the preload floor (1y) and a soft floor (180d).
_HSTS_PRELOAD_MIN = 31536000
_HSTS_SOFT_MIN = 15552000


def _rule(fn):
    RESPONSE_SECURITY_RULES.append(fn)
    return fn


@dataclass(frozen=True)
class ResponseView:
    """One live response, normalized for the security rules.

    `headers` is lower-cased and last-value-wins (fine for the single-valued
    headers these rules read); `set_cookies` keeps every `Set-Cookie` value
    because a response commonly sets more than one.
    """

    status: int
    scheme: str                       # "http" | "https"
    surface: str
    headers: dict[str, str] = field(default_factory=dict)
    set_cookies: tuple[str, ...] = ()

    @property
    def is_https(self) -> bool:
        return self.scheme == "https"

    def get(self, name: str, default: str = "") -> str:
        return self.headers.get(name, default)

    def has(self, name: str) -> bool:
        return name in self.headers


def _f(rule_id, severity, view: ResponseView, message, reference, suggestion=""):
    return Finding(rule_id, severity, view.surface, message, reference, "", suggestion)


def _parse_cookie(raw: str) -> tuple[str, dict[str, str]]:
    """`"sid=x; Path=/; Secure"` -> `("sid", {"path": "/", "secure": ""})`."""
    parts = raw.split(";")
    name = parts[0].split("=", 1)[0].strip()
    attrs: dict[str, str] = {}
    for segment in parts[1:]:
        key, _, value = segment.strip().partition("=")
        attrs[key.strip().lower()] = value.strip()
    return name, attrs


@_rule
def cookie_flags(view: ResponseView) -> Iterator[Finding]:
    """Session-cookie hardening + the RFC 6265bis rules that browsers enforce."""
    for raw in view.set_cookies:
        name, attrs = _parse_cookie(raw)
        if not name:
            continue
        secure = "secure" in attrs
        samesite = attrs.get("samesite", "").lower()
        # Browser-enforced MUSTs (RFC 6265bis): the cookie is silently dropped.
        if samesite == "none" and not secure:
            yield _f("cookie-samesite-none-insecure", Severity.ERROR, view,
                     f"cookie {name!r} is SameSite=None without Secure — browsers drop it",
                     "RFC 6265bis 5.4.7", "set Secure on SameSite=None cookies")
        if name.startswith("__Host-") and not (
            secure and attrs.get("path") == "/" and "domain" not in attrs
        ):
            yield _f("cookie-prefix", Severity.ERROR, view,
                     f"__Host- cookie {name!r} must be Secure, Path=/, and set no Domain",
                     "RFC 6265bis 4.1.3", "fix the attributes or drop the __Host- prefix")
        elif name.startswith("__Secure-") and not secure:
            yield _f("cookie-prefix", Severity.ERROR, view,
                     f"__Secure- cookie {name!r} must be Secure",
                     "RFC 6265bis 4.1.3", "set Secure on __Secure- cookies")
        # Defense-in-depth defaults (OWASP): warn, since not every cookie needs them.
        if not samesite:
            yield _f("cookie-samesite", Severity.WARN, view,
                     f"cookie {name!r} has no SameSite attribute (CSRF exposure)",
                     "OWASP:session", "set SameSite=Lax (or Strict) unless it is cross-site")
        if view.is_https and not secure:
            yield _f("cookie-secure", Severity.WARN, view,
                     f"cookie {name!r} has no Secure flag on an HTTPS response",
                     "OWASP:session", "set Secure so the cookie never rides plain HTTP")
        if "httponly" not in attrs:
            yield _f("cookie-httponly", Severity.WARN, view,
                     f"cookie {name!r} has no HttpOnly flag (readable from JS)",
                     "OWASP:session", "set HttpOnly unless a script must read this cookie")


@_rule
def hsts(view: ResponseView) -> Iterator[Finding]:
    """Strict-Transport-Security on HTTPS responses (RFC 6797)."""
    if not view.is_https:
        return
    raw = view.get("strict-transport-security")
    if not raw:
        yield _f("hsts", Severity.WARN, view,
                 "HTTPS response has no Strict-Transport-Security header",
                 "RFC 6797", "mount SecurityHeadersPolicy(hsts=...)")
        return
    max_age = 0
    for segment in raw.split(";"):
        key, _, value = segment.strip().partition("=")
        if key.strip().lower() == "max-age":
            try:
                max_age = int(value.strip())
            except ValueError:
                max_age = 0
    if max_age < _HSTS_SOFT_MIN:
        yield _f("hsts-max-age", Severity.INFO, view,
                 f"HSTS max-age={max_age} is short (< {_HSTS_SOFT_MIN}s / 180d)",
                 "RFC 6797", f"raise max-age to {_HSTS_PRELOAD_MIN} (1y) for preload eligibility")


@_rule
def content_type_options(view: ResponseView) -> Iterator[Finding]:
    """X-Content-Type-Options: nosniff (defends against MIME confusion)."""
    if view.get("x-content-type-options").lower() != "nosniff":
        yield _f("content-type-options", Severity.WARN, view,
                 "response has no 'X-Content-Type-Options: nosniff'",
                 "OWASP:headers", "mount SecurityHeadersPolicy")


@_rule
def status_required_headers(view: ResponseView) -> Iterator[Finding]:
    """Status-specific headers the RFC marks MUST."""
    if view.status == 401 and not view.has("www-authenticate"):
        yield _f("www-authenticate", Severity.ERROR, view,
                 "401 response has no WWW-Authenticate header (required)",
                 "RFC 9110 15.5.2", "raise Unauthorized(challenge=...) with a scheme")
    if view.status == 405 and not view.has("allow"):
        yield _f("allow-header", Severity.ERROR, view,
                 "405 response has no Allow header (required)",
                 "RFC 9110 15.5.6", "raise MethodNotAllowed(allow=[...])")


@_rule
def cors_credentials(view: ResponseView) -> Iterator[Finding]:
    """A wildcard ACAO with credentials is forbidden and browser-rejected."""
    origin = view.get("access-control-allow-origin")
    credentials = view.get("access-control-allow-credentials").lower() == "true"
    if origin == "*" and credentials:
        yield _f("cors-credentials", Severity.ERROR, view,
                 "Access-Control-Allow-Origin: * with Allow-Credentials: true is invalid",
                 "Fetch:cors", "reflect a specific allowed origin instead of '*'")


@_rule
def referrer_policy(view: ResponseView) -> Iterator[Finding]:
    """Referrer-Policy limits URL leakage to third parties (OWASP)."""
    if not view.has("referrer-policy"):
        yield _f("referrer-policy", Severity.INFO, view,
                 "response has no Referrer-Policy header",
                 "OWASP:headers", "mount SecurityHeadersPolicy (sets no-referrer)")
