"""Public entry point for the ``wreath port`` codemod (design 07).

``wreath port`` statically analyzes an existing FastAPI/Pydantic/ormar/SQLModel
application and reports what maps 1:1 to wreath, what needs review, and what has no
equivalent — never importing the target. Phase 0 (this cut) is report-only; code
emission (Phase 1) is deferred (see ``wreath._port.emit``).

    from wreath.port import analyze, analyze_all
    report = analyze_all(["path/to/app"])
    print(report.to_markdown())
"""
from __future__ import annotations

from ._port import (
    NEEDS_REVIEW,
    TRANSLATED,
    UNSUPPORTED,
    Finding,
    PortResult,
    Report,
    SkippedFile,
    analyze,
    analyze_all,
    emit_module,
    port_tree,
)

__all__ = [
    "analyze",
    "analyze_all",
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
