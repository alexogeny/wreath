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


def resident_bytes():
    values = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        name, _, value = line.partition(":")
        if name in {"Rss", "Pss"}:
            values[name.lower() + "_bytes"] = int(value.split()[0]) * 1024
    return values


async def measure(args):
    from wreath._asgi_state import ResponseCapture

    expected = hashlib.sha256()
    for index in range(args.chunks):
        expected.update(bytes([index % 251]) * args.chunk_bytes)
    gc.collect()
    before = resident_bytes()
    if args.trace:
        tracemalloc.start()
    capture = ResponseCapture()
    await capture.send({"type": "http.response.start", "status": 200})
    for index in range(args.chunks):
        await capture.send(
            {
                "type": "http.response.body",
                "body": bytes([index % 251]) * args.chunk_bytes,
                "more_body": index < args.chunks - 1,
            }
        )
    start = process_time_ns()
    for _ in range(args.reads):
        body = capture.body
    metrics = {"workload_cpu_ns": process_time_ns() - start}
    if args.trace:
        metrics["retained_bytes"], metrics["peak_bytes"] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    metrics.update(resident_bytes())
    metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
    digest = hashlib.sha256(body)
    if digest.digest() != expected.digest() or len(body) != args.chunks * args.chunk_bytes:
        raise RuntimeError("Captured body differs from submitted payload digest oracle")
    if not capture.finished:
        raise RuntimeError("Response did not finish")
    metrics["source"] = str(args.source)
    metrics["source_sha256"] = hashlib.sha256(args.source.read_bytes()).hexdigest()
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(digest.hexdigest())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--chunks", type=int, default=128)
    parser.add_argument("--chunk-bytes", type=int, default=65536)
    parser.add_argument("--reads", type=int, default=8)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    if min(args.chunks, args.chunk_bytes, args.reads) < 1:
        parser.error("workload sizes must be positive")
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from wreath import _asgi_state

    args.source = Path(_asgi_state.__file__).resolve()
    if not args.source.is_relative_to(root):
        raise RuntimeError("Response capture source outside requested root")
    asyncio.run(measure(args))


if __name__ == "__main__":
    main()
