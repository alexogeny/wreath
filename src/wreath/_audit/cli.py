"""Thin CLI facade for `wreath audit` — mirrors the migrations CLI: human or
`--json` report, and a `migrations check`-style exit code (0 clean, 1 on findings,
2 on usage). `static` audits generated + static HTML; `runtime` audits a live server;
`--fix` applies the safe remediation subset.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .model import Report, Severity
from .sources import run_audit

_SEV_LABEL = {Severity.ERROR: "ERROR", Severity.WARN: " WARN", Severity.INFO: " INFO"}


def _render_human(report: Report) -> None:
    if not report.findings:
        print("wreath audit: no findings — clean.")
        return
    surface = None
    for f in report.sorted():
        if f.surface != surface:
            surface = f.surface
            print(f"\n{surface}")
        loc = f" {f.location}" if f.location else ""
        ref = f" ({f.reference})" if f.reference else ""
        print(f"  {_SEV_LABEL[f.severity]} {f.rule_id}{ref}{loc}: {f.message}")
        if f.suggestion:
            print(f"         → {f.suggestion}")
    print(f"\n{len(report.errors)} error(s), {len(report.warnings)} warning(s)")


def _emit(namespace: Any, report: Report) -> int:
    if getattr(namespace, "as_json", False):
        print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
    else:
        _render_human(report)
    if report.errors or (getattr(namespace, "strict", False) and report.warnings):
        return 1
    return 0


def _run_fix(namespace: Any, app: Any) -> int:
    from .fix import apply_fixes
    from .sources import discover_static_dirs, render_api_docs

    # The API-docs surface is generated: patch the source, never rewrite rendered bytes.
    _fixed, suggestions = apply_fixes(
        render_api_docs(app, title=namespace.title, version=namespace.version)
    )
    print("api-docs (generated surface — apply these to the source, not rendered output):")
    for s in suggestions or ():
        print(f"  suggest {s}")
    if not suggestions:
        print("  no auto-fixable findings.")

    # Static HTML files are owned by the consumer: apply the safe subset in place.
    directories = list(namespace.static or ()) + discover_static_dirs(app)
    changed = 0
    for directory in dict.fromkeys(directories):
        for path in sorted(Path(directory).rglob("*.html")):
            html = path.read_text(encoding="utf-8")
            fixed, applied = apply_fixes(html)
            if applied and fixed != html:
                path.write_text(fixed, encoding="utf-8")
                changed += 1
                print(f"\n{path}")
                for a in applied:
                    print(f"  fixed {a}")
    print(f"\n{changed} static file(s) modified.")
    return 0


def _run_runtime(namespace: Any) -> int:
    url = getattr(namespace, "url", None)
    if not url:
        print("wreath audit runtime: a base URL is required (e.g. http://localhost:8000)",
              file=sys.stderr)
        return 2
    from .runtime import run_runtime_audit

    return _emit(namespace, run_runtime_audit(url))


def execute(namespace: Any, load_application: Callable[..., Any]) -> int:
    action = getattr(namespace, "audit_action", None)
    if action == "runtime":
        return _run_runtime(namespace)
    if action != "static":
        print("wreath audit: expected the 'static' or 'runtime' action.", file=sys.stderr)
        return 2

    app = load_application(namespace.target, factory=namespace.factory)
    if getattr(namespace, "fix", False):
        return _run_fix(namespace, app)

    report = run_audit(
        app,
        static_dirs=namespace.static or (),
        title=namespace.title,
        version=namespace.version,
    )
    return _emit(namespace, report)
