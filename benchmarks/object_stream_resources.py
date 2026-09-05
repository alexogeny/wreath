from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import sys
import tracemalloc
from collections.abc import AsyncIterator
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
    from wreath.objects import MemoryObjectStore, ObjectStat

    source = args.source
    metrics = {}
    chunk = bytes(range(256)) * (args.chunk_size // 256) + bytes(range(args.chunk_size % 256))
    count, tail = divmod(args.size, args.chunk_size)
    expected_etag = hashlib.md5(usedforsecurity=False)
    for _ in range(count):
        expected_etag.update(chunk)
    expected_etag.update(chunk[:tail])

    class Store(MemoryObjectStore):
        async def write(
            self, key: str, data: bytes | bytearray | memoryview, *, content_type: str | None = None
        ) -> ObjectStat:
            result = await super().write(key, data, content_type=content_type)
            metrics["workload_cpu_ns"] = process_time_ns() - start
            if args.trace:
                metrics["boundary_retained_bytes"], metrics["peak_bytes"] = (
                    tracemalloc.get_traced_memory()
                )
            metrics.update(resident_bytes())
            metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
            return result

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(count):
            yield chunk
        if tail:
            yield chunk[:tail]

    store = Store()
    gc.collect()
    before = resident_bytes()
    if args.trace:
        tracemalloc.start()
    start = process_time_ns()
    stat = await store.write_stream("payload", chunks(), content_type="application/octet-stream")
    if args.trace:
        tracemalloc.stop()
    data = await store.read("payload")
    if stat.size != args.size or stat.etag != expected_etag.hexdigest():
        raise RuntimeError("Stream metadata differs from the independent hashlib oracle")
    if stat.content_type != "application/octet-stream" or len(data) != args.size:
        raise RuntimeError("Stream metadata or payload length changed")
    view = memoryview(data)
    for index in range(count):
        if view[index * args.chunk_size : (index + 1) * args.chunk_size] != chunk:
            raise RuntimeError(f"Stream payload differs at chunk {index}")
    if tail and view[count * args.chunk_size :] != chunk[:tail]:
        raise RuntimeError("Stream payload tail differs")
    metrics["source"] = str(source)
    metrics["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps({"size": stat.size, "etag": stat.etag}, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--size", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--chunk-size", type=int, default=128 * 1024)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    if args.size <= 0 or args.chunk_size <= 0:
        parser.error("--size and --chunk-size must be positive")
    source_root = args.source_root.resolve()
    sys.path.insert(0, str(source_root))
    from wreath import objects

    args.source = Path(objects.__file__).resolve()
    if not args.source.is_relative_to(source_root):
        raise RuntimeError(f"Object store source {args.source} is outside {source_root}")
    asyncio.run(measure(args))


if __name__ == "__main__":
    main()
