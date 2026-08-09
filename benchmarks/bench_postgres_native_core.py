"""Price the exact shared-pool transition with and without Python frames.

This is the saturated steady-state branch used by Statement: a connection is
already shared by two callers, one more caller takes it, then returns that one
share.  No socket or coroutine is involved, so the delta belongs to pool state
transition alone.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wreath._devtools import cpu_probe
from wreath._native import _postgres
from wreath.postgres import Pool, PoolConfig


async def _unused_connector(dsn: str) -> object:
    raise RuntimeError(f"benchmark connector was unexpectedly called for {dsn}")


_CONNECTION = object()
_POOL = Pool(
    "postgresql://benchmark",
    PoolConfig(min_size=0, max_size=1, pipeline_depth=8),
    connector=_unused_connector,
    read_only=True,
    statements=lambda: (),
)
_POOL._started = True
_POOL._shared[id(_CONNECTION)] = (_CONNECTION, 2)
_SINK: Any = None


def _python_cycle(iterations: int) -> None:
    global _SINK
    for _ in range(iterations):
        connection = _POOL.try_acquire_shared()
        if connection is None or not _POOL.try_release_shared(connection):
            raise RuntimeError("Python pool cycle left the steady-state path")
        _SINK = connection


def _native_cycle(iterations: int) -> None:
    global _SINK
    for _ in range(iterations):
        connection = _postgres._pool_try_acquire(_POOL)
        if connection is None or not _postgres._pool_try_release(_POOL, connection):
            raise RuntimeError("native pool cycle left the steady-state path")
        _SINK = connection


ARMS: dict[str, Callable[[int], None]] = {
    "python": _python_cycle,
    "native": _native_cycle,
    "native-aa": _native_cycle,
}


def _timed(
    payload: Callable[[int], None], operations: int, trials: int, warmup: int
) -> tuple[float, list[float]]:
    payload(warmup)
    samples = []
    for _ in range(trials):
        started = time.perf_counter()
        payload(operations)
        samples.append((time.perf_counter() - started) / operations * 1e6)
    return statistics.median(samples), samples


def _counted(name: str, operations: int, trials: int, warmup: int) -> dict[str, float]:
    counters = cpu_probe.per_operation(
        lambda count: [
            sys.executable,
            __file__,
            "--arm",
            name,
            "--operations",
            str(count),
            "--trials",
            str(trials),
            "--warmup",
            str(warmup),
        ],
        operations,
        scale=trials,
    )
    if counters is None:
        raise RuntimeError("hardware counters are unavailable")
    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=tuple(ARMS))
    parser.add_argument("--operations", type=int, default=100_000)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=5_000)
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.operations < 2 or args.trials < 1 or args.warmup < 0:
        parser.error("operations >= 2, trials >= 1 and warmup >= 0 required")
    selected = {args.arm: ARMS[args.arm]} if args.arm is not None else ARMS
    results: dict[str, Any] = {}
    for name, payload in selected.items():
        median, samples = _timed(payload, args.operations, args.trials, args.warmup)
        results[name] = {
            "median_us": median,
            "samples_us": samples,
            "counters": (
                _counted(name, args.operations, args.trials, args.warmup)
                if args.measure
                else None
            ),
        }
    for name, row in results.items():
        counters = row["counters"]
        if counters is None:
            print(f"{name:<10} {row['median_us']:>8.3f} us/op")
        else:
            print(
                f"{name:<10} {row['median_us']:>8.3f} us/op "
                f"{counters['instructions']:>10,.0f} instr/op"
            )
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(results, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
