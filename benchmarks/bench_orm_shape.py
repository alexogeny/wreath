"""Microbenchmark: native vs pure ORM per-read tree walks.

Both `shape_of` (the plan-cache key) and `_collect_binds` (the bind values) walk
the query's predicate tree on every read. The native builders skip the per-node
Python recursion frame; `shape_of` writes the key into one buffer, and
`collect_binds` collects the value nodes in C (leaving only the flat to_wire
loop in Python). Measured across predicate depths, interleaved, with an A/A
floor.

    python -m benchmarks.bench_orm_shape --output benchmark-results-orm-shape/latest.json
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from typing import Any

from wreath._native import _core
from wreath.orm import and_, or_
from wreath.orm.compiler import _collect_binds_native, _collect_binds_pure, _shape_of_pure


def _build() -> tuple[Any, dict[str, Any]]:
    import sys as _sys
    from pathlib import Path

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
    from orm.conftest import FakeDatabase, Membership, Post, User  # type: ignore

    from wreath.orm.registry import Registry

    registry = Registry(FakeDatabase(), [User, Post, Membership], validate_schema="off")
    shapes = {
        "all-cols": User.select(),
        "1-pred": User.select().where(User.id == 5),
        "2-pred-and": User.select().where(and_(User.id == 5, User.email == "a")),
        "3-pred-and": User.select().where(
            and_(User.id == 5, User.email == "a", User.name == "b")
        ),
        "nested-bool": User.select().where(
            and_(User.id > 3, or_(User.email == "x", User.name == "y"))
        ),
        "pred+order+limit": User.select().where(User.id == 5)
        .order_by(User.created_at)
        .limit(10),
    }
    return registry, shapes


def _time(fn: Any, registry: Any, query: Any, iterations: int) -> float:
    start = time.perf_counter()
    for _ in range(iterations):
        fn(registry, query)
    return (time.perf_counter() - start) / iterations * 1e6


def _measure(pure: Any, native: Any, query: Any, rounds: int, iterations: int) -> dict[str, Any]:
    pure_s, nat_s, aa_s = [], [], []
    for _ in range(rounds):
        pure_s.append(_time(pure, None, query, iterations))
        nat_s.append(_time(native, None, query, iterations))
        aa_s.append(_time(native, None, query, iterations))
    pure_m = statistics.median(pure_s)
    nat_m = statistics.median(nat_s)
    floor = abs(nat_m - statistics.median(aa_s))
    return {
        "pure_us": round(pure_m, 4),
        "native_us": round(nat_m, 4),
        "speedup": round(pure_m / nat_m, 3) if nat_m else None,
        "noise_floor_us": round(floor, 4),
        "resolved": abs(pure_m - nat_m) > 2 * floor,
    }


def run(rounds: int, iterations: int, warmup: int) -> dict[str, Any]:
    registry, shapes = _build()
    native_shape = _core.orm_shape

    def pure_shape(_ignored: Any, query: Any) -> Any:
        return _shape_of_pure(registry, query)

    def nat_shape(_ignored: Any, query: Any) -> Any:
        return native_shape(registry, query)

    def pure_binds(_ignored: Any, query: Any) -> Any:
        return _collect_binds_pure(query)

    def nat_binds(_ignored: Any, query: Any) -> Any:
        return _collect_binds_native(query)

    results = []
    for name, query in shapes.items():
        # Parity gate before timing: identical outputs or the comparison is void.
        assert native_shape(registry, query) == _shape_of_pure(registry, query)
        assert _collect_binds_native(query) == _collect_binds_pure(query)
        for _ in range(warmup):
            nat_shape(None, query)
            nat_binds(None, query)
        results.append({
            "shape": name,
            "shape_of": _measure(pure_shape, nat_shape, query, rounds, iterations),
            "collect_binds": _measure(pure_binds, nat_binds, query, rounds, iterations),
        })
    return {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "rounds": rounds,
            "iterations": iterations,
            "warmup": warmup,
        },
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=50000)
    parser.add_argument("--warmup", type=int, default=2000)
    parser.add_argument("--output")
    args = parser.parse_args()
    if _core is None or not hasattr(_core, "orm_shape"):
        raise SystemExit("native orm_shape not built; nothing to compare")
    document = run(args.rounds, args.iterations, args.warmup)
    for entry in document["results"]:
        for op in ("shape_of", "collect_binds"):
            data = entry[op]
            print(
                f"{entry['shape']:18} {op:14} pure={data['pure_us']:7.3f}us "
                f"native={data['native_us']:7.3f}us speedup={data['speedup']:.2f}x "
                f"({'resolved' if data['resolved'] else 'BELOW NOISE'})"
            )
    if args.output:
        from pathlib import Path

        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
