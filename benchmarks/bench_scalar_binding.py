"""Microbenchmark: what typed handler parameters cost a request.

Request *bodies* are validated by a compiled plan interpreted in C
(`_native/validate.c`), and the obvious next step looked like doing the same for
path/query/header/cookie parameters, which the binder converts in Python.

**The `manual ...(no binder)` arms exist to refute that, and they did.** They run
the identical conversion by hand in a plain `(request)` handler, so the gap
between `manual path int` and `path int only` is the binder's per-request
scaffolding rather than the conversion. Measured 2026-07-27: the conversion sat
at or barely above a ~0.06us floor, while the scaffolding cost ~1.0us -- four
container allocations, a try/finally, an unconditional `await _release([], [])`,
and a `reversed()` over an empty list, all for a handler that leases nothing.
Moving `_convert_scalar` into C would have chased ~0.15us per parameter and left
the larger cost untouched. The fix was to compile that machinery away when the
spec needs none of it; keep these arms so the next person cannot re-derive the
wrong conclusion.

Whole requests through a real app, ablation-style -- AGENTS.md: do not reach for
cProfile on these paths.

    python -m benchmarks.bench_scalar_binding --output benchmark-results-binding/after.json

Arms are interleaved and an A/A control sits at the far end of each round, so
the reported floor includes within-round drift. A delta below twice the floor is
unresolved, not zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from pathlib import Path
from typing import Annotated, Any

from wreath import Wreath
from wreath._devtools import measure
from wreath.binding import Query


def _app_untyped() -> Any:
    """The floor: a handler the binder returns unchanged."""
    app = Wreath()

    @app.get("/items/{item_id}")
    async def handler(request: Any) -> dict[str, int]:
        return {"ok": 1}

    return app


def _app_path_only() -> Any:
    app = Wreath()

    @app.get("/items/{item_id}")
    async def handler(request: Any, item_id: int) -> dict[str, int]:
        return {"ok": item_id}

    return app


def _app_typed(query_count: int) -> Any:
    """One path param plus `query_count` typed query params with defaults."""
    app = Wreath()
    names = [f"q{index}" for index in range(query_count)]

    namespace: dict[str, Any] = {}
    params = ", ".join(f"{name}: int = {index}" for index, name in enumerate(names))
    source = (
        "async def handler(request, item_id: int"
        + (", " + params if params else "")
        + "):\n    return {'ok': item_id}\n"
    )
    exec(source, {"Annotated": Annotated, "Query": Query}, namespace)
    app.get("/items/{item_id}")(namespace["handler"])
    return app


def _app_manual_path() -> Any:
    """The same conversion, done by hand in a `(request)` handler.

    The binder is bypassed entirely, so the gap between this and `path int only`
    is the binder's per-request scaffolding rather than the int() conversion --
    which is the question that decides whether the fix belongs in C or in Python.
    """
    app = Wreath()

    @app.get("/items/{item_id}")
    async def handler(request: Any) -> dict[str, int]:
        item_id = int(request.path_params["item_id"])
        return {"ok": item_id}

    return app


def _app_manual_query() -> Any:
    """Path int plus two query values, converted by hand."""
    from wreath._native._core import parse_qs

    app = Wreath()

    @app.get("/items/{item_id}")
    async def handler(request: Any) -> dict[str, int]:
        item_id = int(request.path_params["item_id"])
        values: dict[str, str] = {}
        for key, value in parse_qs(request.query_string):
            values.setdefault(key, value)
        limit = int(values.get("limit", "10"))
        return {"ok": item_id + limit}

    return app


def _app_mixed() -> Any:
    """A realistic mix: path int, two query, one header, one cookie."""
    app = Wreath()

    @app.get("/items/{item_id}")
    async def handler(
        request: Any,
        item_id: int,
        limit: int = 10,
        cursor: str = "",
    ) -> dict[str, int]:
        return {"ok": item_id}

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=measure.DEFAULT_ROUNDS)
    parser.add_argument("--iterations", type=int, default=3000)
    parser.add_argument("--warmup", type=int, default=1500)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    arms = [
        measure.Arm("untyped (binder bypassed)", _app_untyped()),
        measure.Arm("manual path int (no binder)", _app_manual_path()),
        measure.Arm("path int only", _app_path_only()),
        measure.Arm("manual path+query (no binder)", _app_manual_query()),
        measure.Arm("path + 2 query", _app_mixed()),
        measure.Arm("path + 4 query", _app_typed(4)),
        measure.Arm("path + 8 query", _app_typed(8)),
        # A/A control: same configuration as the baseline, entered last so the
        # floor includes a full round of drift.
        measure.Arm("control (untyped again)", _app_untyped()),
    ]
    template = measure.scope(path="/items/42?limit=5&cursor=abc&q0=1&q1=2&q2=3&q3=4"
                             "&q4=5&q5=6&q6=7&q7=8")

    asyncio.run(
        measure.measure_apps(
            arms, template, args.rounds, args.iterations, args.warmup
        )
    )
    document = measure.report(
        arms, "untyped (binder bypassed)", "control (untyped again)"
    )
    document.update(
        {
            "python": sys.version,
            "platform": platform.platform(),
            "implementation": sys.implementation.name,
            "rounds": args.rounds,
            "iterations": args.iterations,
        }
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2) + "\n")
        print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
