"""wreath audit Tier 2/3: contrast, render/inline perf rules, --fix remediation,
runtime response auditing, static auto-discovery, and the dev middleware. Pure Python —
holds under WREATH_PURE=1.
"""
from __future__ import annotations

import asyncio
from typing import Any

from wreath import Wreath
from wreath._audit.contrast import contrast_findings, contrast_ratio, parse_tokens
from wreath._audit.dom import parse_html
from wreath._audit.fix import apply_fixes
from wreath._audit.middleware import AuditMiddleware
from wreath._audit.model import Report, Severity
from wreath._audit.rules import A11Y_RULES, HTML_PERF_RULES
from wreath._audit.runtime import audit_response
from wreath._audit.sources import discover_static_dirs, run_audit


def _a11y(html: str):
    root = parse_html(html)
    return [f for rule in A11Y_RULES for f in rule(root, "test")]


def _perf(html: str):
    root = parse_html(html)
    return [f for rule in HTML_PERF_RULES for f in rule(root, html, "test")]


def _app() -> Wreath:
    app = Wreath()

    @app.get("/thing", summary="Get a thing", tags=("things",))
    async def thing(request: Any) -> str:
        return "ok"

    return app


# --- Tier 2: contrast -------------------------------------------------------------
def test_contrast_ratio_math() -> None:
    assert round(contrast_ratio("#000000", "#ffffff"), 1) == 21.0
    assert contrast_ratio("#ffffff", "#ffffff") == 1.0


def test_contrast_resolves_tokens_per_theme() -> None:
    css = ("<style>:root{--ink:#111}@media(prefers-color-scheme:dark)"
           "{:root{--ink:#eee}}</style>")
    themes = parse_tokens(css.replace("<style>", "").replace("</style>", ""))
    assert themes["light"]["--ink"] == "#111"
    assert themes["dark"]["--ink"] == "#eee"


def test_contrast_flags_bad_pair_and_is_silent_on_good() -> None:
    bad = "body{color:var(--fg);background:#fff}:root{--fg:#999}"
    fired = list(contrast_findings(bad, "test"))
    assert fired and fired[0].rule_id == "contrast" and fired[0].severity is Severity.WARN
    good = "body{color:var(--fg);background:#fff}:root{--fg:#222}"
    assert list(contrast_findings(good, "test")) == []


def test_contrast_rule_runs_via_style_element() -> None:
    html = ("<html><head><style>body{color:var(--x);background:#fff}:root{--x:#aaa}"
            "</style></head><body>x</body></html>")
    assert any(f.rule_id == "contrast" for f in _a11y(html))


# --- Tier 2: render-blocking / inline-asset ---------------------------------------
def test_render_blocking_fires_and_defer_is_clean() -> None:
    blocking = ('<html><head><link rel="stylesheet" href="a.css">'
                '<script src="b.js"></script></head><body></body></html>')
    assert sum(f.rule_id == "render-blocking" for f in _perf(blocking)) == 2
    ok = '<html><head><script src="b.js" defer></script></head><body></body></html>'
    assert not any(f.rule_id == "render-blocking" for f in _perf(ok))


def test_inline_asset_flags_large_unnonced_only() -> None:
    big = "x" * (20 * 1024)
    unnonced = f"<html><head><style>{big}</style></head><body></body></html>"
    assert any(f.rule_id == "inline-asset" for f in _perf(unnonced))
    nonced = f'<html><head><style nonce="n">{big}</style></head><body></body></html>'
    assert not any(f.rule_id == "inline-asset" for f in _perf(nonced))


# --- Tier 3: --fix ----------------------------------------------------------------
_BAD = ('<!DOCTYPE html><html><head>'
        '<meta name="viewport" content="width=device-width, user-scalable=no">'
        '</head><body><img src="a.png">'
        '<table><tr><th>h</th></tr></table>'
        '<div tabindex="5">x</div></body></html>')


def test_apply_fixes_remediates_and_is_idempotent() -> None:
    fixed, applied = apply_fixes(_BAD)
    kinds = {a.split()[0] for a in applied}
    assert {"html-lang", "img-alt", "table-headers", "tabindex", "viewport-scale"} <= kinds
    # the fixed document no longer trips those rules
    fired = {f.rule_id for f in _a11y(fixed)}
    for gone in ("html-lang", "img-alt", "tabindex", "viewport-scale"):
        assert gone not in fired, f"{gone} still fires after --fix"
    assert not any(f.rule_id == "table-headers" and "scope" in f.message for f in _a11y(fixed))
    # re-running is a no-op
    again, applied2 = apply_fixes(fixed)
    assert applied2 == [] and again == fixed
    # nonce/formatting preserved — splice never reserialises
    assert "<!DOCTYPE html>" in fixed


# --- Tier 3: runtime response auditing --------------------------------------------
def test_runtime_audit_response_flags_html_and_headers() -> None:
    report = Report()
    audit_response(200, {"content-type": "text/html"},
                   '<html><body><img src="a.png"></body></html>', "runtime:/", report)
    fired = {f.rule_id for f in report.findings}
    assert "img-alt" in fired  # HTML rule ran on the body
    assert {"compression-enabled", "cache-control", "security-headers"} <= fired  # headers


# --- Tier 2: static auto-discovery ------------------------------------------------
def test_discover_static_dirs(tmp_path) -> None:
    (tmp_path / "index.html").write_text(
        "<html lang='en'><title>t</title><body><main></main></body></html>"
    )
    app = _app()
    app.static("/assets", str(tmp_path))
    assert str(tmp_path) in discover_static_dirs(app)


# --- dogfooding: wreath's own docs audit clean of the two fixed rules -------------
def test_api_docs_clean_of_heading_and_table_rules() -> None:
    report = run_audit(_app(), title="Demo", version="1.0.0", discover_static=False)
    assert report.errors == [], [f.to_dict() for f in report.errors]
    offenders = [f.rule_id for f in report.findings
                 if f.rule_id in ("heading-order", "table-headers")]
    assert offenders == [], offenders


# --- dev middleware smoke ---------------------------------------------------------
class _Resp:
    def __init__(self, body: bytes, headers) -> None:
        self.body = body
        self.headers = headers


class _Req:
    path = "/x"
    method = "GET"


def test_audit_middleware_logs_and_returns_response() -> None:
    mw = AuditMiddleware()
    resp = _Resp(b'<html><body><img src="a.png"></body></html>',
                 [(b"content-type", b"text/html; charset=utf-8")])
    out = asyncio.run(mw.after(_Req(), resp))
    assert out is resp  # never rewrites the response
    # non-HTML is ignored without error
    other = _Resp(b"{}", [(b"content-type", b"application/json")])
    assert asyncio.run(mw.after(_Req(), other)) is other
