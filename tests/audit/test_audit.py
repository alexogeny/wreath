from __future__ import annotations

from typing import Any

from wreath import Wreath
from wreath._audit.dom import parse_html
from wreath._audit.model import Finding, Severity
from wreath._audit.rules import A11Y_RULES, app_perf
from wreath._audit.sources import run_audit
from wreath.policy import HttpPolicy, SecurityHeadersPolicy
from wreath.policy.compression import CompressionPolicy


def _app() -> Wreath:
    app = Wreath()

    @app.get("/thing", summary="Get a thing", tags=("things",))
    async def thing(request: Any) -> str:
        return "ok"

    return app


def test_finding_does_not_allocate_an_instance_dictionary() -> None:
    finding = Finding("rule", Severity.WARN, "app", "message")

    assert not hasattr(finding, "__dict__")


def test_self_audit_api_docs_has_no_errors() -> None:
    # Wreath's own API-docs shell ships <html lang>, <title>, and a zoomable viewport,
    # so the tool's first job is a clean self-audit (warnings are permitted).
    report = run_audit(_app(), title="Demo", version="1.0.0")
    assert report.errors == [], [f.as_dict() for f in report.errors]
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
        "html-lang",
        "document-title",
        "img-alt",
        "control-label",
        "heading-order",
        "link-text",
        "table-headers",
        "duplicate-id",
        "tabindex",
        "viewport-scale",
        "aria-valid",
        "landmarks",
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
        "</main></body></html>"
    )
    assert _run_a11y(good) == []


def test_image_input_without_alt_is_flagged() -> None:
    # WCAG 1.1.1: <input type=image> is a functional image needing a text alt.
    findings = _run_a11y('<form><input type="image" src="go.png"></form>')
    assert any(f.rule_id == "img-alt" for f in findings)
    # aria-label satisfies it; a non-empty alt does too.
    assert _run_a11y('<input type="image" src="go.png" alt="Search">') == []
    assert _run_a11y('<input type="image" src="go.png" aria-label="Search">') == []


def test_valid_aria_1_2_tokens_are_not_false_flagged() -> None:
    # Regression: the allow-lists must cover ARIA 1.2, or conformant markup is
    # reported as defective (a false positive against WCAG 4.1.2).
    for markup in (
        '<th aria-sort="ascending">Name</th>',
        '<span role="code">x = 1</span>',
        '<li role="none" aria-setsize="3" aria-posinset="1">a</li>',
        '<div role="meter" aria-valuenow="5" aria-valuemin="0" aria-valuemax="10"></div>',
    ):
        fired = {f.rule_id for f in _run_a11y(markup)}
        assert "aria-valid" not in fired, markup


def test_duplicate_id_cites_the_wcag_2_2_criterion() -> None:
    findings = _run_a11y('<p id="x">a</p><p id="x">b</p>')
    dup = next(f for f in findings if f.rule_id == "duplicate-id")
    assert dup.reference == "WCAG 4.1.2"  # 4.1.1 was obsoleted in WCAG 2.2


def test_autoplay_audio_is_flagged() -> None:
    assert any(f.rule_id == "autoplay" for f in _run_a11y("<video autoplay src='v.mp4'>"))
    assert _run_a11y("<video autoplay muted src='v.mp4'>") == []  # muted is fine


def test_meta_refresh_is_flagged() -> None:
    fired = {f.rule_id for f in _run_a11y('<meta http-equiv="refresh" content="10">')}
    assert "meta-refresh" in fired
    # an instant redirect (content=0) is a server-side 3xx concern, not this rule
    assert not any(
        f.rule_id == "meta-refresh"
        for f in _run_a11y('<meta http-equiv="refresh" content="0; url=/next">')
    )


def test_focus_outline_removal_is_flagged() -> None:
    assert any(
        f.rule_id == "focus-visible" for f in _run_a11y("<style>:focus { outline: none }</style>")
    )
    assert any(
        f.rule_id == "focus-visible" for f in _run_a11y('<button style="outline:0">x</button>')
    )


def test_perf_flags_missing_middleware() -> None:
    fired = {f.rule_id for f in app_perf(_app(), "{}")}
    assert {"compression-enabled", "cache-control", "security-headers"} <= fired


def test_perf_middleware_introspection_positive() -> None:
    app = Wreath(http_policy=HttpPolicy(security_headers=SecurityHeadersPolicy()))
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy()))
    fired = {f.rule_id for f in app_perf(app, "{}")}
    assert "compression-enabled" not in fired
    assert "security-headers" not in fired
    assert "cache-control" in fired  # still unmounted


def test_json_size_budget() -> None:
    big = "x" * (600 * 1024)
    fired = {f.rule_id for f in app_perf(_app(), big)}
    assert "json-size" in fired
