"""Microbenchmark: msgpack encoding against the JSON encoder.

`wreath.negotiation` offers JSON or msgpack from one `serialize(request, data)`
call, and both encoders are C. Clients ask for msgpack to spend fewer bytes, so
this reports both the size it saves and what it costs to produce -- the two
numbers that decide whether asking for it is worth it.

msgpack once went to a recursive Python packer, which made the format chosen for
efficiency the slower one to produce. That is what closing this gap fixed.

    python -m benchmarks.bench_msgpack --output benchmark-results-msgpack/before.json

Arms are interleaved with an A/A control per round, per the rules in
`src/wreath/_devtools/measure.py`; a delta below twice that floor is unresolved.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from wreath._native import _core

RESOLUTION_FACTOR = 2.0


def _payloads() -> dict[str, Any]:
    """Shapes a negotiated endpoint actually returns."""
    row = {
        "id": 1234,
        "email": "rider@example.com",
        "name": "A Rider",
        "active": True,
        "score": 12.5,
        "tags": ["alpha", "beta"],
    }
    return {
        "scalar": 12345,
        "small-map": row,
        "rows-100": {"rows": [dict(row, id=index) for index in range(100)]},
        "rows-1000": {"rows": [dict(row, id=index) for index in range(1000)]},
        "wide-strings": {"rows": [{"text": "x" * 512} for _ in range(200)]},
        "deep-nest": {"a": {"b": {"c": {"d": {"e": list(range(200))}}}}},
    }


def _time(fn: Any, payload: Any, iterations: int) -> float:
    start = time.perf_counter()
    for _ in range(iterations):
        fn(payload)
    return (time.perf_counter() - start) / iterations * 1e6


def _measure(
    fn: Any, payload: Any, rounds: int, iterations: int, warmup: int
) -> tuple[float, float]:
    _time(fn, payload, warmup)
    main: list[float] = []
    control: list[float] = []
    for _ in range(rounds):
        main.append(_time(fn, payload, iterations))
        control.append(_time(fn, payload, iterations))
    median = statistics.median(main)
    return median, abs(median - statistics.median(control))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=9)
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--warmup", type=int, default=500)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    packb = _core.msgpack_dumps
    encoders: list[tuple[str, Any]] = [
        ("json", _core.json_dumps),
        ("msgpack", packb),
    ]

    scenarios: list[dict[str, Any]] = []
    for label, payload in _payloads().items():
        # Big payloads get fewer iterations; the per-call figure stays comparable.
        size = len(packb(payload))
        iterations = max(20, args.iterations // max(1, size // 2048))
        results: dict[str, Any] = {}
        for name, fn in encoders:
            median, floor = _measure(
                fn, payload, args.rounds, iterations, max(10, args.warmup // 4)
            )
            results[name] = {
                "median_us": round(median, 3),
                "aa_floor_us": round(floor, 3),
                "resolution_us": round(floor * RESOLUTION_FACTOR, 3),
            }
        line = f"{label:14s} {size:8d}B"
        for name, _ in encoders:
            line += f"   {name}={results[name]['median_us']:9.2f}us"
        json_bytes = len(results and _core.json_dumps(payload))
        line += f"   msgpack/json bytes={size / json_bytes:5.2f}"
        print(line)
        scenarios.append(
            {
                "payload": label,
                "encoded_bytes": size,
                "iterations": iterations,
                "encoders": results,
            }
        )

    document = {
        "python": sys.version,
        "platform": platform.platform(),
        "implementation": sys.implementation.name,
        "executable": sys.executable,
        "rounds": args.rounds,
        "scenarios": scenarios,
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2) + "\n")
        print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
