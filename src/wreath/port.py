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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._port.analyzer import TreeContext, analyze, analyze_all
    from ._port.emit import PortResult, emit_module, port_tree
    from ._port.inventory import (
        MigrationInventory,
        PolicyCandidate,
        ProjectReport,
        RouteContract,
        inventory_projects,
    )
    from ._port.ir import NEEDS_REVIEW, TRANSLATED, UNSUPPORTED, Finding, Report, SkippedFile
    from ._port.verify import (
        Difference,
        RequestCase,
        ResponseSnapshot,
        VerificationReport,
        load_cases,
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

_EXPORTS = {
    "analyze": "_port.analyzer",
    "analyze_all": "_port.analyzer",
    "TreeContext": "_port.analyzer",
    "emit_module": "_port.emit",
    "port_tree": "_port.emit",
    "PortResult": "_port.emit",
    "Finding": "_port.ir",
    "Report": "_port.ir",
    "SkippedFile": "_port.ir",
    "TRANSLATED": "_port.ir",
    "NEEDS_REVIEW": "_port.ir",
    "UNSUPPORTED": "_port.ir",
    "MigrationInventory": "_port.inventory",
    "PolicyCandidate": "_port.inventory",
    "ProjectReport": "_port.inventory",
    "RouteContract": "_port.inventory",
    "inventory_projects": "_port.inventory",
    "Difference": "_port.verify",
    "RequestCase": "_port.verify",
    "ResponseSnapshot": "_port.verify",
    "VerificationReport": "_port.verify",
    "load_cases": "_port.verify",
    "verify_apps": "_port.verify",
}

_MODULE_EXPORTS = {
    "_port.analyzer": ("analyze", "analyze_all", "TreeContext"),
    "_port.emit": ("emit_module", "port_tree", "PortResult"),
    "_port.ir": (
        "Finding",
        "Report",
        "SkippedFile",
        "TRANSLATED",
        "NEEDS_REVIEW",
        "UNSUPPORTED",
    ),
    "_port.inventory": (
        "MigrationInventory",
        "PolicyCandidate",
        "ProjectReport",
        "RouteContract",
        "inventory_projects",
    ),
    "_port.verify": (
        "Difference",
        "RequestCase",
        "ResponseSnapshot",
        "VerificationReport",
        "load_cases",
        "verify_apps",
    ),
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    loaded = import_module(f".{module}", __package__)
    namespace = globals()
    for export in _MODULE_EXPORTS[module]:
        namespace[export] = getattr(loaded, export)
    return namespace[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
