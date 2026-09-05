"""Route-image fixed work for interleaved resource-bench comparisons.

Run with the same Python executable and --source pointing at each source tree.
OpenAPI includes first image analysis; lookups prewarm it; control touches the
same number of routes without resolving either contracts or operation IDs.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sys
from pathlib import Path
from time import process_time_ns


def resident_bytes() -> dict[str, int]:
    fields = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        name, _, value = line.partition(":")
        if name in {"Rss", "Pss"}:
            fields[name.lower() + "_bytes"] = int(value.split()[0]) * 1024
    return fields


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--size", type=int, default=4000)
    parser.add_argument("--scenario", choices=("openapi", "lookups", "control"), default="openapi")
    args = parser.parse_args()
    if args.size <= 0:
        parser.error("--size must be positive")
    sys.path.insert(0, str(args.source.resolve()))

    import wreath.app
    from wreath import Wreath
    from wreath.openapi import generate_openapi

    loaded = Path(wreath.app.__file__).resolve()
    if not loaded.is_relative_to(args.source.resolve()):
        raise RuntimeError(f"loaded {loaded}, expected source under {args.source}")
    app = Wreath(ai_scraping="allow")

    async def endpoint(limit: int = 10) -> str:
        return str(limit)

    for index in range(args.size):
        app.get(f"/items/{index}", summary="List items")(endpoint)
    image = app._application_image
    if args.scenario != "openapi":
        definitions = image.routes()
        image.binding_specs()
        image.operation_ids()
    gc.collect()
    started = process_time_ns()
    if args.scenario == "openapi":
        result = generate_openapi(app)
        if len(result["paths"]) != args.size:
            raise RuntimeError("OpenAPI did not include every registered route")
    elif args.scenario == "lookups":
        result = [
            (image.operation_id(route, "GET"), len(image.contract_candidates(route, "GET")))
            for route in definitions
        ]
    else:
        result = sum(len(image._requirements) for _ in definitions)
        if result != args.size * args.size:
            raise RuntimeError("control did not touch every route")
    elapsed = process_time_ns() - started
    metrics = {"cpu_ns": elapsed, **resident_bytes(), "source": str(loaded)}
    args.metrics.write_text(json.dumps(metrics) + "\n")
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    print(json.dumps({"size": args.size, "sha256": hashlib.sha256(encoded).hexdigest()}))


if __name__ == "__main__":
    main()
