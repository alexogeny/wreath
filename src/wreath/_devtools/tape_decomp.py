"""Decompose what the global middleware tape costs a request.

`wreath-request-trace` counts boundary crossings. It cannot say what they cost, and
a count is not a reason to move code into C. This measures that:

    uv run wreath-tape-decomp                     # the realistic sample stack
    uv run wreath-tape-decomp --mode cumulative   # marginal cost, in context
    uv run wreath-tape-decomp --app myapp:app --path /users/1
    uv run wreath-tape-decomp --json benchmark-results-tape/run.json

Two questions, two modes, and they do not answer the same thing:

* `alone` installs one middleware at a time. Each arm therefore pays the *fixed*
  cost of having any global hook at all -- `_handle_http` eagerly builds
  Request+State and sets `route_outcome` as soon as `_global_hooks` is
  non-empty, and `_finish_http` unwinds after-hooks -- so these costs
  deliberately do not sum to the full stack's.
* `cumulative` adds them one at a time. The first step carries that fixed cost;
  later steps are marginal.

Read together, the gap between `sum(alone)` and the full stack is the fixed
price of turning the tape on at all.

## Why this is careful

An earlier version of this measurement reported a *negative* cost for one
middleware, which is how noise announces itself. So:

* Arms are interleaved round-robin, so thermal or governor drift hits every arm
  rather than whichever one ran while the CPU was asleep.
* An A/A control -- the identical bare app entered as two separate arms, placed
  at opposite ends of the round -- measures the noise floor directly, including
  within-round drift. Placing it adjacent to its twin flatters the floor by an
  order of magnitude; it is deliberately not adjacent.
* Any delta smaller than twice that floor is reported as unresolved rather than
  as a number. On a laptop with a powersave governor, per-hook costs frequently
  land there, and the honest output is "this box cannot see it", not a
  plausible-looking figure.

Medians of repeated rounds throughout. Per this repository's benchmark policy,
`--json` records enough environment metadata to reproduce a run, and no single
run should be quoted as a win.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import platform
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .decomp import _frame_chain
from .measure import run as _run
from .measure import scope as _scope
from .measure import status_of as _status
from .measure import time_app as _time
from .sample_app import MIDDLEWARE_FACTORIES, build_realistic_app

DEFAULT_ROUNDS = 11
DEFAULT_ITERATIONS = 4000
DEFAULT_WARMUP = 2000
#: A delta must clear this multiple of the measured A/A floor to be reported.
RESOLUTION_FACTOR = 2.0


@dataclass
class Arm:
    label: str
    middleware: list[Any]
    app: Any = None
    samples: list[float] = field(default_factory=list)

    @property
    def median(self) -> float:
        return statistics.median(self.samples)

    @property
    def p95(self) -> float:
        ordered = sorted(self.samples)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]






def _configure(app: Any, middleware: list[Any]) -> Any:
    app._global_middleware = [(0, index, item) for index, item in enumerate(middleware)]
    app._dirty = True
    app._compile_routes()
    return app



class _FrameMiddleware:
    """A hook whose only cost is `depth` Python frames."""

    global_scope = True

    def __init__(self, depth: int) -> None:
        self.depth = depth
        self._chain = _frame_chain(depth) if depth else None

    async def before(self, request: Any) -> None:
        if self._chain is not None:
            self._chain(0)
        return None


async def _calibrate(
    template: dict[str, Any], rounds: int, iterations: int, warmup: int
) -> int:
    """Measure nanoseconds per Python frame, by slope.

    A single frame-removing fix is usually too small for one A/B to resolve --
    which is a statement about the instrument, not about the fix. Adding frames
    in bulk puts the signal far above the floor, and the slope converts the
    deterministic frame counts from `wreath-request-trace` into microseconds. That
    is what makes an accumulation argument checkable rather than rhetorical.
    """
    depths = (0, 50, 100, 200, 400, 800)
    arms: list[tuple[int, Any]] = []
    for depth in depths:
        app = build_realistic_app()[0]
        # Any, for the same reason sample_app.py notes: the public `Middleware`
        # union cannot describe a hook-shaped class.
        hook: Any = _FrameMiddleware(depth)
        app._global_middleware.append((99, 99, hook))
        app._dirty = True
        app._compile_routes()
        arms.append((depth, app))

    for _depth, app in arms:
        await _run(app, template, warmup)

    samples: dict[int, list[float]] = {depth: [] for depth, _ in arms}
    for _ in range(rounds):
        for depth, app in arms:
            samples[depth].append(await _time(app, template, iterations))

    medians = {depth: statistics.median(values) for depth, values in samples.items()}
    xs = [float(depth) for depth, _ in arms]
    ys = [medians[depth] for depth, _ in arms]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sum(
        (x - mean_x) ** 2 for x in xs
    )
    per_frame_ns = slope * 1000

    print(f"{'extra frames':>13s} {'us/request':>11s} {'vs 0':>9s}")
    for depth, _ in arms:
        print(f"{depth:13d} {medians[depth]:10.2f}u {medians[depth] - medians[0]:+8.2f}u")
    print(f"\nslope = {per_frame_ns:.1f} ns per Python frame")
    print(
        f"  a fix removing 11 frames is worth ~{11 * per_frame_ns / 1000:.2f}us -- "
        "often below\n  a single A/B's noise floor, and still real. Track those with\n"
        "  `wreath-request-trace`, whose frame counts are exact and deterministic,\n"
        "  and re-measure time once several have landed."
    )
    return 0


def _load_app(target: str) -> tuple[Any, list[Any]]:
    module_name, separator, attribute = target.partition(":")
    if not separator:
        raise SystemExit(f"wreath-tape-decomp: --app must be 'module:attribute', got {target!r}")
    module = importlib.import_module(module_name)
    try:
        app = getattr(module, attribute)
    except AttributeError:
        raise SystemExit(
            f"wreath-tape-decomp: {module_name} has no attribute {attribute!r}"
        ) from None
    installed = [item[2] for item in sorted(getattr(app, "_global_middleware", []))]
    if not installed:
        raise SystemExit(
            f"wreath-tape-decomp: {target} has no global middleware to decompose"
        )
    return app, installed


def _build_arms(factory: Any, names: list[str], make: Any, mode: str) -> list[Arm]:
    """Bare and A/A control bracket the round; the control is never adjacent.

    `make(i)` returns a *fresh* instance of stack member i, so no two arms ever
    share a middleware object.
    """
    plans: list[tuple[str, list[int]]] = [("bare", [])]
    full = list(range(len(names)))
    if mode in ("alone", "both"):
        plans.append(("full stack", full))
        plans.extend((f"only {names[i]}", [i]) for i in full)
    if mode in ("cumulative", "both"):
        if mode == "cumulative":
            plans.append(("full stack", full))
        plans.extend((f"+{names[i]}", full[: i + 1]) for i in full)
    plans.append(("A/A control", []))

    arms = []
    for label, indexes in plans:
        middleware = [make(i) for i in indexes]
        arms.append(Arm(label, middleware, app=_configure(factory(), middleware)))
    return arms



async def _verify(arms: list[Arm], template: dict[str, Any], when: str) -> None:
    """Every arm must still serve the request it claims to be measuring.

    Without this the tool silently lies: a middleware that starts rejecting --
    a drained token bucket is the easy way -- makes its arm faster than bare,
    and the decomposition reports the cost of a 429 as if it were the cost of
    the request. Checked after the run too, because that failure develops
    *during* it.
    """
    for arm in arms:
        status = await _status(arm.app, template)
        if status != 200:
            raise SystemExit(
                f"wreath-tape-decomp: arm {arm.label!r} answered {status}, not 200, "
                f"{when} measuring.\nIts timings would be the cost of that response, "
                "not of a served request.\nA rate limiter draining mid-run is the "
                "usual cause."
            )


async def _measure(
    arms: list[Arm], template: dict[str, Any], rounds: int, iterations: int, warmup: int
) -> None:
    for arm in arms:
        await _run(arm.app, template, warmup)
    await _verify(arms, template, "before")
    for _ in range(rounds):
        for arm in arms:  # interleaved, so drift hits every arm alike
            arm.samples.append(await _time(arm.app, template, iterations))
    await _verify(arms, template, "after")


def _report(arms: list[Arm], mode: str, floor: float, bare: float) -> dict[str, Any]:
    resolution = floor * RESOLUTION_FACTOR
    rows: list[dict[str, Any]] = []
    print(f"bare app = {bare:.2f}us/request")
    print(
        f"A/A noise floor = {floor:.2f}us ({floor / bare * 100:.1f}%); "
        f"a delta must exceed {resolution:.2f}us to be reported\n"
    )
    print(f"{'arm':30s} {'median':>8s} {'p95':>8s} {'vs bare':>9s}   {'resolved?':>9s}")
    print("-" * 72)
    for arm in arms:
        if arm.label in ("bare", "A/A control"):
            continue
        delta = arm.median - bare
        resolved = abs(delta) > resolution
        rows.append(
            {
                "arm": arm.label,
                "median_us": round(arm.median, 3),
                "p95_us": round(arm.p95, 3),
                "delta_us": round(delta, 3),
                "resolved": resolved,
            }
        )
        verdict = "yes" if resolved else "BELOW NOISE"
        print(
            f"{arm.label:30s} {arm.median:7.2f}u {arm.p95:7.2f}u "
            f"{delta:+8.2f}u   {verdict:>9s}"
        )

    summary: dict[str, Any] = {}
    full = next((arm for arm in arms if arm.label == "full stack"), None)
    if full is not None:
        total = full.median - bare
        summary["full_stack_delta_us"] = round(total, 3)
        summary["full_stack_share"] = round(total / full.median, 4)
        print(
            f"\nThe whole tape costs {total:+.2f}us, "
            f"{total / full.median * 100:.1f}% of the request."
        )
        alone = [arm for arm in arms if arm.label.startswith("only ")]
        if alone:
            parts = sum(arm.median - bare for arm in alone)
            summary["sum_of_alone_us"] = round(parts, 3)
            summary["fixed_cost_estimate_us"] = round((parts - total) / max(1, len(alone) - 1), 3)
            print(
                f"Installed one at a time they sum to {parts:+.2f}us -- more than the "
                f"whole tape,\nbecause each arm re-pays the fixed cost of having any "
                f"global hook at all.\nThat fixed cost is roughly "
                f"{(parts - total) / max(1, len(alone) - 1):.2f}us: eager Request+State "
                f"construction,\n`route_outcome` bookkeeping, and the after-hook unwind."
            )
    unresolved = [row["arm"] for row in rows if not row["resolved"]]
    if unresolved:
        print(
            f"\n{len(unresolved)} arm(s) did not clear the noise floor on this box. "
            "Their cost is\nnot zero -- it is unmeasured. Quiet the machine (performance "
            "governor, no\nbackground load) or raise --rounds/--iterations before "
            "attributing anything to them."
        )
    return {"rows": rows, "summary": summary, "unresolved": unresolved}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-tape-decomp",
        description="Measure what each global middleware costs a request.",
    )
    parser.add_argument(
        "--mode",
        choices=("alone", "cumulative", "both"),
        default="both",
        help="alone: each installed by itself. cumulative: added one at a time.",
    )
    parser.add_argument("--app", help="decompose your own app, as 'module:attribute'")
    parser.add_argument("--method", default="GET")
    parser.add_argument("--path", default=None)
    parser.add_argument("--header", action="append", default=[], metavar="NAME:VALUE")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--json", type=Path, help="write results plus environment metadata")
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="measure ns per Python frame, to convert wreath-request-trace counts to time",
    )
    args = parser.parse_args(argv)

    if args.calibrate:
        sample_headers, sample_path = build_realistic_app()[1], build_realistic_app()[3]
        print(f"rounds={args.rounds} iterations={args.iterations}\n")
        return asyncio.run(
            _calibrate(
                _scope("GET", args.path or sample_path, sample_headers),
                args.rounds,
                args.iterations,
                args.warmup,
            )
        )

    if args.app:
        app, installed = _load_app(args.app)
        factory = lambda: app  # noqa: E731 -- one instance; arms reconfigure it
        names = [type(item).__name__ for item in installed]
        make = installed.__getitem__  # cannot rebuild a user's instances
        headers: dict[str, str] = {"host": "example.com"}
        path = args.path or "/"
        print(
            "note: --app reuses one application object and one set of middleware\n"
            "      instances across arms, because neither can be rebuilt from the\n"
            "      outside. Stateful middleware (rate limiters) may therefore carry\n"
            "      state between arms; the 200-check after the run catches the\n"
            "      damaging case.\n"
        )
    else:
        _, headers, method, sample_path = build_realistic_app()
        factories = MIDDLEWARE_FACTORIES
        names = [type(factory()).__name__ for factory in factories]
        make = lambda index: factories[index]()  # noqa: E731 -- fresh per arm
        factory = lambda: build_realistic_app()[0]  # noqa: E731
        path = args.path or sample_path
        args.method = args.method if args.method != "GET" else method

    for item in args.header:
        name, separator, value = item.partition(":")
        if not separator:
            raise SystemExit(f"wreath-tape-decomp: --header wants NAME:VALUE, got {item!r}")
        headers[name.strip().lower()] = value.strip()

    arms = _build_arms(factory, names, make, args.mode)
    template = _scope(args.method, path, headers)

    print(
        f"rounds={args.rounds} iterations={args.iterations} warmup={args.warmup} "
        f"arms={len(arms)}\nmeasuring {args.method} {path} ... "
    )
    sys.stdout.flush()
    asyncio.run(_measure(arms, template, args.rounds, args.iterations, args.warmup))

    bare = next(arm for arm in arms if arm.label == "bare").median
    control = next(arm for arm in arms if arm.label == "A/A control").median
    floor = abs(control - bare)
    print()
    payload = _report(arms, args.mode, floor, bare)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "environment": {
                        "python": sys.version,
                        "implementation": platform.python_implementation(),
                        "platform": platform.platform(),
                        "machine": platform.machine(),
                        "processor": platform.processor(),
                        "event_loop": type(asyncio.new_event_loop()).__name__,
                    },
                    "parameters": {
                        "mode": args.mode,
                        "rounds": args.rounds,
                        "iterations": args.iterations,
                        "warmup": args.warmup,
                        "method": args.method,
                        "path": path,
                        "app": args.app or "built-in realistic sample",
                    },
                    "bare_us": round(bare, 3),
                    "noise_floor_us": round(floor, 3),
                    **payload,
                    "samples": {arm.label: [round(v, 3) for v in arm.samples] for arm in arms},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nwreath-tape-decomp: wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
