"""`wreath port` codemod internals: analysis, emission, and verification.

Pure-stdlib and import-light: nothing here imports the `wreath` package or the
native `_core`, so the analyzer runs standalone (design 07's "never import the
target" constraint applies to the tool itself too — it must analyze source without
importing wreath's own heavy runtime).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .analyzer import TreeContext, analyze, analyze_all
    from .emit import PortResult, emit_module, port_tree
    from .inventory import (
        MigrationInventory,
        PolicyCandidate,
        ProjectReport,
        RouteContract,
        inventory_projects,
    )
    from .ir import NEEDS_REVIEW, TRANSLATED, UNSUPPORTED, Finding, Report, SkippedFile
    from .verify import (
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
    "analyze": "analyzer",
    "analyze_all": "analyzer",
    "TreeContext": "analyzer",
    "emit_module": "emit",
    "port_tree": "emit",
    "PortResult": "emit",
    "Finding": "ir",
    "Report": "ir",
    "SkippedFile": "ir",
    "TRANSLATED": "ir",
    "NEEDS_REVIEW": "ir",
    "UNSUPPORTED": "ir",
    "MigrationInventory": "inventory",
    "PolicyCandidate": "inventory",
    "ProjectReport": "inventory",
    "RouteContract": "inventory",
    "inventory_projects": "inventory",
    "Difference": "verify",
    "RequestCase": "verify",
    "ResponseSnapshot": "verify",
    "VerificationReport": "verify",
    "load_cases": "verify",
    "verify_apps": "verify",
}

_MODULE_EXPORTS = {
    "analyzer": ("analyze", "analyze_all", "TreeContext"),
    "emit": ("emit_module", "port_tree", "PortResult"),
    "ir": (
        "Finding",
        "Report",
        "SkippedFile",
        "TRANSLATED",
        "NEEDS_REVIEW",
        "UNSUPPORTED",
    ),
    "inventory": (
        "MigrationInventory",
        "PolicyCandidate",
        "ProjectReport",
        "RouteContract",
        "inventory_projects",
    ),
    "verify": (
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

    loaded = import_module(f".{module}", __name__)
    namespace = globals()
    for export in _MODULE_EXPORTS[module]:
        namespace[export] = getattr(loaded, export)
    return namespace[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
