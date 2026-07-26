"""Thin CLI facade for ``wreath docs`` — mirrors the migrations CLI.

``build`` renders the site, ``check`` builds strictly and reports orphan/dead-link
issues with a ``migrations check``-style exit code (0 clean, 1 on findings, 2 on
usage), and ``serve`` builds then serves the output for local preview.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from ._docs import build
from ._docs.config import Site


def _load_site(config_path: str) -> Site | None:
    path = Path(config_path)
    if not path.is_file():
        print(f"wreath docs: config not found: {config_path}", file=sys.stderr)
        return None
    spec = importlib.util.spec_from_file_location("wreath_docs_config", path)
    if spec is None or spec.loader is None:
        print(f"wreath docs: cannot load {config_path}", file=sys.stderr)
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    site = getattr(module, "site", None)
    if not isinstance(site, Site):
        print(f"wreath docs: {config_path} must define a `site = Site(...)`", file=sys.stderr)
        return None
    return site


def _report(report: Any) -> None:
    for warning in report.warnings:
        print(f"  warning: {warning}")
    for error in report.errors:
        print(f"  ERROR: {error}")


def execute(namespace: Any) -> int:
    action = getattr(namespace, "docs_action", None)
    if action not in ("build", "check", "serve"):
        print("wreath docs: expected 'build', 'check', or 'serve'.", file=sys.stderr)
        return 2

    site = _load_site(namespace.config)
    if site is None:
        return 2
    root = Path(namespace.config).resolve().parent

    if action == "check":
        report = build(replace(site, strict=True), root=root)
        _report(report)
        if report.errors:
            print(f"wreath docs check: {len(report.errors)} error(s)", file=sys.stderr)
            return 1
        print(f"wreath docs check: {report.pages} page(s) clean"
              + (f", {len(report.warnings)} warning(s)" if report.warnings else ""))
        return 0

    report = build(site, root=root)
    _report(report)
    if report.errors:
        return 1
    print(f"wreath docs: built {report.pages} page(s) into {report.output}")
    if action == "serve":
        return _serve(Path(report.output), getattr(namespace, "port", 8000))
    return 0


def _serve(directory: Path, port: int) -> int:
    """Preview the built site with the standard-library HTTP server."""
    import functools
    import http.server
    import socketserver

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        print(f"wreath docs: serving {directory} at http://127.0.0.1:{port} (ctrl-c to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nwreath docs: stopped")
    return 0
