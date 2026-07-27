"""Memory cost of each routing backend over an application's lifecycle.

Wallclock is only half the question, and for these backends it is the less
interesting half: the compiled forms differ by orders of magnitude in size
(see docs/plans/bitset-routing.md), while match time differs by a few percent.
This measures the other half on the real 10,000-route benchmark application.

Per routing mode, in a *fresh subprocess* (resident memory is only meaningful in
isolation, and route registration happens at import):

- ``rss_registered``   after importing the app, so routes exist but are not compiled
- ``rss_compiled``     after ``_compile_routes()``
- ``compiled_bytes``   the difference: what compiling eagerly costs resident
- ``lazy_bytes``       further growth while matching (groups build on first use)
- ``total_bytes``      eager + lazy: what the backend actually holds
- ``vmhwm_bytes``      peak resident over the whole lifecycle, from /proc VmHWM
- ``traced_peak``      tracemalloc peak during compile alone
- ``compile_seconds``  wall time to compile
- ``rss_steady``       after driving matches, to expose per-match churn

Run:

    uv run python -m benchmarks.bench_routing_memory
    uv run python -m benchmarks.bench_routing_memory --trials 5 --output PATH
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tracemalloc
from pathlib import Path
from time import perf_counter_ns
from typing import Any

MODES = ("decision", "trie", "bitset")


def _proc_kb(field: str) -> int:
    """One /proc/self/status size field, in bytes."""
    with open("/proc/self/status") as handle:
        for line in handle:
            if line.startswith(field):
                return int(line.split()[1]) * 1024
    raise LookupError(field)


def _rss() -> int:
    return _proc_kb("VmRSS:")


def _param_heavy_app(mode: str, routes: int, segmax: int, param: float) -> tuple[Any, list]:
    """A synthetic parameter-heavy table: the shape the bitset is built for.

    The benchmark application is mostly static routes, and a fully literal route
    is answered by the shared static dict without reaching any of the three
    backends. That makes it the wrong table to see the compiled forms diverge on
    -- so measure this one alongside it, not instead of it.
    """
    import random

    from wreath import Wreath

    rng = random.Random(20260716)
    words = ["api", "users", "orders", "items", "teams", "files", "jobs", "keys"]
    app = Wreath(routing=mode)

    async def handler(request):
        return b"ok"

    specs, seen = [], set()
    while len(specs) < routes:
        nseg = rng.randint(2, segmax)
        shape = [None if rng.random() < param else rng.choice(words) for _ in range(nseg)]
        if all(s is None for s in shape):
            continue
        key = tuple("*" if s is None else s for s in shape)
        if key in seen:
            continue
        seen.add(key)
        path = "/" + "/".join(f"{{p{i}}}" if s is None else s for i, s in enumerate(shape))
        app.route(path, methods=["GET"])(handler)
        specs.append(("GET", "/" + "/".join(
            f"v{rng.randrange(100)}" if s is None else s for s in shape)))
    return app, specs


def _child(mode: str, matches: int, shape: str) -> dict[str, Any]:
    """Measure one mode. Runs as its own process; prints one JSON document."""
    os.environ["WREATH_BENCH_ROUTING"] = mode
    os.environ["WREATH_BENCH_FRAMEWORK"] = "wreath"
    import gc

    if shape == "param-heavy":
        app, ROUTE_SPECS = _param_heavy_app(mode, 512, 6, 0.5)
    else:
        from benchmarks.apps import ROUTE_SPECS, app

    gc.collect()
    rss_registered = _rss()

    tracemalloc.start()
    started = perf_counter_ns()
    app._compile_routes()
    compile_seconds = (perf_counter_ns() - started) / 1e9
    _current, traced_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    gc.collect()
    rss_compiled = _rss()

    # Drive real matches: per-match allocation churn shows up here, not in the
    # compiled size. Cycle the route set so no single group stays hot.
    router = app.router
    probes = [
        (method, path.replace("{tenant_id}", "acme").replace("{item_id}", "42"))
        for method, path in ROUTE_SPECS
    ]
    for index in range(matches):
        method, path = probes[index % len(probes)]
        router.match(method, path, 0)
    gc.collect()
    rss_steady = _rss()

    table = getattr(router, "_table", None)
    stats = table.stats() if hasattr(table, "stats") else None
    return {
        "mode": mode,
        "shape": shape,
        "routes": len(ROUTE_SPECS),
        "matches_driven": matches,
        "rss_registered": rss_registered,
        "rss_compiled": rss_compiled,
        "compiled_bytes": rss_compiled - rss_registered,
        "rss_steady": rss_steady,
        # Not all backends build eagerly: both the decision tree and the bitset
        # build a group on its first match, so `_compile_routes()` does not show
        # their full cost. Measuring only up to compile understates the decision
        # tree by ~30 MiB on a parameter-heavy table. `total_bytes` is the honest
        # figure: everything the backend is still holding once it has served the
        # whole route set.
        "lazy_bytes": rss_steady - rss_compiled,
        "total_bytes": rss_steady - rss_registered,
        "vmhwm_bytes": _proc_kb("VmHWM:"),
        "traced_peak_bytes": traced_peak,
        "compile_seconds": compile_seconds,
        "table_stats": stats,
    }


def _mib(value: float) -> str:
    return f"{value / (1024 * 1024):,.1f} MiB"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--matches", type=int, default=200_000)
    parser.add_argument("--mode", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--shape", choices=("app", "param-heavy"), default="app",
                        help="the real 10k-route benchmark app, or a synthetic "
                             "parameter-heavy table")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.child:
        print(json.dumps(_child(args.child, args.matches, args.shape)))
        return

    rows: dict[str, list[dict[str, Any]]] = {}
    for mode in args.mode:
        for _trial in range(args.trials):
            proc = subprocess.run(
                [sys.executable, "-m", "benchmarks.bench_routing_memory",
                 "--child", mode, "--matches", str(args.matches),
                 "--shape", args.shape],
                capture_output=True, text=True, check=True,
            )
            rows.setdefault(mode, []).append(json.loads(proc.stdout.strip().splitlines()[-1]))

    def med(mode: str, key: str) -> float:
        return statistics.median([r[key] for r in rows[mode]])

    print(f"shape={args.shape}  {rows[args.mode[0]][0]['routes']:,} routes  "
          f"{args.trials} trials, medians\n")
    print(f"{'mode':<10}{'total':>12}{'(eager':>10}{'+lazy)':>10}"
          f"{'peak RSS':>12}{'compile':>11}")
    for mode in args.mode:
        print(f"{mode:<10}{_mib(med(mode, 'total_bytes')):>12}"
              f"{_mib(med(mode, 'compiled_bytes')):>10}"
              f"{_mib(med(mode, 'lazy_bytes')):>10}"
              f"{_mib(med(mode, 'vmhwm_bytes')):>12}"
              f"{med(mode, 'compile_seconds') * 1000:>9.1f} ms")

    base = args.mode[0]
    print(f"\nrelative to {base}:")
    for mode in args.mode[1:]:
        for key, label in (("total_bytes", "total resident"), ("vmhwm_bytes", "peak RSS"),
                           ("compile_seconds", "compile time")):
            ratio = med(base, key) / med(mode, key) if med(mode, key) else float("inf")
            print(f"  {mode:<8} {label:<13} {ratio:6.2f}x smaller/faster than {base}")

    document = {
        "tool": "benchmarks.bench_routing_memory",
        "schema_version": 1,
        "python": sys.version,
        "platform": platform.platform(),
        "trials": args.trials,
        "matches": args.matches,
        "raw": rows,
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2) + "\n")
        print(f"\nwrote {args.output}")


if __name__ == "__main__":
    main()
