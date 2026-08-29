"""Tier-3 runtime mode: audit a running server's live responses.

One-shot, dependency-free — fetches with `urllib.request` (stdlib), so no lifespan or
client wiring. The per-response logic (`audit_response`) is separated from the fetch so
it is testable against a synthetic or in-process response without a socket.
"""

from __future__ import annotations

import gzip
import urllib.error
import urllib.request
from collections.abc import Iterable
from urllib.parse import urlsplit

from .dom import parse_html
from .model import Finding, Report, Severity
from .rules import A11Y_RULES, HTML_PERF_RULES, RESPONSE_SECURITY_RULES, ResponseView


def _header_findings(headers: dict[str, str], surface: str):
    lower = {name.lower(): value for name, value in headers.items()}
    if "content-encoding" not in lower:
        yield Finding(
            "compression-enabled",
            Severity.WARN,
            surface,
            "response has no Content-Encoding (uncompressed)",
            "perf:compression",
            "",
            "mount CompressionPolicy and negotiate gzip",
        )
    if "cache-control" not in lower and "etag" not in lower:
        yield Finding(
            "cache-control",
            Severity.WARN,
            surface,
            "response has no Cache-Control or ETag",
            "perf:cache",
            "",
            "add cache directives or an ETag",
        )
    if "content-security-policy" not in lower:
        yield Finding(
            "security-headers",
            Severity.WARN,
            surface,
            "response has no Content-Security-Policy",
            "perf:security-headers",
            "",
            "mount SecurityHeadersPolicy",
        )


def audit_response(
    status: int,
    headers: dict[str, str],
    body: str,
    surface: str,
    report: Report,
    *,
    scheme: str = "http",
    set_cookies: Iterable[str] = (),
) -> None:
    """Apply the a11y + perf + security rules to one live response.

    `headers` is the collapsed single-value map; `set_cookies` carries every
    `Set-Cookie` value separately, because a response often sets more than one
    and a dict would hide all but the last.
    """
    content_type = next(
        (value for name, value in headers.items() if name.lower() == "content-type"),
        "",
    )
    if "text/html" in content_type.lower() and body:
        root = parse_html(body)
        for rule in A11Y_RULES:
            report.extend(rule(root, surface))
        for rule in HTML_PERF_RULES:
            report.extend(rule(root, body, surface))
    report.extend(_header_findings(headers, surface))
    view = ResponseView(
        status=status,
        scheme=scheme,
        surface=surface,
        headers={name.lower(): value for name, value in headers.items()},
        set_cookies=tuple(set_cookies),
    )
    for rule in RESPONSE_SECURITY_RULES:
        report.extend(rule(view))


def run_runtime_audit(base_url: str, paths: Iterable[str] = ("/",)) -> Report:
    scheme = urlsplit(base_url).scheme or "http"
    report = Report()
    for path in paths:
        url = base_url.rstrip("/") + "/" + path.lstrip("/")
        request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})  # noqa: S310 (operator-supplied URL)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 (operator-supplied URL)
                raw = response.read()
                if "gzip" in response.headers.get("Content-Encoding", ""):
                    raw = gzip.decompress(raw)
                headers = dict(response.headers.items())
                # get_all preserves every Set-Cookie; the dict above keeps only one.
                set_cookies = response.headers.get_all("Set-Cookie") or ()
                audit_response(
                    response.status,
                    headers,
                    raw.decode("utf-8", "replace"),
                    f"runtime:{path}",
                    report,
                    scheme=scheme,
                    set_cookies=set_cookies,
                )
        except (urllib.error.URLError, OSError) as exc:
            report.add(
                Finding(
                    "runtime-fetch",
                    Severity.ERROR,
                    f"runtime:{path}",
                    f"could not fetch {url}: {exc}",
                    "runtime",
                    "",
                    "is the server running and reachable?",
                )
            )
    return report
