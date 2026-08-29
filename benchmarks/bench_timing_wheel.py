"""wreath-metal timer: the native hashed timing wheel vs the event loop's own timer.

The framework arms two deadlines per request -- a keep-alive and a request
timeout -- and almost always *cancels* them before they fire (the request
finished in time). This measures that exact churn (schedule two, cancel two)
against the timer store each event loop ships (asyncio's heap, uvloop's libuv
heap), plus the memory each holds per live timer.

The exploratory five-way shootout is benchmarks/bench_timer_shootout.py
(reference only, not in the default battery).
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tracemalloc
from pathlib import Path
from time import perf_counter_ns

from wreath._native import _reactor as _native

try:
    import uvloop
except ImportError:  # pragma: no cover - optional
    uvloop = None

WHEEL_SLOTS = 4096


def _noop() -> None:
    return None


class _Wheel:
    label = "wreath_wheel"

    def __init__(self):
        self.w = _native.TimingWheel(resolution=0.001, slots=WHEEL_SLOTS, base=0.0)

    def schedule(self, delay):
        return self.w.schedule(delay, _noop)

    @staticmethod
    def cancel(h):
        h.cancel()

    def close(self):
        pass


class _Loop:
    def __init__(self, loop):
        self.loop = loop

    def schedule(self, delay):
        return self.loop.call_later(delay, _noop)

    @staticmethod
    def cancel(h):
        h.cancel()

    def close(self):
        self.loop.close()


def bench_request_churn(make, iters, trials, *, batch=512, pinned=64):
    """ns per request (arm keep-alive + request timeout, cancel both).

    The store is recycled every `batch` requests so a real event loop's
    un-drained heap cannot grow without bound while it is never run -- this
    isolates the genuine per-call overhead (object allocation + a small heap),
    the same thing the framework pays on the hot path.
    """
    samples = []
    for _ in range(trials):
        total = 0.0
        done = 0
        while done < iters:
            s = make()
            keep = [s.schedule(3600.0) for _ in range(pinned)]
            n = min(batch, iters - done)
            started = perf_counter_ns()
            for _ in range(n):
                ka = s.schedule(5.0)
                rq = s.schedule(30.0)
                s.cancel(rq)
                s.cancel(ka)
            total += perf_counter_ns() - started
            done += n
            s.close()
            del keep
        samples.append(total / iters)
    return statistics.median(samples)


def bench_memory(make, k=10_000):
    tracemalloc.start()
    tracemalloc.reset_peak()
    s = make()
    keep = [s.schedule(30.0) for _ in range(k)]
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del keep, s
    return peak / k


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=200_000)
    parser.add_argument("--trials", type=int, default=9)
    parser.add_argument("--label", default="unlabelled")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    import asyncio

    arms = {"wreath_wheel": _Wheel}
    arms["asyncio"] = lambda: _Loop(asyncio.new_event_loop())
    if uvloop is not None:
        arms["uvloop"] = lambda: _Loop(uvloop.new_event_loop())

    churn = {
        name: bench_request_churn(make, args.iterations, args.trials) for name, make in arms.items()
    }
    memory = {name: bench_memory(make) for name, make in arms.items()}

    document = {
        "tool": "benchmarks.bench_timing_wheel",
        "schema_version": 1,
        "label": args.label,
        "python": sys.version,
        "platform": platform.platform(),
        "uvloop": getattr(uvloop, "__version__", None),
        "wheel_slots": WHEEL_SLOTS,
        "iterations": args.iterations,
        "trials": args.trials,
        "ns_per_request": churn,
        "memory_bytes_per_timer": memory,
    }
    text = json.dumps(document, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
        print(f"wrote {args.output}")
        return

    base = churn.get("asyncio")
    print(f"\nwreath-metal timer ({WHEEL_SLOTS}-slot hashed wheel) vs event-loop timers")
    print("ns per request (arm keep-alive + request, cancel both; median):")
    for name, v in churn.items():
        speedup = f"  ({base / v:.1f}x vs asyncio)" if base and name != "asyncio" else ""
        print(f"  {name.ljust(14)} {v:7.0f}{speedup}")
    print("\nmemory (bytes per live timer):")
    for name, v in memory.items():
        print(f"  {name.ljust(14)} {v:7.0f}")


if __name__ == "__main__":
    main()
