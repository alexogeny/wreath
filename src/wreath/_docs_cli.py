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
        return _serve(
            site, root, Path(report.output), getattr(namespace, "port", 8000),
            reload=not getattr(namespace, "no_reload", False),
        )
    return 0


def _sources(site: Site, root: Path, config: Path) -> list[Path]:
    """Every file a rebuild would read: the markdown tree and the config itself."""
    return [config, *sorted((root / site.source).rglob("*.md"))]


def _stamp(paths: list[Path]) -> tuple:
    """A cheap signature that changes when any watched file does."""
    marks = []
    for path in paths:
        try:
            marks.append((path.as_posix(), path.stat().st_mtime_ns))
        except OSError:
            continue                     # deleted between listing and stat
    return tuple(marks)


def _serve(site: Site, root: Path, directory: Path, port: int, *, reload: bool) -> int:
    """Preview the built site, rebuilding when a source file changes.

    Polling rather than inotify: a watcher would be a dependency, and a docs
    tree is small enough that stat-ing it twice a second costs nothing. The
    browser is not told to refresh — reloading the tab is one keystroke, and a
    live-reload socket would mean shipping a server into every built page.
    """
    import functools
    import http.server
    import socketserver
    import threading
    import time

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(directory))

    class _Quiet(socketserver.TCPServer):
        allow_reuse_address = True       # so a restart is not refused for 60s

    with _Quiet(("127.0.0.1", port), handler) as httpd:
        print(f"wreath docs: serving {directory} at http://127.0.0.1:{port} (ctrl-c to stop)")
        if not reload:
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nwreath docs: stopped")
            return 0

        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        config = Path(root / "wreath_docs.py")
        watched = _sources(site, root, config)
        print(f"wreath docs: watching {len(watched)} source file(s); "
              "edit and reload the page")
        stamp = _stamp(watched)
        try:
            while True:
                time.sleep(0.4)
                watched = _sources(site, root, config)
                current = _stamp(watched)
                if current == stamp:
                    continue
                stamp = current
                fresh = build(site, root=root)
                _report(fresh)
                print(f"wreath docs: rebuilt {fresh.pages} page(s)")
        except KeyboardInterrupt:
            print("\nwreath docs: stopped")
        finally:
            httpd.shutdown()
    return 0
