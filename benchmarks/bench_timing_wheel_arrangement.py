"""Insert, cancel and fire on the timing wheel, by *arrangement* rather than size.

The wheel's cost used to depend on how deadlines were arranged, not on how many
there were. Slots are a hash of the deadline, so deadlines congruent modulo
`slots` share one; with the unordered chain that preceded the per-slot pairing
heap, both "which nodes are due" and "what is this slot's new minimum" needed a
full walk, and each was quadratic in the chain length.

That is why this benchmark measures the three operations *separately* in *two*
arrangements. Timing schedule-and-cancel as one region hides which of the two
moved, and a fix that flattens cancel by making insert quadratic is a relocation
rather than a fix -- so all three are reported, and the spread arrangement is the
control that says the difference is arrangement and not count.

The measurement to read is the **ratio**, not the nanoseconds. Doubling the size
should double the time; a ratio near 4 is quadratic. Ratios survive a shared
machine, absolute timings do not.

    uv run python benchmarks/bench_timing_wheel_arrangement.py
    uv run python benchmarks/bench_timing_wheel_arrangement.py --control

`--control` runs the same arm twice and reports the difference, so a delta
smaller than that number means nothing.
"""

from __future__ import annotations

import argparse
import importlib
import time
from typing import Any

#: The shipped defaults, so the arms measure what actually runs.
SLOTS = 512
RESOLUTION = 0.001

SIZES = (500, 1000, 2000, 4000)
ROUNDS = 5


def _noop() -> None:
    pass


def _wheel() -> Any:
    reactor: Any = importlib.import_module("wreath._native._reactor")
    return reactor.TimingWheel(resolution=RESOLUTION, slots=SLOTS, base=0.0)


def _delays(count: int, *, colliding: bool) -> list[float]:
    """Deadlines that share one slot, or spread one per slot.

    Colliding steps by `SLOTS` so every deadline lands in the same bucket at a
    *distinct* deadline -- distinct is the point, because a same-deadline cohort
    is the case the old chain already handled in O(1).
    """
    step = SLOTS if colliding else 1
    return [step * index * RESOLUTION for index in range(1, count + 1)]


def insert(count: int, *, colliding: bool) -> tuple[float, dict[str, Any]]:
    wheel = _wheel()
    delays = _delays(count, colliding=colliding)
    start = time.perf_counter()
    handles = [wheel.schedule(delay, _noop) for delay in delays]
    elapsed = time.perf_counter() - start
    assert wheel.count == count
    return elapsed, {"rescans": wheel.slot_rescans, "held": len(handles)}


def cancel(count: int, *, colliding: bool) -> tuple[float, dict[str, Any]]:
    """Cancel in ascending deadline order, which always removes the slot minimum.

    That is both the pathological order and the natural one: a server cancels a
    request deadline when the response is written, and responses tend to finish
    in the order their deadlines were set.
    """
    wheel = _wheel()
    handles = [wheel.schedule(delay, _noop) for delay in _delays(count, colliding=colliding)]
    before = wheel.slot_rescans
    start = time.perf_counter()
    for handle in handles:
        handle.cancel()
    elapsed = time.perf_counter() - start
    assert wheel.count == 0, f"{wheel.count} timers survived cancellation"
    return elapsed, {"rescans": wheel.slot_rescans - before}


def fire(count: int, *, colliding: bool) -> tuple[float, dict[str, Any]]:
    wheel = _wheel()
    delays = _delays(count, colliding=colliding)
    for delay in delays:
        wheel.schedule(delay, _noop)
    before = wheel.slot_rescans
    start = time.perf_counter()
    due = wheel.advance(delays[-1] + RESOLUTION)
    elapsed = time.perf_counter() - start
    assert len(due) == count, f"fired {len(due)}, expected {count}"
    return elapsed, {"rescans": wheel.slot_rescans - before}


OPERATIONS = {"insert": insert, "cancel": cancel, "fire": fire}


def report() -> None:
    header = (
        f"{'op':<8}{'arrangement':<12}{'k':>6}{'ns/op':>11}{'spread%':>9}{'ratio':>8}{'rescans':>9}"
    )
    print(header)
    print("-" * len(header))
    for name, operation in OPERATIONS.items():
        for colliding in (False, True):
            previous: float | None = None
            for size in SIZES:
                best = float("inf")
                worst = 0.0
                detail: dict[str, Any] = {}
                for _ in range(ROUNDS):
                    elapsed, detail = operation(size, colliding=colliding)
                    best = min(best, elapsed)
                    worst = max(worst, elapsed)
                ratio = "" if previous is None else f"{best / previous:.2f}"
                print(
                    f"{name:<8}{'colliding' if colliding else 'spread':<12}"
                    f"{size:>6}{best / size * 1e9:>11.1f}"
                    f"{(worst - best) / best * 100:>8.1f}%{ratio:>8}"
                    f"{detail.get('rescans', 0):>9}"
                )
                previous = best
            print()


def control() -> None:
    """A/A: the same arm twice. A delta below this is noise, not a result."""
    for name, operation in OPERATIONS.items():
        runs = [
            min(operation(SIZES[-1], colliding=False)[0] for _ in range(ROUNDS)) for _ in range(2)
        ]
        drift = abs(runs[0] - runs[1]) / min(runs) * 100.0
        print(f"A/A {name:<8} {drift:.2f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--control", action="store_true", help="report the A/A noise floor instead of the table"
    )
    if parser.parse_args().control:
        control()
    else:
        report()


if __name__ == "__main__":
    main()
