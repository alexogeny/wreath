"""Measure native-kernel latency and cross-thread GIL exclusion.

This is deliberately not an event-loop fairness benchmark: releasing the GIL in
a synchronous call does not let the same event loop run.  The observer is an
independent Python thread and reports progress only while one native call is in
flight.  Preserve every trial and establish A/A noise before attributing deltas.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wreath._native import _core

Kernel = Callable[[], object]


def _one_trial(kernel: Kernel) -> dict[str, int]:
    ready = threading.Event()
    done = threading.Event()
    elapsed_ns = 0

    def worker() -> None:
        nonlocal elapsed_ns
        ready.set()
        started = time.perf_counter_ns()
        kernel()
        elapsed_ns = time.perf_counter_ns() - started
        done.set()

    thread = threading.Thread(target=worker)
    thread.start()
    ready.wait()
    observer_iterations = 0
    while not done.is_set():
        observer_iterations += 1
    thread.join()
    return {"elapsed_ns": elapsed_ns, "observer_iterations": observer_iterations}


def _measure(kernel: Kernel, warmup: int, trials: int) -> list[dict[str, int]]:
    for _ in range(warmup):
        _one_trial(kernel)
    return [_one_trial(kernel) for _ in range(trials)]


def _measure_uncontended(kernel: Kernel, warmup: int, trials: int) -> list[int]:
    for _ in range(warmup):
        kernel()
    measured: list[int] = []
    for _ in range(trials):
        started = time.perf_counter_ns()
        kernel()
        measured.append(time.perf_counter_ns() - started)
    return measured


def _summary(trials: list[dict[str, int]]) -> dict[str, float | int]:
    elapsed = [trial["elapsed_ns"] for trial in trials]
    observer = [trial["observer_iterations"] for trial in trials]
    return {
        "elapsed_median_ns": statistics.median(elapsed),
        "elapsed_min_ns": min(elapsed),
        "elapsed_max_ns": max(elapsed),
        "observer_median_iterations": statistics.median(observer),
        "observer_min_iterations": min(observer),
        "observer_max_iterations": max(observer),
    }


def run_ws(sizes: list[int], warmup: int, trials: int) -> dict[str, Any]:
    if _core is None or not hasattr(_core, "ws_mask"):
        raise RuntimeError("native _core.ws_mask is unavailable")
    key = b"\x13\x37\x42\x99"
    results: list[dict[str, Any]] = []
    for size in sizes:
        payload = bytes((index * 17 + 3) & 0xFF for index in range(size))
        masked = _core.ws_mask(payload, key)
        if _core.ws_mask(masked, key) != payload:
            raise RuntimeError(f"ws_mask integrity check failed for {size} bytes")
        def kernel(data: bytes = payload) -> object:
            return _core.ws_mask(data, key)

        uncontended = _measure_uncontended(kernel, warmup, trials)
        measured = _measure(kernel, warmup, trials)
        results.append({
            "size": size,
            "uncontended": {
                "median_ns": statistics.median(uncontended),
                "min_ns": min(uncontended),
                "max_ns": max(uncontended),
                "trials_ns": uncontended,
            },
            "contended": {"summary": _summary(measured), "trials": measured},
        })
    return {
        "schema": 1,
        "kernel": "ws_mask",
        "python": sys.version,
        "platform": platform.platform(),
        "implementation": getattr(_core, "__file__", None),
        "warmup": warmup,
        "trial_count": trials,
        "results": results,
    }


def _sizes(value: str) -> list[int]:
    parsed = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not parsed or any(item <= 0 for item in parsed):
        raise argparse.ArgumentTypeError("sizes must be positive comma-separated byte counts")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kernel", choices=("ws-mask",), default="ws-mask")
    parser.add_argument("--sizes", type=_sizes, default=_sizes("1024,65536,1048576,16777216"))
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--trials", type=int, default=9)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = run_ws(args.sizes, args.warmup, args.trials)
    rendered = json.dumps(result, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
