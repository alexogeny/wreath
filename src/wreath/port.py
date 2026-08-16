"""Analyze, emit, and behaviourally verify an application port.

`wreath port` statically analyzes an existing FastAPI/Pydantic/ormar/SQLModel
application and reports what maps 1:1 to Wreath, what needs review, and what has no
equivalent, without importing the target. It can emit a sister source tree and
then compare the source and candidate ASGI applications against one declared
HTTP corpus. Analysis and emission remain static; only explicit verification
imports and runs application targets.

    from wreath.port import analyze, analyze_all
    report = analyze_all(["path/to/app"])
    print(report.to_markdown())
"""

from __future__ import annotations

from ._port import (
    NEEDS_REVIEW,
    TRANSLATED,
    UNSUPPORTED,
    Difference,
    Finding,
    MigrationInventory,
    PolicyCandidate,
    PortResult,
    ProjectReport,
    Report,
    RequestCase,
    ResponseSnapshot,
    RouteContract,
    SkippedFile,
    TreeContext,
    VerificationReport,
    analyze,
    analyze_all,
    emit_module,
    inventory_projects,
    load_cases,
    port_tree,
    verify_apps,
)

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
    "MigrationInventory",
    "PolicyCandidate",
    "ProjectReport",
    "RouteContract",
    "inventory_projects",
    "Difference",
    "RequestCase",
    "ResponseSnapshot",
    "VerificationReport",
    "load_cases",
    "verify_apps",
]
