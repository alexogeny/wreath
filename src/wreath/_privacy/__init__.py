"""Implementation of `wreath.privacy`. Import the facade, not this package."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .declare import (
        Classified,
        Personal,
        Subject,
        classified,
        declare_model,
        declare_registry,
    )
    from .execute import (
        ErasureBlocked,
        ErasureIncomplete,
        PlanMoved,
        PreparedErasure,
        prepare,
        record_erasure,
    )
    from .graph import CATALOG_EDGES, Graph, build_graph, catalog_edge_rows, missing_edges
    from .model import (
        ColumnAction,
        CycleFinding,
        Disposal,
        Edge,
        Erase,
        ErasurePlan,
        ExportPlan,
        OrphanRisk,
        Pseudonymise,
        Reach,
        Retained,
        SurvivingReference,
        TableAction,
        Unreachable,
        as_dict,
    )
    from .planner import build_export_plan, build_plan
    from .record import ERASURE_TABLE, ErasureRecord, erasure_log
    from .registry import (
        Classification,
        PrivacyDeclarationError,
        PrivacyRegistry,
        Retention,
    )
    from .render import render_export_text, render_json, render_text
    from .retention import describe_retention, retention_passes, schema_sql

__all__ = [
    "CATALOG_EDGES",
    "ERASURE_TABLE",
    "Classification",
    "Classified",
    "ColumnAction",
    "CycleFinding",
    "Disposal",
    "Edge",
    "Erase",
    "ErasureBlocked",
    "ErasureIncomplete",
    "ErasurePlan",
    "ErasureRecord",
    "ExportPlan",
    "Graph",
    "OrphanRisk",
    "Personal",
    "PlanMoved",
    "PreparedErasure",
    "PrivacyDeclarationError",
    "PrivacyRegistry",
    "Pseudonymise",
    "Reach",
    "Retained",
    "Retention",
    "Subject",
    "SurvivingReference",
    "TableAction",
    "Unreachable",
    "as_dict",
    "build_export_plan",
    "build_graph",
    "build_plan",
    "catalog_edge_rows",
    "classified",
    "declare_model",
    "declare_registry",
    "describe_retention",
    "erasure_log",
    "missing_edges",
    "prepare",
    "record_erasure",
    "render_export_text",
    "render_json",
    "render_text",
    "retention_passes",
    "schema_sql",
]

_EXPORTS = {
    "CATALOG_EDGES": "graph",
    "ERASURE_TABLE": "record",
    "Classification": "registry",
    "Classified": "declare",
    "ColumnAction": "model",
    "CycleFinding": "model",
    "Disposal": "model",
    "Edge": "model",
    "Erase": "model",
    "ErasureBlocked": "execute",
    "ErasureIncomplete": "execute",
    "ErasurePlan": "model",
    "ErasureRecord": "record",
    "ExportPlan": "model",
    "Graph": "graph",
    "OrphanRisk": "model",
    "Personal": "declare",
    "PlanMoved": "execute",
    "PreparedErasure": "execute",
    "PrivacyDeclarationError": "registry",
    "PrivacyRegistry": "registry",
    "Pseudonymise": "model",
    "Reach": "model",
    "Retained": "model",
    "Retention": "registry",
    "Subject": "declare",
    "SurvivingReference": "model",
    "TableAction": "model",
    "Unreachable": "model",
    "as_dict": "model",
    "build_export_plan": "planner",
    "build_graph": "graph",
    "build_plan": "planner",
    "catalog_edge_rows": "graph",
    "classified": "declare",
    "declare_model": "declare",
    "declare_registry": "declare",
    "describe_retention": "retention",
    "erasure_log": "record",
    "missing_edges": "graph",
    "prepare": "execute",
    "record_erasure": "execute",
    "render_export_text": "render",
    "render_json": "render",
    "render_text": "render",
    "retention_passes": "retention",
    "schema_sql": "retention",
}

_MODULE_EXPORTS = {
    "declare": (
        "Classified",
        "Personal",
        "Subject",
        "classified",
        "declare_model",
        "declare_registry",
    ),
    "execute": (
        "ErasureBlocked",
        "ErasureIncomplete",
        "PlanMoved",
        "PreparedErasure",
        "prepare",
        "record_erasure",
    ),
    "graph": ("CATALOG_EDGES", "Graph", "build_graph", "catalog_edge_rows", "missing_edges"),
    "model": (
        "ColumnAction",
        "CycleFinding",
        "Disposal",
        "Edge",
        "Erase",
        "ErasurePlan",
        "ExportPlan",
        "OrphanRisk",
        "Pseudonymise",
        "Reach",
        "Retained",
        "SurvivingReference",
        "TableAction",
        "Unreachable",
        "as_dict",
    ),
    "planner": ("build_export_plan", "build_plan"),
    "record": ("ERASURE_TABLE", "ErasureRecord", "erasure_log"),
    "registry": (
        "Classification",
        "PrivacyDeclarationError",
        "PrivacyRegistry",
        "Retention",
    ),
    "render": ("render_export_text", "render_json", "render_text"),
    "retention": ("describe_retention", "retention_passes", "schema_sql"),
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
