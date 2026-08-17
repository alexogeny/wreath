"""Measure PolicyRouteTable match cost, for the MaskMap key experiment.

`docs/plans/bitset-routing.md` ("Discriminating bytes") argues the remaining
per-lookup cost in the bitset matcher is not *finding* the bucket -- probe
chains are length 1 -- but that `hash_bytes` reads every byte of the segment
and then ~60% of lookups `memcmp` it again, so each segment is read ~1.6x.
This benchmark exists to decide that with numbers rather than argument.

It is a focused route-table microbenchmark, not an end-to-end server result.
Nothing here touches the request path.

The route vocabularies matter more than the route count, because they are what
decides whether one or two byte offsets can separate the literals at a
position:

- ``words``    -- short distinct nouns; the first byte usually discriminates.
- ``prefixed`` -- a long shared prefix and a varying tail ("resource-137"), so
                  the discriminating bytes sit deep in the segment. This is the
                  case a naive first-byte/last-byte key would fumble.
- ``tenants``  -- the shape of ``bench_router_pruning``: a repeated literal
                  group, a distinct per-leaf literal, and a path parameter.
- ``hostile``  -- literals differing only in the final byte of a long common
                  prefix, all the same length.

Request paths are built only from routes that carry at least one parameter,
because a fully-literal route is answered by the static dict and never reaches
the bitmap at all.
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from wreath._native import _core

WORDS = [
    "api", "users", "orders", "items", "posts", "teams", "files", "jobs",
    "events", "tokens", "roles", "plans", "sites", "hooks", "keys", "logs",
    "search", "billing", "reports", "settings", "webhooks", "sessions",
]


def vocabulary(kind: str, size: int, rng: random.Random) -> list[str]:
    if kind == "realwords":
        # A real route table draws its literal segments from a modest vocabulary
        # of actual words and reuses them across many routes. Tying the pool
        # size to the route count instead (as "words" does) silently turns a
        # large table into the "prefixed" case, because the filler is numbered.
        return list(WORDS)
    if kind == "words":
        pool = list(WORDS)
        while len(pool) < size:
            pool.append(f"{rng.choice(WORDS)}{len(pool)}")
        return pool[:size]
    if kind == "prefixed":
        return [f"resource-{i}" for i in range(size)]
    if kind == "tenants":
        return (
            [f"tenant-{i}" for i in range(size)]
            + [f"group-{i}" for i in range(10)]
            + ["control", "services"]
        )
    if kind == "hostile":
        return [f"organization-unit-{i:04d}" for i in range(size)]
    raise ValueError(f"unknown vocabulary: {kind}")


def build_routes(
    routes: int, segmax: int, param: float, vocab: str, seed: int
) -> tuple[list[str], list[str]]:
    """Return (route paths, request paths). Requests only hit parameter routes."""
    rng = random.Random(seed)
    pool = vocabulary(vocab, max(routes, 16), rng)
    seen: set[str] = set()
    paths: list[str] = []
    parameterized: list[list[str | None]] = []
    guard = 0
    while len(paths) < routes and guard < routes * 100:
        guard += 1
        nseg = rng.randint(2, segmax)
        shape: list[str | None] = [
            None if rng.random() < param else rng.choice(pool) for _ in range(nseg)
        ]
        if all(s is None for s in shape):
            continue  # an all-parameter route at every position: no shape to key
        path = "/" + "/".join(
            f"{{p{i}}}" if s is None else s for i, s in enumerate(shape)
        )
        key = "/" + "/".join("*" if s is None else s for s in shape)
        if key in seen:
            continue  # same literal/parameter shape is a conflicting route
        seen.add(key)
        paths.append(path)
        if any(s is None for s in shape):
            parameterized.append(shape)

    requests: list[str] = []
    for shape in parameterized:
        requests.append(
            "/" + "/".join(
                f"v{rng.randrange(1000)}" if s is None else s for s in shape
            )
        )
    return paths, requests


def build_table(factory: Any, paths: list[str]) -> Any:
    table = factory()
    for index, path in enumerate(paths):
        table.add(path, "GET", index, (0,))
    return table


def sample(table: Any, requests: list[str], iterations: int, trials: int) -> list[float]:
    """Median-friendly per-match nanoseconds, cycling over the request set."""
    match = table.match
    n = len(requests)
    samples: list[float] = []
    for _ in range(trials):
        started = perf_counter_ns()
        for i in range(iterations):
            match("GET", requests[i % n], 0)
        elapsed = perf_counter_ns() - started
        samples.append(elapsed / iterations)
    return samples


def loop_floor(requests: list[str], iterations: int, trials: int) -> float:
    """The list-indexing and call overhead the timed loop adds to every arm."""
    n = len(requests)

    def noop(_method: str, _path: str, _mask: int) -> None:
        return None

    samples: list[float] = []
    for _ in range(trials):
        started = perf_counter_ns()
        for i in range(iterations):
            noop("GET", requests[i % n], 0)
        samples.append((perf_counter_ns() - started) / iterations)
    return statistics.median(samples)


SHAPES = [
    # (routes, segmax, param, vocabulary)
    (64, 5, 0.3, "realwords"),
    (256, 5, 0.3, "realwords"),
    (512, 6, 0.3, "realwords"),
    (512, 6, 0.5, "realwords"),
    (2000, 7, 0.3, "realwords"),
    (64, 5, 0.3, "words"),
    (256, 5, 0.3, "words"),
    (512, 5, 0.3, "words"),
    (512, 6, 0.5, "words"),
    (2000, 6, 0.3, "words"),
    (256, 5, 0.3, "prefixed"),
    (512, 5, 0.3, "prefixed"),
    (512, 6, 0.5, "prefixed"),
    (512, 5, 0.3, "tenants"),
    (512, 5, 0.3, "hostile"),
    (2000, 6, 0.3, "hostile"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=200_000)
    parser.add_argument("--trials", type=int, default=9)
    parser.add_argument("--seed", type=int, default=20260716)
    parser.add_argument("--label", default="unlabelled",
                        help="which build this run measured, e.g. 'baseline'")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    if _core is None or getattr(_core, "PolicyRouteTable", None) is None:
        raise SystemExit("native _core with PolicyRouteTable is required")

    rows = []
    for routes, segmax, param, vocab in SHAPES:
        paths, requests = build_routes(routes, segmax, param, vocab, args.seed)
        if not requests:
            continue
        table = build_table(_core.PolicyRouteTable, paths)
        # Warm every group into existence before timing: groups are built lazily
        # on first match, and that compile must not land inside a timed trial.
        for request in requests:
            table.match("GET", request, 0)
        hits = sum(table.match("GET", r, 0) is not None for r in requests)
        _core.PolicyRouteTable.probe_stats()
        for request in requests:
            table.match("GET", request, 0)
        probe = _core.PolicyRouteTable.probe_stats()

        raw = sample(table, requests, args.iterations, args.trials)
        rows.append({
            "routes": routes,
            "segmax": segmax,
            "param": param,
            "vocabulary": vocab,
            "registered": len(paths),
            "requests": len(requests),
            "hit_rate": hits / len(requests),
            "stats": table.stats(),
            "probe": probe,
            "ns_per_match": {
                "raw": raw,
                "median": statistics.median(raw),
                "min": min(raw),
            },
        })

    _paths, floor_requests = build_routes(64, 5, 0.3, "words", args.seed)
    document = {
        "tool": "benchmarks.bench_bitset_key",
        "schema_version": 1,
        "label": args.label,
        "python": sys.version,
        "platform": platform.platform(),
        "core_module": _core.__file__,
        "iterations": args.iterations,
        "trials": args.trials,
        "seed": args.seed,
        "empty_loop_ns": loop_floor(floor_requests, args.iterations, args.trials),
        "rows": rows,
    }
    text = json.dumps(document, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
        print(f"wrote {args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
