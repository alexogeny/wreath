from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import sys
import tracemalloc
from pathlib import Path
from time import process_time_ns


def resident_bytes() -> dict[str, int]:
    values = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        name, _, value = line.partition(":")
        if name in {"Rss", "Pss"}:
            values[name.lower() + "_bytes"] = int(value.split()[0]) * 1024
    return values


async def measure(args: argparse.Namespace) -> None:
    from wreath.objects import MemoryObjectStore

    store = MemoryObjectStore()
    data = b"x" if args.scenario == "tiny" else b"x" * args.size
    key_count = 1000 if args.scenario == "tiny" else 8 if args.scenario == "list" else 1
    keys = [f"body/{index:04d}" for index in range(key_count)]
    if args.scenario != "tiny":
        for key in keys:
            await store.write(key, data, content_type="text/plain")
    span = (1024, args.size // 2 - 1) if args.scenario == "range" else None
    expected = hashlib.sha256()
    expected.update(memoryview(data) if span is None else memoryview(data)[span[0] : span[1] + 1])
    expected_etag = hashlib.md5(data, usedforsecurity=False).hexdigest()
    digest = hashlib.sha256()
    gc.collect()
    before = resident_bytes()
    if args.trace:
        tracemalloc.start()
    sampled = False
    sample_cpu_ns = 0
    metrics = {}
    start = process_time_ns()
    if args.scenario == "stat":
        stats = [await store.stat(keys[0]) for _ in range(8)]
    elif args.scenario == "list":
        stats = []
        for _ in range(2):
            stats.extend([stat async for stat in store.list("body/")])
    elif args.scenario == "tiny":
        for key in keys:
            await store.write(key, data, content_type="text/plain")
    else:
        total = 0
        async for chunk in store.read_stream(keys[0], range=span):
            if not sampled:
                sample_start = process_time_ns()
                metrics.update(resident_bytes())
                sample_cpu_ns = process_time_ns() - sample_start
                sampled = True
            total += len(chunk)
            digest.update(chunk)
    metrics["workload_cpu_ns"] = process_time_ns() - start - sample_cpu_ns
    if args.trace:
        metrics["retained_bytes"], metrics["peak_bytes"] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    if not sampled:
        metrics.update(resident_bytes())
    metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
    if args.scenario in {"stat", "list"}:
        count = 8 if args.scenario == "stat" else 16
        if len(stats) != count or any(
            stat.size != len(data)
            or stat.etag != expected_etag
            or stat.content_type != "text/plain"
            for stat in stats
        ):
            raise RuntimeError("Metadata differs from the immutable payload oracle")
        output = {"stats": len(stats), "etag": expected_etag}
    elif args.scenario == "tiny":
        stats = [stat async for stat in store.list("body/")]
        if len(stats) != 1000 or any(
            stat.size != 1 or stat.etag != expected_etag for stat in stats
        ):
            raise RuntimeError("Tiny-object metadata differs from the payload oracle")
        output = {"objects": len(stats), "etag": expected_etag}
    else:
        expected_size = len(data) if span is None else span[1] - span[0] + 1
        if total != expected_size or digest.digest() != expected.digest():
            raise RuntimeError("Range stream differs from the independent slice digest oracle")
        output = {"bytes": total, "sha256": digest.hexdigest()}
    metrics["source"] = str(args.source)
    metrics["source_sha256"] = hashlib.sha256(args.source.read_bytes()).hexdigest()
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps(output, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument(
        "--scenario", choices=("stat", "list", "range", "whole", "tiny"), required=True
    )
    parser.add_argument("--size", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    if args.size <= 2048:
        parser.error("--size must exceed 2048 bytes")
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from wreath import objects

    args.source = Path(objects.__file__).resolve()
    if not args.source.is_relative_to(root):
        raise RuntimeError(f"Object source {args.source} is outside {root}")
    asyncio.run(measure(args))


if __name__ == "__main__":
    main()
