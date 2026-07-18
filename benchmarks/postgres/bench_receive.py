"""Measure extension-owned PostgreSQL receive slabs against the Slice 2 baseline.

The benchmark records native slab growth after warmup, peak traced memory, and
latency. Pass a previously recorded Slice 2 receive-allocation count when
reproducing a before/after comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import importlib
import json
import platform
import statistics
import sys
import time
import tracemalloc
from typing import Any

from wreath._pure import postgres as pure_postgres


def _native_backend() -> Any:
    try:
        return importlib.import_module("wreath._native._postgres")
    except ImportError as error:
        raise RuntimeError("build the native PostgreSQL extension first") from error


async def _measure_backend(
    backend: Any, dsn: str, iterations: int, warmup: int
) -> dict[str, object]:
    connection = await backend.connect(dsn)
    sql = "select $1::int4"
    try:
        for value in range(warmup):
            assert await connection.fetchval(sql, value) == value
        receive_stats = getattr(connection._reader, "_receive_stats", None)
        before_stats = receive_stats() if receive_stats is not None else None
        gc.collect()
        tracemalloc.start()
        tracemalloc.reset_peak()
        baseline, _ = tracemalloc.get_traced_memory()
        samples: list[float] = []
        for value in range(iterations):
            started = time.perf_counter_ns()
            assert await connection.fetchval(sql, value) == value
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        after_stats = receive_stats() if receive_stats is not None else None
    finally:
        await connection.close()

    slab_growth = None
    if before_stats is not None and after_stats is not None:
        slab_growth = after_stats["slab_allocations"] - before_stats["slab_allocations"]
    return {
        "median_latency_ms": statistics.median(samples),
        "p95_latency_ms": sorted(samples)[min(len(samples) - 1, int(len(samples) * 0.95))],
        "peak_traced_bytes_over_baseline": peak - baseline,
        "receive_slab_allocations": slab_growth,
        "receive_slab_allocations_per_query": (
            slab_growth / iterations if slab_growth is not None else None
        ),
        "receive_stats_before": before_stats,
        "receive_stats_after": after_stats,
    }


async def run(args: argparse.Namespace) -> int:
    native = _native_backend()
    pure = await _measure_backend(pure_postgres, args.dsn, args.iterations, args.warmup)
    native_result = await _measure_backend(native, args.dsn, args.iterations, args.warmup)
    native_allocations = float(native_result["receive_slab_allocations_per_query"] or 0)
    improved = native_allocations < args.slice2_allocations_per_query
    document = {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "iterations": args.iterations,
            "warmup": args.warmup,
            "slice2_receive_allocations_per_query": args.slice2_allocations_per_query,
        },
        "pure_stream_reader": pure,
        "native_buffered_protocol": native_result,
        "native_receive_allocations_lower_than_slice2": improved,
    }
    print(json.dumps(document, indent=2))
    return 1 if args.require_improvement and not improved else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument(
        "--slice2-allocations-per-query",
        type=float,
        default=4.0,
        help="retained Slice 2 receive-allocation baseline",
    )
    parser.add_argument("--require-improvement", action="store_true")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
