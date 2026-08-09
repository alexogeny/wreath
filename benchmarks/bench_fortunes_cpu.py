"""Instruction decomposition of the CPU-only half of a Fortunes request.

The end-to-end verdict lives in ``.futures/tfb-local``.  This smaller probe uses
the exact 12 seeded rows and the entry's exact Record/template/response shapes
to price the work after PostgreSQL completion.  Arms are cumulative, so each
adjacent delta is one stage; ``complete (A/A)`` measures counter and timing
noise with the same payload at the opposite end of the round.

Run:

    uv run python benchmarks/bench_fortunes_cpu.py --measure
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from collections.abc import Callable
from operator import itemgetter
from pathlib import Path
from typing import Any

from wreath._devtools import cpu_probe
from wreath.postgres import Record
from wreath.response import HTMLResponse
from wreath.templates import Template

_COLUMNS = ("id", "message")
_MESSAGES = (
    "fortune: No such file or directory",
    "A computer scientist is someone who fixes things that aren't broken.",
    "After enough decimal places, nobody gives a damn.",
    "A bad random number generator: 1, 1, 1, 1, 1, 4.33e+67, 1, 1, 1",
    "A computer program does what you tell it to do, not what you want it to do.",
    "Emacs is a nice operating system, but I prefer UNIX. — Tom Christaensen",
    "Any program that runs right is obsolete.",
    "A list is only as strong as its weakest link. — Donald Knuth",
    "Feature: A bug with seniority.",
    "Computers make very fast, very accurate mistakes.",
    '<script>alert("This should not be displayed in a browser alert box.");</script>',
    "フレームワークのベンチマーク",
)
_ROWS = tuple(
    Record(_COLUMNS, (identifier, message))
    for identifier, message in enumerate(_MESSAGES, 1)
)
_EPHEMERAL = Record(
    _COLUMNS, (0, "Additional fortune added at request time.")
)
_BY_MESSAGE = itemgetter("message")
_BY_MESSAGE_INDEX = itemgetter(1)
_ORDERED_ROWS = sorted((*_ROWS, _EPHEMERAL), key=_BY_MESSAGE)
_RENDER_CONTEXT = {"rows": _ORDERED_ROWS}
_TEMPLATE = Template.from_string(
    "<!DOCTYPE html>"
    "<html><head><title>Fortunes</title></head><body>"
    "<table><tr><th>id</th><th>message</th></tr>"
    "{% for row in rows %}<tr><td>{{ row.id }}</td><td>{{ row.message }}</td></tr>"
    "{% endfor %}</table></body></html>"
)
_RENDERED = _TEMPLATE.render_bytes(_RENDER_CONTEXT)
_SINK: Any = None


def _nothing(iterations: int) -> None:
    global _SINK
    for _ in range(iterations):
        _SINK = _ROWS


def _materialize(iterations: int) -> None:
    global _SINK
    for _ in range(iterations):
        _SINK = list(_ROWS)


def _append(iterations: int) -> None:
    global _SINK
    for _ in range(iterations):
        rows = list(_ROWS)
        rows.append(_EPHEMERAL)
        _SINK = rows


def _sort(iterations: int) -> None:
    global _SINK
    for _ in range(iterations):
        rows = list(_ROWS)
        rows.append(_EPHEMERAL)
        rows.sort(key=_BY_MESSAGE)
        _SINK = rows


def _sort_index(iterations: int) -> None:
    global _SINK
    for _ in range(iterations):
        rows = list(_ROWS)
        rows.append(_EPHEMERAL)
        rows.sort(key=_BY_MESSAGE_INDEX)
        _SINK = rows


def _render(iterations: int) -> None:
    global _SINK
    for _ in range(iterations):
        rows = list(_ROWS)
        rows.append(_EPHEMERAL)
        rows.sort(key=_BY_MESSAGE)
        _SINK = _TEMPLATE.render_bytes({"rows": rows})


def _render_only(iterations: int) -> None:
    global _SINK
    for _ in range(iterations):
        _SINK = _TEMPLATE.render_bytes(_RENDER_CONTEXT)


def _complete(iterations: int) -> None:
    global _SINK
    for _ in range(iterations):
        rows = list(_ROWS)
        rows.append(_EPHEMERAL)
        rows.sort(key=_BY_MESSAGE)
        _SINK = HTMLResponse(_TEMPLATE.render_bytes({"rows": rows}))


def _response_only(iterations: int) -> None:
    global _SINK
    for _ in range(iterations):
        _SINK = HTMLResponse(_RENDERED)


ARMS: dict[str, Callable[[int], None]] = {
    "nothing": _nothing,
    "materialize": _materialize,
    "append": _append,
    "sort": _sort,
    "sort-index": _sort_index,
    "render": _render,
    "render-only": _render_only,
    "render-only-aa": _render_only,
    "complete": _complete,
    "response-only": _response_only,
    "response-only-aa": _response_only,
    "complete-aa": _complete,
}

STAGES = ("nothing", "materialize", "append", "sort", "render", "complete")


def _check() -> None:
    _complete(1)
    if not isinstance(_SINK, HTMLResponse):
        raise RuntimeError("complete arm did not construct HTMLResponse")
    body = _SINK.body
    if b"&lt;script&gt;" not in body or "フレームワーク".encode() not in body:
        raise RuntimeError("Fortunes renderer changed escaping or UTF-8")
    if body.count(b"<tr>") != 14:
        raise RuntimeError("Fortunes renderer did not emit 13 data rows")


def _timed(payload: Callable[[int], None], operations: int, trials: int,
           warmup: int) -> tuple[float, list[float]]:
    payload(warmup)
    samples: list[float] = []
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
            "--arm", name,
            "--operations", str(count),
            "--trials", str(trials),
            "--warmup", str(warmup),
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
    parser.add_argument("--operations", type=int, default=50000)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=2000)
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.operations < 2 or args.trials < 1 or args.warmup < 0:
        parser.error("operations >= 2, trials >= 1 and warmup >= 0 required")
    _check()
    if args.arm and not args.measure:
        payload = ARMS[args.arm]
        for _ in range(args.trials):
            payload(args.warmup)
            payload(args.operations)
        return 0

    results: dict[str, Any] = {}
    print(f"python {platform.python_version()} on {platform.platform()}")
    print(f"operations={args.operations} trials={args.trials}\n")
    selected = {args.arm: ARMS[args.arm]} if args.arm is not None else ARMS
    for name, payload in selected.items():
        median, samples = _timed(payload, args.operations, args.trials, args.warmup)
        results[name] = {
            "median_us": median,
            "samples_us": samples,
            "counters": _counted(name, args.operations, args.trials, args.warmup)
            if args.measure else None,
        }
    header = f"{'arm':<14} {'us/op':>9} {'instr/op':>12} {'cycles/op':>12} {'IPC':>6}"
    print(header)
    print("-" * len(header))
    for name, row in results.items():
        counters = row["counters"]
        if counters is None:
            print(f"{name:<14} {row['median_us']:>9.3f}")
            continue
        cycles = counters["cycles"]
        ipc = counters["instructions"] / cycles if cycles else 0.0
        print(
            f"{name:<14} {row['median_us']:>9.3f} "
            f"{counters['instructions']:>12,.0f} {cycles:>12,.0f} {ipc:>6.2f}"
        )
    if args.measure and args.arm is None:
        print("\nadjacent stage cost:")
        previous: dict[str, Any] | None = None
        for name in STAGES:
            row = results[name]
            if previous is not None:
                time_delta = row["median_us"] - previous["median_us"]
                instruction_delta = (
                    row["counters"]["instructions"]
                    - previous["counters"]["instructions"]
                )
                print(f"  {name:<14} {time_delta:+8.3f}us {instruction_delta:+10,.0f} instr")
            previous = row
        print("\nablations and A/A controls:")
        for name, control in (
            ("sort-index", "sort"),
            ("render-only-aa", "render-only"),
            ("response-only-aa", "response-only"),
            ("complete-aa", "complete"),
        ):
            row = results[name]
            reference = results[control]
            time_delta = row["median_us"] - reference["median_us"]
            instruction_delta = (
                row["counters"]["instructions"]
                - reference["counters"]["instructions"]
            )
            print(
                f"  {name:<16} vs {control:<14} "
                f"{time_delta:+8.3f}us {instruction_delta:+10,.0f} instr"
            )
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(results, indent=2) + "\n")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
