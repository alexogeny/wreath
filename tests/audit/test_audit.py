"""wreath audit (Tier 1): self-audit of the API-docs surface, a11y rule firing,
and middleware-introspection performance checks. Pure Python — holds under WREATH_PURE=1.
"""
from __future__ import annotations

from typing import Any

from wreath import Wreath
from wreath._audit.dom import parse_html
from wreath._audit.model import Severity
from wreath._audit.rules import A11Y_RULES, app_perf
from wreath._audit.sources import run_audit
from wreath.middleware.compression import CompressionMiddleware
from wreath.middleware.security import SecurityHeadersMiddleware


def _app() -> Wreath:
    app = Wreath()

    @app.get("/thing", summary="Get a thing", tags=("things",))
    async def thing(request: Any) -> str:
        return "ok"

    return app


def test_self_audit_api_docs_has_no_errors() -> None:
    # Wreath's own API-docs shell ships <html lang>, <title>, and a zoomable viewport,
    # so the tool's first job is a clean self-audit (warnings are permitted).
    report = run_audit(_app(), title="Demo", version="1.0.0")
    assert report.errors == [], [f.to_dict() for f in report.errors]
    assert any(f.surface == "api-docs" for f in report.findings) or not report.findings


_BAD_HTML = """<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width, user-scalable=no"></head>
<body>
<h1 id="dup">Heading</h1>
<h3>Skipped a level</h3>
<img src="a.png">
<form><input type="text" name="q"></form>
<a href="/y">click here</a>
<table><tr><td>cell</td></tr></table>
<div id="dup" tabindex="3" aria-flurb="1">x</div>
</body></html>
"""


def _run_a11y(html: str):
    root = parse_html(html)
    findings = []
    for rule in A11Y_RULES:
        findings.extend(rule(root, "test"))
    return findings


def test_a11y_rules_fire_on_bad_html() -> None:
    findings = _run_a11y(_BAD_HTML)
    fired = {f.rule_id for f in findings}
    for expected in {
        "html-lang", "document-title", "img-alt", "control-label", "heading-order",
        "link-text", "table-headers", "duplicate-id", "tabindex", "viewport-scale",
        "aria-valid", "landmarks",
    }:
        assert expected in fired, f"{expected} did not fire; fired={sorted(fired)}"
    # errors carry a WCAG reference and the right severity
    lang = next(f for f in findings if f.rule_id == "html-lang")
    assert lang.severity is Severity.ERROR and lang.reference == "WCAG 3.1.1"
    dup = next(f for f in findings if f.rule_id == "duplicate-id")
    assert dup.severity is Severity.ERROR


def test_a11y_clean_html_is_silent() -> None:
    good = (
        '<!DOCTYPE html><html lang="en"><head><title>Ok</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        '<body><main><h1>Ok</h1><h2>Sub</h2><img src="a.png" alt="a" width="4" height="4">'
        '</main></body></html>'
    )
    assert _run_a11y(good) == []


def test_perf_flags_missing_middleware() -> None:
    fired = {f.rule_id for f in app_perf(_app(), "{}")}
    assert {"compression-enabled", "cache-control", "security-headers"} <= fired


def test_perf_middleware_introspection_positive() -> None:
    app = _app()
    app.add_middleware(CompressionMiddleware())
    app.add_middleware(SecurityHeadersMiddleware())
    fired = {f.rule_id for f in app_perf(app, "{}")}
    assert "compression-enabled" not in fired
    assert "security-headers" not in fired
    assert "cache-control" in fired  # still unmounted


def test_json_size_budget() -> None:
    big = "x" * (600 * 1024)
    fired = {f.rule_id for f in app_perf(_app(), big)}
    assert "json-size" in fired
