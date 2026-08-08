"""What a request costs the *instance*, not the caller.

Every other benchmark here reports wall-clock microseconds. On a small cloud
instance that is the wrong unit twice over.

A burstable instance -- t4g/t3 on AWS, e2 on GCP, B-series on Azure -- does not
sell you a CPU, it sells you a *rate*. A t4g.small has 2 vCPUs and a 20%
baseline, so it earns roughly 24 CPU-minutes an hour and spends whatever it
uses. Stay under and credits accumulate; go over and you draw down a bucket that
eventually empties, at which point the kernel clamps you to baseline and
throughput falls off a cliff that looks, from the outside, exactly like a memory
leak. The number that predicts that cliff is CPU-seconds per request.

And the two numbers diverge. Wall time counts every microsecond the request
spent blocked on a socket or a database; the instance is not billed for those
and cannot use them to serve anyone else's request either -- but a saving there
raises the throughput ceiling only if the process was CPU-bound to begin with.
The `cpu/wall` ratio each arm reports is how to tell which case you are in.

    uv run python benchmarks/bench_cpu_cost.py

Arms are interleaved with an A/A control and the round's direction alternates,
per `_devtools/measure.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from pathlib import Path
from typing import Any

from wreath import Response, Wreath
from wreath._devtools.measure import Arm, measure_apps, report, report_cpu, scope
from wreath._devtools.sample_app import POLICY_FACTORIES, policy_from_components

#: A t4g.small: 2 vCPU, 20% sustained baseline. The comparison every other
#: cloud's burstable tier is a rescaling of.
BASELINE_VCPU = 0.4

_BODY = Response(b'{"ok":1}', media_type=b"application/json")


def _app(components: list[Any]) -> Wreath:
    app = Wreath(
        http_policy=policy_from_components(components) if components else None
    )

    @app.get("/i/{x}")
    async def hot(request: Any) -> Response:
        return _BODY

    @app.get("/cold")
    async def cold(request: Any) -> Response:
        return _BODY

    app._compile_routes()
    return app


def _bare() -> Wreath:
    return _app([])


def _full() -> Wreath:
    return _app([factory() for factory in POLICY_FACTORIES])


def _ceiling() -> Wreath:
    return _app([factory() for factory in POLICY_FACTORIES[:2]])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--iterations", type=int, default=2500)
    parser.add_argument("--warmup", type=int, default=2000)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    arms = [
        Arm("full: 7 policy components", app=_full()),
        Arm("ceiling: 2 of 7", app=_ceiling()),
        Arm("bare: no policy", app=_bare()),
        Arm("A/A control", app=_full()),
    ]
    template = scope("GET", "/i/42", {"host": "example.com"})

    asyncio.run(measure_apps(arms, template, args.rounds, args.iterations, args.warmup))

    print(f"python {platform.python_version()} on {platform.platform()}")
    print(f"rounds={args.rounds} iterations={args.iterations}\n")
    print("── wall clock: what the caller waits for ──\n")
    wall = report(arms, "full: 7 policy components", "A/A control")
    print("\n── CPU: what the instance is billed for ──\n")
    cpu = report_cpu(arms, "full: 7 policy components", "A/A control")

    print("\n── sustained throughput on one t4g.small (0.4 vCPU baseline) ──\n")
    print(f"  {'arm':32s} {'req/s at baseline':>18s}")
    for arm in arms:
        if arm.label == "A/A control":
            continue
        # CPU microseconds per request against 0.4 CPU-seconds available per
        # second. This is a ceiling for the framework alone -- a real handler's
        # own CPU comes straight off it.
        print(f"  {arm.label:32s} {BASELINE_VCPU / (arm.cpu_median / 1e6):18,.0f}")
    print(
        "\n  Framework only. A handler doing real work spends its own CPU on top,\n"
        "  so treat these as an upper bound that no application reaches."
    )

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"wall": wall, "cpu": cpu}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
