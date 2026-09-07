"""Measure named URL lookups and their registration/invalidation controls.

Point --source at frozen before/after trees. Warm lookup cases establish the
existing application-image route snapshot before building the name index.
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
from dataclasses import replace
from pathlib import Path
from time import process_time_ns

from route_image import resident_bytes


async def endpoint(item: str) -> str:
    return item


async def receive():
    return {"type": "http.request", "body": b""}


def populate(app, size, host):
    for index in range(size):
        app.get(f"/items/{index}/{{item}}", name=f"item-{index}", host=host)(endpoint)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--size", type=int, default=4000)
    parser.add_argument("--repeats", type=int, default=4000)
    parser.add_argument(
        "--scenario", required=True, choices=("path", "host", "missing", "registration", "mutation")
    )
    args = parser.parse_args()
    if args.size <= 0 or args.repeats <= 0:
        parser.error("--size and --repeats must be positive")
    sys.path.insert(0, str(args.source.resolve()))
    import wreath.app
    from wreath import Wreath
    from wreath.request import Request

    loaded = Path(wreath.app.__file__).resolve()
    if not loaded.is_relative_to(args.source.resolve()):
        raise RuntimeError(f"loaded {loaded}, expected source under {args.source}")
    app = Wreath(ai_scraping="allow")
    host = "{tenant}.example.test" if args.scenario == "host" else None
    name = f"item-{args.size - 1}"
    expected = f"/items/{args.size - 1}/a%20b"
    if args.scenario != "registration":
        populate(app, args.size, host)
        app._application_image.routes()
        if app.url_path_for(name, item="a b") != expected:
            raise RuntimeError("warm lookup did not find the final route")
    request = Request(
        {"type": "http", "scheme": "https", "root_path": "/api", "headers": []},
        receive,
        app=app,
    )
    checksum = 0
    gc.collect()
    started = process_time_ns()
    if args.scenario == "registration":
        populate(app, args.size, host)
        checksum = len(app._routes)
    else:
        for iteration in range(args.repeats):
            if args.scenario == "missing":
                try:
                    app.url_path_for("missing")
                except KeyError as error:
                    if error.args != ("no route named 'missing'",):
                        raise RuntimeError("missing-route message changed") from error
                    checksum += 1
                    continue
                raise RuntimeError("missing route unexpectedly resolved")
            if args.scenario == "mutation":
                app._routes[-1] = replace(app._routes[-1], path=f"/changed/{iteration}/{{item}}")
                expected = f"/changed/{iteration}/a%20b"
            if args.scenario == "host":
                value = request.url_for(name, item="a b", tenant="shop")
                correct = "https://shop.example.test/api" + expected
            else:
                value = app.url_path_for(name, item="a b")
                correct = expected
            if value != correct:
                raise RuntimeError(f"expected {correct}, got {value}")
            checksum += len(value)
    elapsed = process_time_ns() - started
    metrics = {"cpu_ns": elapsed, **resident_bytes(), "source": str(loaded)}
    args.metrics.write_text(json.dumps(metrics) + "\n")
    print(json.dumps({"checksum": checksum, "routes": len(app._routes)}, sort_keys=True))


if __name__ == "__main__":
    main()
