"""Collectors + the audit runner.

Reconstructs the exact HTML Wreath generates (the API-docs surface) and, optionally,
walks static HTML trees, then applies the a11y + performance rules. Introspects the
loaded application for the middleware/OpenAPI performance checks.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .dom import parse_html
from .model import Report
from .rules import A11Y_RULES, HTML_PERF_RULES, app_perf


def _audit_html(html: str, surface: str, report: Report) -> None:
    root = parse_html(html)
    for rule in A11Y_RULES:
        report.extend(rule(root, surface))
    for rule in HTML_PERF_RULES:
        report.extend(rule(root, html, surface))


def render_api_docs(app: Any, *, title: str, version: str, spec_path: str = "/openapi.json") -> str:
    """The full API-docs document Wreath serves, as a self-contained HTML string."""
    from ..openapi import render_docs_body, render_docs_shell

    body = render_docs_body(app, title=title, version=version)
    return render_docs_shell(
        title=title, version=version, spec_path=spec_path, nonce="wreath-audit", body=body
    )


def discover_static_dirs(app: Any) -> list[str]:
    """The directories behind the app's mounted ``static()`` handlers, by walking the
    static-matcher trie. Empty when the app mounts no static trees or the internals move."""
    matcher = getattr(app, "_static_matcher", None)
    root = getattr(matcher, "_root", None)
    if root is None:
        return []
    dirs: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        mount = getattr(node, "mount", None)
        if mount is not None:
            directory = getattr(mount[2], "_root", None)
            if directory is not None:
                dirs.add(str(directory))
        stack.extend(getattr(node, "children", {}).values())
    return sorted(dirs)


def run_audit(
    app: Any,
    *,
    static_dirs: Iterable[str | Path] = (),
    discover_static: bool = True,
    title: str = "Wreath",
    version: str = "0.1.0",
) -> Report:
    """Audit the API-docs surface, any static HTML trees, and app-level performance."""
    report = Report()

    # API-docs surface — the highest-signal HTML Wreath owns.
    _audit_html(render_api_docs(app, title=title, version=version), "api-docs", report)

    # Static HTML trees: explicit dirs plus, unless disabled, the app's mounted roots.
    directories = list(static_dirs)
    if discover_static:
        directories += discover_static_dirs(app)
    for directory in dict.fromkeys(directories):
        base = Path(directory)
        for path in sorted(base.rglob("*.html")):
            _audit_html(path.read_text(encoding="utf-8"), f"static:{path}", report)

    # App-level performance (middleware stack + OpenAPI size).
    from ..openapi import generate_openapi

    openapi_json = json.dumps(generate_openapi(app, title=title, version=version))
    report.extend(app_perf(app, openapi_json))

    return report
