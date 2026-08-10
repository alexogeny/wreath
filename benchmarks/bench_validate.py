"""Microbenchmark: native vs pure request-body validation.

Compiles one plan per body shape and times the complete validate call (the unit
the binder invokes per request) for the Python and C validators, interleaved, with an A/A
control fixing the noise floor. Reports per-body microseconds and speedup.

    python -m benchmarks.bench_validate --output benchmark-results-validate/latest.json
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from wreath._native import _core
from wreath.binding import _compile_plan, validate


@dataclass
class Address:
    street: str
    city: str
    zip: str | None = None


@dataclass
class Item:
    name: str
    price: float
    quantity: int
    active: bool
    tags: list[str] = field(default_factory=list)
    address: Address | None = None
    meta: dict[str, int] = field(default_factory=dict)


SHAPES: dict[str, tuple[Any, Any]] = {
    "flat": (
        Address,
        {"street": "1 Main", "city": "Springfield", "zip": "12345"},
    ),
    "nested": (
        Item,
        {
            "name": "widget",
            "price": 19.99,
            "quantity": 3,
            "active": True,
            "tags": ["a", "b", "c"],
            "address": {"street": "s", "city": "c", "zip": "1"},
            "meta": {"x": 1, "y": 2},
        },
    ),
    "list": (list[int], list(range(50))),
}


def _time(fn: Any, iterations: int) -> float:
    start = time.perf_counter()
    fn(iterations)
    return (time.perf_counter() - start) / iterations * 1e6  # us


def run(shape: str, rounds: int, iterations: int, warmup: int) -> dict[str, Any]:
    annotation, payload = SHAPES[shape]
    plan = _compile_plan(annotation, frozenset())
    loc = ("body",)
    run_validation = _core.run_validation

    def pure(n: int) -> None:
        for _ in range(n):
            validate(annotation, payload, loc)

    def native(n: int) -> None:
        for _ in range(n):
            run_validation(plan, payload, loc)

    pure(warmup)
    native(warmup)
    pure_samples: list[float] = []
    native_samples: list[float] = []
    aa_samples: list[float] = []
    for _ in range(rounds):
        pure_samples.append(_time(pure, iterations))
        native_samples.append(_time(native, iterations))
        aa_samples.append(_time(native, iterations))  # A/A twin of native
    pure_median = statistics.median(pure_samples)
    native_median = statistics.median(native_samples)
    floor = abs(native_median - statistics.median(aa_samples))
    return {
        "shape": shape,
        "pure_us": round(pure_median, 4),
        "native_us": round(native_median, 4),
        "speedup": round(pure_median / native_median, 3) if native_median else None,
        "noise_floor_us": round(floor, 4),
        "resolved": abs(pure_median - native_median) > 2 * floor,
        "pure_samples": [round(s, 4) for s in pure_samples],
        "native_samples": [round(s, 4) for s in native_samples],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape", nargs="+", choices=SHAPES, default=list(SHAPES))
    parser.add_argument("--rounds", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=20000)
    parser.add_argument("--warmup", type=int, default=2000)
    parser.add_argument("--output")
    args = parser.parse_args()
    if _core is None:
        raise SystemExit("native core not built; nothing to compare")
    results = [run(shape, args.rounds, args.iterations, args.warmup) for shape in args.shape]
    for entry in results:
        print(
            f"{entry['shape']:8} pure={entry['pure_us']:7.3f}us "
            f"native={entry['native_us']:7.3f}us "
            f"speedup={entry['speedup']:.2f}x "
            f"({'resolved' if entry['resolved'] else 'BELOW NOISE'})"
        )
    document = {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "rounds": args.rounds,
            "iterations": args.iterations,
            "warmup": args.warmup,
        },
        "results": results,
    }
    if args.output:
        from pathlib import Path

        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
