from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
import sys
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--shape", choices=("chain", "star", "plain"), required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--metrics", type=Path, required=True)
    args = parser.parse_args()
    if args.size < 1 or args.iterations < 1:
        parser.error("size and iterations must be positive")
    spec = importlib.util.spec_from_file_location("resolver_bench_subject", args.source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load resolver source: {args.source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    names = [f"field_{index}" for index in range(args.size)]
    selected = [SimpleNamespace(name=name) for name in names]
    if args.shape == "chain":
        resolvers = {
            name: SimpleNamespace(requires=tuple(names[index + 1 : index + 2]))
            for index, name in enumerate(names)
        }
        expected = selected[::-1]
    elif args.shape == "star":
        resolvers = {names[0]: SimpleNamespace(requires=tuple(names[1:]))}
        expected = selected[1:] + selected[:1]
    else:
        resolvers = {}
        expected = selected
    order_fields = module.order_fields
    for _ in range(20):
        order_fields(selected, resolvers, type_name="Thing")
    gc.collect()
    started = time.process_time_ns()
    for _ in range(args.iterations):
        result = order_fields(selected, resolvers, type_name="Thing")
    cpu_ns = time.process_time_ns() - started
    if len(result) != len(expected) or any(
        actual is not wanted for actual, wanted in zip(result, expected, strict=True)
    ):
        raise RuntimeError("resolver walk returned an unexpected selection order")
    gc.collect()
    tracemalloc.start()
    order_fields(selected, resolvers, type_name="Thing")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    args.metrics.write_text(
        json.dumps(
            {
                "cpu_ns_per_call": cpu_ns / args.iterations,
                "peak_traced_bytes": peak,
            }
        )
    )
    print(
        json.dumps(
            {
                "shape": args.shape,
                "size": args.size,
                "iterations": args.iterations,
                "order_sha256": hashlib.sha256(
                    "\n".join(item.name for item in result).encode()
                ).hexdigest(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
