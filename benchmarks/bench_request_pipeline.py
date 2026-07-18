"""Compare legacy probe/rematch routing with single-pass classification.

This is a focused in-process router benchmark. It deliberately measures both
algorithms against the same compiled table so before/after structural costs can
be compared without server or load-generator noise.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
from collections.abc import Callable
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from wreath import Router, Wreath


def build_application(branches: int, leaves: int) -> tuple[Wreath, str, str]:
    protected = Router(prefix="/control", permissions=("control:access",))
    target = ""
    for branch in range(branches):
        tenant = Router(prefix=f"/tenant-{branch}")
        for leaf in range(leaves):
            path = f"/resource-{leaf}/items/{{item_id}}"

            async def endpoint(request: Any, _branch: int = branch, _leaf: int = leaf) -> str:
                return f"{_branch}:{_leaf}"

            tenant.get(path)(endpoint)
            if branch == branches - 1 and leaf == leaves - 1:
                target = f"/control/tenant-{branch}/resource-{leaf}/items/42"
        protected.include_router(tenant)

    app = Wreath()
    app.include_router(protected)

    @app.get("/public/{item_id}")
    async def public(request: Any) -> str:
        return request.path_params["item_id"]

    app._compile_routes()
    return app, target, "/definitely-missing"


def sample(operation: Callable[[], Any], iterations: int, trials: int) -> list[float]:
    values: list[float] = []
    for _ in range(trials):
        for _ in range(max(100, iterations // 100)):
            operation()
        started = perf_counter_ns()
        for _ in range(iterations):
            operation()
        values.append((perf_counter_ns() - started) / iterations)
    return values


def summarize(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "median_ns": statistics.median(values),
        "min_ns": ordered[0],
        "max_ns": ordered[-1],
        "trials_ns": values,
    }


def run(branches: int, leaves: int, iterations: int, trials: int) -> dict[str, Any]:
    app, protected_path, missing_path = build_application(branches, leaves)
    table = app.router._table
    all_mask = app._all_capability_mask
    allowed_mask = all_mask

    def legacy_public() -> Any:
        return table.match("GET", "/public/42", 0)

    def classified_public() -> Any:
        return table.classify("GET", "/public/42")

    def legacy_missing() -> Any:
        public = table.match("GET", missing_path, 0)
        return public if public is not None else table.match("GET", missing_path, all_mask)

    def classified_missing() -> Any:
        return table.classify("GET", missing_path)

    def legacy_protected_allow() -> Any:
        public = table.match("GET", protected_path, 0)
        if public is not None:
            return public
        protected = table.match("GET", protected_path, all_mask)
        if protected is None:
            return None
        return table.match("GET", protected_path, allowed_mask)

    def classified_protected_allow() -> Any:
        classification, payload = table.classify("GET", protected_path)
        return table.resolve(payload, allowed_mask) if classification == 2 else payload

    def legacy_protected_deny() -> Any:
        public = table.match("GET", protected_path, 0)
        if public is not None:
            return public
        protected = table.match("GET", protected_path, all_mask)
        if protected is None:
            return None
        return table.match("GET", protected_path, 0)

    def classified_protected_deny() -> Any:
        classification, payload = table.classify("GET", protected_path)
        return table.resolve(payload, 0) if classification == 2 else payload

    operations = {
        "legacy-public": legacy_public,
        "classified-public": classified_public,
        "legacy-missing": legacy_missing,
        "classified-missing": classified_missing,
        "legacy-protected-allow": legacy_protected_allow,
        "classified-protected-allow": classified_protected_allow,
        "legacy-protected-deny": legacy_protected_deny,
        "classified-protected-deny": classified_protected_deny,
    }
    results = {
        name: summarize(sample(operation, iterations, trials))
        for name, operation in operations.items()
    }
    return {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "routes": branches * leaves + 1,
            "branches": branches,
            "leaves": leaves,
            "iterations": iterations,
            "trials": trials,
            "router_type": type(table).__name__,
        },
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--branches", type=int, default=24)
    parser.add_argument("--leaves", type=int, default=16)
    parser.add_argument("--iterations", type=int, default=100_000)
    parser.add_argument("--trials", type=int, default=7)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.branches, args.leaves, args.iterations, args.trials)
    for name, summary in result["results"].items():
        print(f"{name:28} {summary['median_ns']:10.1f} ns")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
