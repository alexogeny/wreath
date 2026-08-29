"""Routing backends head to head: every table implementation on the same tables.

The same comparison ``tests/test_native_perf.py`` asserts the ordering of, but
emitted as JSON so ``wreath-bench-report`` can render it. The test guards against
regressions; this reports the numbers.

Two tables, because the backends do not rank the same way on both:

``small-api``
    Seven routes, the shape of an ordinary application. Mostly static, so most
    matches never reach the backend at all -- the static dict answers them.
``large-shared``
    800 routes sharing two leading parameter positions and differing at one deep
    literal segment: the widest literal fanout the trie's binary search has to
    handle, and the shape the decision tree's hashed branch selection is for.

Not an apples-to-apples race, and the report says so: the decision tree and the
bitset take a caller capability mask and evaluate access clauses; the trie takes
no mask and does no authorization work, because it has no such feature. On these
tables (no access clauses) both pay for machinery they never exercise.

    uv run python -m benchmarks.bench_routing_backends --output PATH
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
from pathlib import Path
from time import perf_counter
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from _routing_impls import IMPLS, build

#: Rendered in this order.
ORDER = ("c-dt", "c-trie", "c-bitset")


def _small_api() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    routes = [
        ("GET", "/"),
        ("GET", "/users"),
        ("GET", "/users/{id}"),
        ("GET", "/users/{id}/posts"),
        ("POST", "/users/{id}/posts"),
        ("GET", "/health"),
        ("GET", "/static/{path}"),
    ]
    rng = random.Random(7)
    choices = [
        ("GET", "/users/42"),
        ("GET", "/health"),
        ("POST", "/users/9/posts"),
        ("GET", "/static/x"),
        ("GET", "/nope"),
        ("GET", "/users/1/posts"),
    ]
    return routes, [rng.choice(choices) for _ in range(20_000)]


def _large_shared() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    routes = [("GET", f"/api/{{a}}/{{b}}/thing{i}/{{c}}") for i in range(800)]
    queries = [
        ("GET", f"/api/1/2/thing{random.Random(i).randint(0, 799)}/3") for i in range(20_000)
    ]
    return routes, queries


TABLES = {
    "small-api": (_small_api, "7 routes, an ordinary application"),
    "large-shared": (_large_shared, "800 routes, one deep literal position"),
}


def _best(table: Any, queries: list[tuple[str, str]], reps: int) -> float:
    """Best-of-N seconds for one full pass. Best-of, not mean: the minimum is the
    run least disturbed by the scheduler."""
    for method, path in queries[:1000]:
        table.match(method, path)  # compile / warm
    best = float("inf")
    for _ in range(reps):
        started = perf_counter()
        for method, path in queries:
            table.match(method, path)
        best = min(best, perf_counter() - started)
    return best


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    tables: list[dict[str, Any]] = []
    for name, (factory, description) in TABLES.items():
        routes, queries = factory()
        backends: dict[str, Any] = {}
        for impl in ORDER:
            if impl not in IMPLS:
                continue
            samples = [
                _best(build(IMPLS[impl], routes), queries, args.reps)
                for _trial in range(args.trials)
            ]
            backends[impl] = {
                "raw_seconds": samples,
                "median_seconds": statistics.median(samples),
                "ns_per_match": statistics.median(samples) / len(queries) * 1e9,
            }
        tables.append(
            {
                "name": name,
                "description": description,
                "routes": len(routes),
                "queries": len(queries),
                "backends": backends,
            }
        )
        print(
            f"{name}: "
            + "  ".join(
                f"{impl} {values['median_seconds'] * 1e3:.2f}ms"
                for impl, values in backends.items()
            )
        )

    document = {
        "tool": "benchmarks.bench_routing_backends",
        "schema_version": 1,
        "python": sys.version,
        "platform": platform.platform(),
        "reps": args.reps,
        "trials": args.trials,
        "caveat": (
            "Not apples to apples: the decision tree and bitset take a caller "
            "capability mask and evaluate access clauses; the trie has no such "
            "feature and does no authorization work."
        ),
        "tables": tables,
    }
    text = json.dumps(document, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
