"""Implementation of `wreath.privacy`. Import the facade, not this package."""

from __future__ import annotations

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
