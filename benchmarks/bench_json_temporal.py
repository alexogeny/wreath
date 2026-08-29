"""Microbenchmark: what a timestamp costs a JSON response.

`wreath._json.dumps` encodes, and on TypeError rebuilds the payload through
`temporal.jsonable` and encodes again. The retry is cheap; the *rebuild* is not
-- it reconstructs every dict and list in the document in Python, so one
`created_at` column made encoding many times slower. This measures the tax by
encoding the same rows with and without a temporal field.

    python -m benchmarks.bench_json_temporal --output benchmark-results-json-temporal/after.json

The `ratio` column is the point: it is the multiplier a response pays purely for
carrying timestamps, which is the common ORM shape rather than an edge case.
"""

from __future__ import annotations

import argparse
import datetime
import json
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from wreath._json import dumps
from wreath.temporal import format_iso, jsonable

RESOLUTION_FACTOR = 2.0
_AT = datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_PLAIN = {"id": 0, "email": "a@b.c", "name": "A", "active": True, "score": 1.5}


def _rows(count: int, temporal: bool) -> dict[str, Any]:
    row = dict(_PLAIN, created_at=_AT) if temporal else dict(_PLAIN)
    return {"rows": [dict(row, id=index) for index in range(count)], "total": count}


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
    parser.add_argument("--counts", type=int, nargs="+", default=[1, 10, 100, 1000])
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    scenarios = []
    for count in args.counts:
        iterations = max(20, 4000 // max(1, count))
        warmup = max(10, iterations // 4)
        plain, floor_p = _measure(dumps, _rows(count, False), args.rounds, iterations, warmup)
        timed, floor_t = _measure(dumps, _rows(count, True), args.rounds, iterations, warmup)
        # The old path, kept as the comparison point: rebuild then encode.
        payload = _rows(count, True)
        rebuild, floor_r = _measure(jsonable, payload, args.rounds, iterations, warmup)
        scenarios.append(
            {
                "rows": count,
                "iterations": iterations,
                "no_temporal_us": round(plain, 3),
                "with_temporal_us": round(timed, 3),
                "ratio": round(timed / plain, 3),
                "jsonable_rebuild_us": round(rebuild, 3),
                "aa_floor_us": round(max(floor_p, floor_t, floor_r), 3),
                "resolution_us": round(max(floor_p, floor_t, floor_r) * RESOLUTION_FACTOR, 3),
            }
        )
        print(
            f"rows={count:5d}  none={plain:9.2f}us  temporal={timed:9.2f}us  "
            f"ratio={timed / plain:5.2f}x   (jsonable rebuild alone "
            f"{rebuild:9.2f}us)"
        )

    document = {
        "python": sys.version,
        "platform": platform.platform(),
        "implementation": sys.implementation.name,
        "rounds": args.rounds,
        "note": (
            "jsonable_rebuild_us is the cost the encoder no longer pays once "
            "temporal values are rendered inline; it is reported to show the "
            "rebuild was the dominant term, not the formatting. format_iso "
            f"itself is ~{_time(format_iso, _AT, 20000):.2f}us per value."
        ),
        "scenarios": scenarios,
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2) + "\n")
        print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
