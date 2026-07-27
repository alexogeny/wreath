"""Microbenchmark: framing one Server-Sent Event.

`wreath.response._encode_sse` carries an explicit TODO(native) reserving a
`_core.sse_frame` slot, on the condition that a benchmark justify it first. This
is that benchmark. Framing runs once per event *per subscriber*, so a fan-out
stream multiplies it; a progress endpoint pushing to a few hundred open tabs
pays it a few hundred times per update.

    python -m benchmarks.bench_sse_frame --output benchmark-results-sse/before.json

Interleaved arms with an A/A control; a delta below twice the measured floor is
unresolved, not zero.
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
from wreath.response import _sse_frame_fields

RESOLUTION_FACTOR = 2.0


def _cases() -> dict[str, tuple[Any, ...]]:
    """(comment, name, ident, retry, data) -- the framer's actual arguments."""
    big = json.dumps({"rows": [{"id": i, "name": f"n{i}"} for i in range(100)]})
    return {
        "keepalive": ("ka", None, None, None, None),
        "data-only": (None, None, None, None, "a short data payload"),
        "typical": (None, "progress", "1234", None, '{"progress":42,"stage":"idx"}'),
        "multiline": (None, None, None, None, "line one\nline two\nline three\nfour"),
        "crlf-heavy": (None, None, None, None, "\r\n".join(f"line {i}" for i in range(40))),
        "large-json": (None, "page", "99", None, big),
        "all-fields": ("c", "progress", "42", 3000, "payload"),
    }


def _time(fn: Any, fields: tuple[Any, ...], iterations: int) -> float:
    start = time.perf_counter()
    for _ in range(iterations):
        fn(*fields)
    return (time.perf_counter() - start) / iterations * 1e6


def _measure(
    fn: Any, fields: tuple[Any, ...], rounds: int, iterations: int, warmup: int
) -> tuple[float, float]:
    _time(fn, fields, warmup)
    main: list[float] = []
    control: list[float] = []
    for _ in range(rounds):
        main.append(_time(fn, fields, iterations))
        control.append(_time(fn, fields, iterations))
    median = statistics.median(main)
    return median, abs(median - statistics.median(control))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=9)
    parser.add_argument("--iterations", type=int, default=20000)
    parser.add_argument("--warmup", type=int, default=2000)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    native = getattr(_core, "sse_frame", None)
    if native is None:
        print("note: _core.sse_frame is absent; measuring the pure encoder only\n")

    scenarios = []
    for label, event in _cases().items():
        size = len(_sse_frame_fields(*event))
        pure_median, pure_floor = _measure(
            _sse_frame_fields, event, args.rounds, args.iterations, args.warmup
        )
        record: dict[str, Any] = {
            "case": label,
            "framed_bytes": size,
            "pure": {
                "median_us": round(pure_median, 4),
                "aa_floor_us": round(pure_floor, 4),
                "resolution_us": round(pure_floor * RESOLUTION_FACTOR, 4),
            },
        }
        line = f"{label:12s} {size:7d}B   pure={pure_median:8.3f}us"
        if native is not None:
            nat_median, nat_floor = _measure(
                native, event, args.rounds, args.iterations, args.warmup
            )
            record["native"] = {
                "median_us": round(nat_median, 4),
                "aa_floor_us": round(nat_floor, 4),
            }
            record["speedup"] = round(pure_median / nat_median, 2)
            line += f"   native={nat_median:8.3f}us   speedup={record['speedup']:5.2f}x"
        else:
            line += f"   (A/A floor {pure_floor:.3f}us)"
        print(line)
        scenarios.append(record)

    document = {
        "python": sys.version,
        "platform": platform.platform(),
        "implementation": sys.implementation.name,
        "rounds": args.rounds,
        "native_available": native is not None,
        "scenarios": scenarios,
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2) + "\n")
        print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
