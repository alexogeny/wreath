"""Measure eligible and capability-pruned matches in a large nested Router tree.

This is a focused framework/router benchmark, not an end-to-end server result.
It builds a protected parent Router containing many tenant subrouters, compiles
it through Wreath, and compares a fully eligible leaf match with an anonymous
match that should reject the protected subtree from its root capability
summary without traversing descendant decision nodes.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from wreath import Router, Wreath


def build_routers(branches: int, leaves: int) -> tuple[Wreath, str]:
    """Build the app and register routes, without compiling."""
    protected = Router(prefix="/control", permissions=("control:access",))

    async def endpoint(request: Any) -> bytes:
        return b"ok"

    for branch in range(branches):
        tenant = Router(
            prefix=f"/tenant-{branch}",
            permissions=(f"tenant:{branch}:read",),
        )
        for leaf in range(leaves):
            tenant.get(f"/services/group-{leaf % 10}/resource-{leaf}/{{item_id}}")(
                endpoint
            )
        protected.include_router(tenant)

    app = Wreath(routing="decision")
    app.include_router(protected)
    target = (
        f"/control/tenant-{branches - 1}/services/"
        f"group-{(leaves - 1) % 10}/resource-{leaves - 1}/42"
    )
    return app, target


def build_application(branches: int, leaves: int) -> tuple[Wreath, str]:
    app, target = build_routers(branches, leaves)
    app._compile_routes()
    return app, target


def compile_trials(branches: int, leaves: int, trials: int) -> list[float]:
    """Time compilation only; route construction stays outside the timer."""
    samples: list[float] = []
    for _ in range(trials):
        app, _target = build_routers(branches, leaves)
        started = perf_counter_ns()
        app._compile_routes()
        samples.append((perf_counter_ns() - started) / 1e9)
    return samples


def implementation_name() -> str:
    """Report whether the decision router resolved to native or pure Python."""
    try:
        import wreath._native._core as core

        if getattr(core, "DecisionRouteTable", None) is not None:
            return f"native ({core.__file__})"
    except ImportError:
        pass
    return "pure"


def sample(match: Any, path: str, mask: int, iterations: int, trials: int) -> list[float]:
    samples: list[float] = []
    for _ in range(trials):
        started = perf_counter_ns()
        for _iteration in range(iterations):
            match("GET", path, mask)
        elapsed = perf_counter_ns() - started
        samples.append(elapsed / iterations)
    return samples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--branches", type=int, default=100)
    parser.add_argument("--leaves", type=int, default=100)
    parser.add_argument("--iterations", type=int, default=100_000)
    parser.add_argument("--trials", type=int, default=7)
    parser.add_argument("--compile-trials", type=int, default=5)
    parser.add_argument("--output", type=Path, default=None,
                        help="write the JSON document here instead of stdout")
    args = parser.parse_args()

    compile_raw = compile_trials(args.branches, args.leaves, args.compile_trials)
    app, target = build_application(args.branches, args.leaves)
    match = app._match
    capabilities = app._capabilities
    eligible_mask = (
        capabilities["authenticated"]
        | capabilities["permission:control:access"]
        | capabilities[f"permission:tenant:{args.branches - 1}:read"]
    )

    eligible = sample(match, target, eligible_mask, args.iterations, args.trials)
    pruned = sample(match, target, 0, args.iterations, args.trials)
    assert match("GET", target, eligible_mask) is not None
    assert match("GET", target, 0) is None

    document = {
        "tool": "benchmarks.bench_router_pruning",
        "schema_version": 1,
        "python": sys.version,
        "platform": platform.platform(),
        "implementation": sys.implementation.name,
        "executable": sys.executable,
        "routing_implementation": implementation_name(),
        "shape": {
            "branches": args.branches,
            "leaves": args.leaves,
            "routes": args.branches * args.leaves,
            "target_path": target,
            "description": (
                "nested protected routers with inherited permissions; a repeated "
                "literal group, a distinct per-leaf literal, and one path parameter"
            ),
        },
        "compile": {
            "trials": args.compile_trials,
            "raw_seconds": compile_raw,
            "median_seconds": statistics.median(compile_raw),
        },
        "match_iterations": args.iterations,
        "match_trials": args.trials,
        "eligible_ns_per_match": {
            "raw": eligible,
            "median": statistics.median(eligible),
        },
        "pruned_ns_per_match": {
            "raw": pruned,
            "median": statistics.median(pruned),
        },
    }
    text = json.dumps(document, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
        print(f"wrote {args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
