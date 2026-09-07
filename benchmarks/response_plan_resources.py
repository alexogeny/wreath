from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sys
import tracemalloc
from pathlib import Path
from time import process_time_ns
from typing import Any


def resident_bytes() -> dict[str, int]:
    fields = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        name, _, value = line.partition(":")
        if name in {"Rss", "Pss"}:
            fields[name.lower() + "_bytes"] = int(value.split()[0]) * 1024
    return fields


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--scenario", choices=("compile", "unannotated", "execute"), required=True)
    parser.add_argument("--routes", type=int, default=10_000)
    parser.add_argument("--executions", type=int, default=100_000)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    if args.routes <= 0 or args.executions <= 0:
        parser.error("--routes and --executions must be positive")
    source_root = args.source_root.resolve()
    sys.path.insert(0, str(source_root))
    from wreath import binding
    from wreath.response import _EncodedJSON

    source = Path(binding.__file__).resolve()
    if not source.is_relative_to(source_root):
        raise RuntimeError(f"Binding source {source} is outside {source_root}")
    payload = [{"n": 1}, {"n": -2}]
    expected_body = b'[{"n":1},{"n":-2}]'

    def handler(request: Any) -> Any:
        return request

    annotation = Any if args.scenario == "unannotated" else list[dict[str, int]]
    checked = binding.compile_response_validator(handler, annotation)
    checked(payload)
    gc.collect()
    before = resident_bytes()
    if args.trace:
        tracemalloc.start()
    start = process_time_ns()
    if args.scenario == "execute":
        for _ in range(args.executions):
            result = checked(payload)
    else:
        wrappers = [
            binding.compile_response_validator(handler, annotation) for _ in range(args.routes)
        ]
    metrics = {"workload_cpu_ns": process_time_ns() - start}
    if args.trace:
        metrics["retained_bytes"], metrics["peak_bytes"] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    metrics.update(resident_bytes())
    metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
    digest = hashlib.sha256()
    if args.scenario == "execute":
        if not isinstance(result, _EncodedJSON) or result.body != expected_body:
            raise RuntimeError("Executed response differs from the literal JSON oracle")
        digest.update(result.body)
        count = args.executions
    else:
        for wrapper in wrappers:
            result = wrapper(payload)
            if args.scenario == "unannotated":
                if wrapper is not handler or result is not payload:
                    raise RuntimeError("Unannotated response did not retain handler identity")
                digest.update(expected_body)
            else:
                if not isinstance(result, _EncodedJSON) or result.body != expected_body:
                    raise RuntimeError("Compiled response differs from the literal JSON oracle")
                digest.update(result.body)
        count = len(wrappers)
        if count != args.routes:
            raise RuntimeError(f"Expected {args.routes} wrappers, got {count}")
    metrics["source"] = str(source)
    metrics["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps({"count": count, "sha256": digest.hexdigest()}, sort_keys=True))


if __name__ == "__main__":
    main()
