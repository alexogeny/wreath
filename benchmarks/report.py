"""Generate a self-contained HTML benchmark report from raw result data.

The renderer itself lives in ``wreath._devtools.bench_report`` because console
scripts must point at an installed package and ``benchmarks/`` is not one (see
``[tool.setuptools.packages.find]``). Keeping one implementation means the live
``latest.html`` this module writes and the ``wreath-bench-report`` CLI can never
render differently.

    uv run wreath-bench-report benchmark-results/ --open
"""

from __future__ import annotations

from wreath._devtools.bench_report import (
    _chart,
    generate_report,
    has_mixed_generators,
    has_mixed_protocols,
    is_rankable,
    merge_documents,
    render,
)

__all__ = [
    "_chart",
    "generate_report",
    "has_mixed_generators",
    "has_mixed_protocols",
    "is_rankable",
    "merge_documents",
    "render",
]
