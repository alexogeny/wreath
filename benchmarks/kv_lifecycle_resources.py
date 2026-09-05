"""Fixed-work KV counts, read controls, and clear/refill resource comparisons.

Use frozen --source trees with isolated extensions. CPU runs without tracing;
a separate replay measures retained/peak allocator bytes around the same phase.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sys
import tracemalloc
from pathlib import Path
from time import process_time_ns

from route_image import resident_bytes


def fill(table, keys, scenario):
    for key in keys:
        if scenario == "expired":
            table.set(key, key, ttl=1, now=0)
        elif scenario == "mixed":
            table.set(key, key, ttl=1 if key % 2 == 0 else 10, now=0)
        else:
            table.set(key, key, now=0)


def prepare(table_type, keys, scenario):
    table = table_type(max_entries=len(keys), clock=lambda: 0)
    if scenario != "fill":
        fill(table, keys, scenario)
    if scenario in {"empty-clear", "refill"}:
        table.clear()
    return table


def exercise(table, keys, scenario, repeats):
    checksum = 0
    if scenario in {"fill", "refill"}:
        fill(table, keys, scenario)
        return len(keys)
    for _ in range(repeats):
        if scenario == "length":
            checksum += len(table)
        elif scenario in {"count", "expired", "mixed"}:
            checksum += table.count(now=2 if scenario in {"expired", "mixed"} else 0)
        elif scenario == "peek":
            checksum += table.peek(0, None, 0)
        elif scenario in {"clear", "empty-clear"}:
            checksum += table.clear()
        else:
            checksum += table.clear()
            fill(table, keys, scenario)
    return checksum


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--size", type=int, default=8192)
    parser.add_argument("--repeats", type=int, default=2000)
    parser.add_argument(
        "--scenario",
        required=True,
        choices=(
            "count",
            "length",
            "peek",
            "expired",
            "mixed",
            "clear",
            "empty-clear",
            "fill",
            "refill",
            "cycle",
        ),
    )
    args = parser.parse_args()
    if args.size <= 0 or args.size % 2 or args.repeats <= 0:
        parser.error("--size must be positive and even; --repeats must be positive")
    sys.path.insert(0, str(args.source.resolve()))
    from wreath._native import _core

    loaded = Path(_core.__file__).resolve()
    if not loaded.is_relative_to(args.source.resolve()):
        raise RuntimeError(f"loaded {loaded}, expected extension under {args.source}")
    keys = tuple(range(args.size))
    table = prepare(_core.KV, keys, args.scenario)
    gc.collect()
    started = process_time_ns()
    checksum = exercise(table, keys, args.scenario, args.repeats)
    elapsed = process_time_ns() - started
    memory = resident_bytes()
    expected = 0
    if args.scenario in {"count", "length", "cycle"}:
        expected = args.size * args.repeats
    elif args.scenario == "mixed":
        expected = args.size // 2 * args.repeats
    elif args.scenario in {"clear", "fill", "refill"}:
        expected = args.size
    if checksum != expected:
        raise RuntimeError(f"expected checksum {expected}, got {checksum}")
    live = table.count(now=2)
    if live != (
        0
        if args.scenario in {"clear", "empty-clear", "expired"}
        else args.size // 2
        if args.scenario == "mixed"
        else args.size
    ):
        raise RuntimeError("final live-entry count changed")
    gc.collect()
    tracemalloc.start()
    try:
        measured = prepare(_core.KV, keys, args.scenario)
        before, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        measured_checksum = exercise(measured, keys, args.scenario, args.repeats)
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if measured_checksum != checksum:
        raise RuntimeError("traced and untraced phases disagree")
    metrics = {
        "cpu_ns": elapsed,
        "retained_bytes": retained,
        "peak_bytes": peak,
        "phase_start_bytes": before,
        "slots": table.slots,
        **memory,
        "source": str(loaded),
        "extension_sha256": hashlib.sha256(loaded.read_bytes()).hexdigest(),
    }
    args.metrics.write_text(json.dumps(metrics) + "\n")
    print(json.dumps({"checksum": checksum, "live": live, "configured": args.size}, sort_keys=True))


if __name__ == "__main__":
    main()
