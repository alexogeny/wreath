"""Thin presentation layer for ``wreath port`` (mirrors ``_migrations_cli``).

All analysis lives in the ``_port`` core; this only parses the namespace, drives
``analyze_all``, and renders. Exit code is nonzero if unsupported constructs were
found (so CI can gate a "clean port" the way ``wreath migrations check`` gates on
drift), matching design 07 §3's report-first posture.
"""
from __future__ import annotations

import json
from pathlib import Path

from .analyzer import analyze_all
from .emit import port_tree


def execute(namespace) -> int:
    roots = [Path(s) for s in namespace.source]
    missing = [str(r) for r in roots if not r.exists()]
    if missing:
        raise ValueError(f"source path(s) not found: {', '.join(missing)}")

    in_place = bool(getattr(namespace, "in_place", False))
    output = getattr(namespace, "output", None)
    force = bool(getattr(namespace, "force", False))

    # Emit mode (Phase 1): --output <dir> or --in-place. Otherwise report-only.
    if in_place or output:
        total = 0
        for root in roots:
            result = port_tree(root, output, in_place=in_place, force=force)
            total += len(result.written_files) + len(result.regenerated)
            for path in result.written_files:
                print(f"wrote      {path}")
            for path in result.regenerated:
                print(f"regenerated {path}")
            for path in result.skipped:
                print(f"skipped    {path}")
        print(f"\n{total} file(s) emitted. Review every `# TODO(wreath-port: ...)` before use.")
        return 0

    report = analyze_all(roots)
    if getattr(namespace, "as_json", False):
        print(json.dumps(report.to_json(), indent=2))
    else:
        print(report.to_markdown())

    # Exit nonzero when the port is not fully automatic, so CI can gate on it.
    return 1 if report.counts()["unsupported"] else 0
