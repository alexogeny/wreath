"""Count what a request *costs*, in units that survive a change of clock speed.

Every other measurement tool here reports wall time. Wall time answers "how fast
is this box", which is the wrong question twice over: it moves when the governor
moves, and it cannot tell work apart from waiting. This counts instead.

    time = instructions / (IPC x clock)

**Instructions per request is frequency-invariant.** A phase costing 40,000
instructions costs 40,000 at 4.5 GHz and at 400 MHz; only the time to retire
them changes, and it changes exactly with 1/clock. That makes it the *floor*:
the work that scales straight down with the clock, and so the work that
dominates a small ARM board. Cutting it is what makes a framework usable there.

**Stall cycles are not frequency-invariant.** A DRAM miss costs a roughly fixed
number of nanoseconds, so at a lower core clock it costs proportionally fewer
cycles -- memory-bound phases get relatively cheaper as the clock drops, and
their IPC rises. That is the *ceiling*: cutting cache misses buys a fast machine
a lot and a slow one comparatively little.

So `instructions/op` ranks a phase as a floor target, and IPC says how much of
its cost was ceiling rather than floor. High instructions with already-high IPC
is dense interpreter work: the best thing to attack for a small board.

## Reading the numbers

Instruction counts here are reproducible to within a few percent. Cycles, IPC
and cache-misses are **not**, unless the clock is pinned -- on a varying clock
they moved by more than 60% between identical runs while instruction counts held
steady, which is this module's own thesis demonstrating itself. Treat cycles and
IPC as indicative unless you pinned the governor, and trust instructions.

## Why two runs

Counters are taken by *slope*, not by subtraction from zero: the same command at
N operations and at N/2, differenced. Everything that does not scale with N --
interpreter start, imports, route compilation, warmup -- is identical in both
and cancels. Differencing against a zero-operation run instead leaves warmup in
the numerator but not the denominator (about 7% of inflation, measured) and puts
two nearly-equal large numbers in the subtraction, which drove the cheap
counters negative.

## Use

`perf` counts `instructions` for a process the caller owns even at
`kernel.perf_event_paranoid=3`, reported as `:u` userspace events -- so this
needs no privileges. Matching on the bare event name silently reported
"unavailable" on a machine that was counting perfectly well; the suffix is
stripped below.

    uv run wreath-cpu-probe                      # the native request path
    uv run wreath-cpu-probe --json out.json

As a library, for a benchmark that has its own arms:

    from wreath._devtools.cpu_probe import per_operation
    counters = per_operation(lambda n: [sys.executable, "bench.py", "-n", str(n)], 4000)
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: `instructions` is the floor metric; `cycles` gives IPC, which says how much
#: of a phase was stalls rather than work.
COUNTERS: tuple[str, ...] = ("instructions", "cycles", "cache-misses", "branch-misses")


def available() -> bool:
    """Whether `perf` is installed and willing to count hardware events here."""
    if shutil.which("perf") is None:
        return False
    return perf_counters([sys.executable, "-c", "pass"]) is not None


def perf_counters(command: list[str]) -> dict[str, float] | None:
    """Run `command` under `perf stat`, or None if the counters are unavailable."""
    try:
        proc = subprocess.run(
            ["perf", "stat", "-x,", "-e", ",".join(COUNTERS), "--", *command],
            capture_output=True,
            text=True,
        )
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    counts: dict[str, float] = {}
    for line in proc.stderr.splitlines():
        parts = line.split(",")
        if len(parts) < 3:
            continue
        value, _unit, event = parts[0], parts[1], parts[2]
        # perf appends the privilege modifier it managed to use --
        # `instructions:u` when paranoia allows userspace counting only.
        event = event.split(":", 1)[0]
        if event in COUNTERS:
            if not value or not value.replace(".", "").isdigit():
                return None  # <not supported> / <not counted>
            counts[event] = float(value)
    return counts if len(counts) == len(COUNTERS) else None


def per_operation(
    command_for: Callable[[int], list[str]],
    operations: int,
    *,
    scale: int = 1,
) -> dict[str, float] | None:
    """Counters for one operation, from the slope between `operations` and half.

    `command_for(n)` returns the command performing `n` operations. `scale` is
    how many times the command repeats that count internally (a trial count),
    and multiplies the denominator.
    """
    high = perf_counters(command_for(operations))
    low = perf_counters(command_for(operations // 2))
    if high is None or low is None:
        return None
    spread = (operations - operations // 2) * scale
    if spread <= 0:
        return None
    return {name: (high[name] - low[name]) / spread for name in COUNTERS}


def observed_mhz() -> float:
    """What the governor is actually delivering, for the record."""
    values = []
    for path in Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_cur_freq"):
        try:
            values.append(int(path.read_text()) / 1000.0)
        except (OSError, ValueError):
            continue
    return max(values) if values else 0.0


def report(rows: dict[str, dict[str, Any]], *, unit: str = "op") -> None:
    """Print a table of `{label: {"ns": float, "counters": dict | None}}`."""
    counted = all(row.get("counters") for row in rows.values())
    if not counted:
        print("hardware counters unavailable -- wall time only.\n")
        print(f"{'arm':16s} {'ns/' + unit:>10s}")
        print("-" * 27)
        for label, row in rows.items():
            print(f"{label:16s} {row['ns']:9.0f}n")
        return

    header = (
        f"{'arm':16s} {'ns/' + unit:>9s} {'instr/' + unit:>11s} "
        f"{'cycles/' + unit:>11s} {'IPC':>6s} {'cache-miss':>11s}"
    )
    print(header)
    print("-" * len(header))
    for label, row in rows.items():
        counters = row["counters"]
        ipc = counters["instructions"] / counters["cycles"] if counters["cycles"] else 0.0
        print(
            f"{label:16s} {row['ns']:8.0f}n {counters['instructions']:11,.0f} "
            f"{counters['cycles']:11,.0f} {ipc:6.2f} {counters['cache-misses']:11,.1f}"
        )

    print("\nlayer costs (each arm minus the one above it):\n")
    previous: dict[str, Any] | None = None
    for label, row in rows.items():
        if previous is not None:
            delta_ns = row["ns"] - previous["ns"]
            delta = row["counters"]["instructions"] - previous["counters"]["instructions"]
            print(f"  {label:16s} {delta_ns:+8.0f} ns  {delta:+12,.0f} instructions")
        previous = row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-cpu-probe",
        description="Instructions, cycles and IPC per request on the native path.",
    )
    parser.add_argument("--requests", type=int, default=3000)
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=1500)
    parser.add_argument("--arm", action="append", default=None, help="limit to these arms")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    # The arms live with the benchmark that defines them; this tool owns the
    # counting, not the workload.
    benchmark = Path(__file__).resolve().parents[3] / "benchmarks" / "bench_clock_scaling.py"
    if not benchmark.exists():
        print(f"wreath-cpu-probe: no workload at {benchmark}", file=sys.stderr)
        return 2

    from importlib import util as _util

    spec = _util.spec_from_file_location("_wreath_cpu_workload", benchmark)
    if spec is None or spec.loader is None:
        print("wreath-cpu-probe: could not load the workload", file=sys.stderr)
        return 2
    module = _util.module_from_spec(spec)
    spec.loader.exec_module(module)

    names = args.arm or list(module.ARMS)
    unknown = [name for name in names if name not in module.ARMS]
    if unknown:
        print(
            f"wreath-cpu-probe: unknown arm(s) {unknown}; "
            f"available: {sorted(module.ARMS)}",
            file=sys.stderr,
        )
        return 2

    mhz = observed_mhz()
    print(f"python {platform.python_version()} on {platform.platform()}")
    print(f"observed clock {mhz:.0f} MHz (pin it to read cycles/IPC)")
    print(f"requests={args.requests} trials={args.trials}\n")

    rows: dict[str, dict[str, Any]] = {}
    for name in names:
        module._check(name)
        seconds = module._run_arm(name, args.requests, args.trials, args.warmup)
        rows[name] = {
            "ns": seconds / args.requests * 1e9,
            "counters": per_operation(
                lambda n, name=name: [
                    sys.executable, str(benchmark), "--arm", name,
                    "--trials", str(args.trials), "--warmup", str(args.warmup),
                    "--requests", str(n),
                ],
                args.requests,
                scale=args.trials,
            ),
        }
    report(rows, unit="req")

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {"observed_mhz": mhz, "platform": platform.platform(), "arms": rows},
                indent=2,
            )
            + "\n"
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
