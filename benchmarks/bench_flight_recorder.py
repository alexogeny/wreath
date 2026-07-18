"""Native Flight Recorder acceptance microbenchmark (NFR Stage 1 §12).

Isolates the recorder's per-request cost from the server so the two Stage-1
acceptance questions can be answered with numbers, not argument:

- **Off is free.** The disabled recorder must be statistically indistinguishable
  from a telemetry-free build. There is no separate telemetry-free binary to
  compare against, so the proxy here is an A/A: two independent Off arms. Their
  spread is the noise floor; a real Off cost would exceed it.
- **Pulse is cheap.** One completion cell per request should stay well under the
  plan's 1% budget. The arms below ablate the writer so the cost of each added
  cell (completion, correlation) and of route attribution is attributable
  independently, per the plan's "ablate at whole-request level" rule.

This drives the recorder through the exact hook sequence the native protocols
use -- ``begin -> route -> [propagate] -> finish`` -- not the fused ``record``
shortcut, so the numbers reflect the request path. It is a focused recorder
microbenchmark, not an end-to-end server result; nothing here binds a socket.

The ring is drained between timed batches (never inside one) so the publish path
is measured, not the drop path. Each batch is one ring's worth of requests.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
from collections.abc import Callable
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from wreath._native import _flight

# A valid W3C traceparent, parsed fresh each request in the propagation arm.
_TRACEPARENT = b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

RING = 16_384
ACTIVE = 2_048


def _cycle_context(recorder: Any) -> None:
    """begin -> route -> finish: the Pulse hook sequence without propagation."""
    req = recorder.begin(1, 1, 0)
    req.route(7, 3)
    req.finish(1_000, 200, 0, 0, 0, 12)


def _cycle_propagated(recorder: Any) -> None:
    """begin -> route -> propagate -> finish: adds a W3C parse + correlation cell."""
    req = recorder.begin(1, 1, 0)
    req.route(7, 3)
    req.propagate(_TRACEPARENT)
    req.finish(1_000, 200, 0, 0, 0, 12)


def sample(
    recorder: Any, cycle: Callable[[Any], None], batch: int, trials: int
) -> list[float]:
    """Median-friendly per-request nanoseconds; drain between batches, untimed."""
    samples: list[float] = []
    for _ in range(trials):
        recorder.drain(batch)  # keep the ring empty so every publish succeeds
        started = perf_counter_ns()
        for _ in range(batch):
            cycle(recorder)
        samples.append((perf_counter_ns() - started) / batch)
    return samples


def loop_floor(batch: int, trials: int) -> float:
    """The call + loop overhead every arm's timed region shares."""
    def noop(_recorder: Any) -> None:
        return None

    samples: list[float] = []
    for _ in range(trials):
        started = perf_counter_ns()
        for _ in range(batch):
            noop(None)
        samples.append((perf_counter_ns() - started) / batch)
    return statistics.median(samples)


def _arm(mode: int, *, summaries: bool = True) -> Any:
    return _flight.Recorder(
        mode, ring_records=RING, active_requests=ACTIVE,
        completion_summaries=summaries,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=RING,
                        help="requests per timed batch (<= ring capacity)")
    parser.add_argument("--trials", type=int, default=15)
    parser.add_argument("--label", default="unlabelled",
                        help="which build this run measured, e.g. 'baseline'")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    if _flight is None:
        raise SystemExit("native _flight extension is required")
    batch = min(args.batch, RING)

    # (name, recorder, cycle). Two Off arms establish the A/A noise floor.
    arms: list[tuple[str, Any, Callable[[Any], None]]] = [
        ("off_a", _arm(_flight.MODE_OFF), _cycle_context),
        ("off_b", _arm(_flight.MODE_OFF), _cycle_context),
        ("pulse_counters_only", _arm(_flight.MODE_PULSE, summaries=False), _cycle_context),
        ("pulse_completion", _arm(_flight.MODE_PULSE), _cycle_context),
        ("pulse_completion_plus_correlation", _arm(_flight.MODE_PULSE), _cycle_propagated),
    ]

    floor = loop_floor(batch, args.trials)
    rows = []
    medians: dict[str, float] = {}
    for name, recorder, cycle in arms:
        # Warm the code paths (and the active-slot free list) before timing.
        for _ in range(batch):
            cycle(recorder)
        recorder.drain(batch)
        raw = sample(recorder, cycle, batch, args.trials)
        median = statistics.median(raw)
        medians[name] = median
        rows.append({
            "arm": name,
            "mode": recorder.mode,
            "ns_per_request": {
                "raw": raw,
                "median": median,
                "min": min(raw),
                "net_median": median - floor,  # loop overhead removed
            },
        })

    off = min(medians["off_a"], medians["off_b"])
    aa_noise_pct = abs(medians["off_a"] - medians["off_b"]) / off * 100.0
    overheads = {
        name: (medians[name] - off) / off * 100.0
        for name in ("pulse_counters_only", "pulse_completion",
                     "pulse_completion_plus_correlation")
    }

    document = {
        "tool": "benchmarks.bench_flight_recorder",
        "schema_version": 1,
        "label": args.label,
        "python": sys.version,
        "platform": platform.platform(),
        "flight_module": _flight.__file__,
        "batch": batch,
        "trials": args.trials,
        "empty_loop_ns": floor,
        "aa_noise_pct": aa_noise_pct,
        "pulse_overhead_pct_vs_off": overheads,
        "rows": rows,
    }
    text = json.dumps(document, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
        print(f"wrote {args.output}")
    else:
        print(text)

    # A compact human summary after the JSON.
    print(f"\nA/A noise: {aa_noise_pct:.2f}%   (Off floor {off:.1f} ns/req)", file=sys.stderr)
    for name, pct in overheads.items():
        print(f"  {name:38s} +{pct:6.2f}%  ({medians[name]:.1f} ns/req)", file=sys.stderr)


if __name__ == "__main__":
    main()
