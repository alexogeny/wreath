"""`wreath port` codemod internals (Phase 0: static analysis + report).

Pure-stdlib and import-light: nothing here imports the `wreath` package or the
native `_core`, so the analyzer runs standalone (design 07's "never import the
target" constraint applies to the tool itself too — it must analyze source without
importing wreath's own heavy runtime).
"""
from __future__ import annotations

from .analyzer import TreeContext, analyze, analyze_all
from .emit import PortResult, emit_module, port_tree
from .ir import NEEDS_REVIEW, TRANSLATED, UNSUPPORTED, Finding, Report, SkippedFile

__all__ = [
    "analyze",
    "analyze_all",
    "TreeContext",
    "emit_module",
    "port_tree",
    "PortResult",
    "Finding",
    "Report",
    "SkippedFile",
    "TRANSLATED",
    "NEEDS_REVIEW",
    "UNSUPPORTED",
]
