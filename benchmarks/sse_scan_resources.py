from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import sys
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
    from wreath._agents import backplanes

    event = {"type": "response.output_text.delta", "delta": "a" * args.text_size}
    encoded = json.dumps(event, separators=(",", ":")).encode()
    body = (b"data: " + encoded + b"\n\n") * args.events
    chunk_size = len(body) if args.chunk_size == 0 else args.chunk_size
    scanned = 0
    searches = 0

    class Buffer(bytearray):
        def find(self, sub: bytes, start: int = 0, end: int | None = None) -> int:
            nonlocal scanned, searches
            stop = len(self) if end is None else end
            found = super().find(sub, start, stop)
            scanned += (stop if found < 0 else found + len(sub)) - start
            searches += 1
            return found

    if args.count:
        backplanes.bytearray = Buffer

    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(body), chunk_size):
            yield body[start : start + chunk_size]

    gc.collect()
    before = resident_bytes()
    start = process_time_ns()
    actual = [
        item
        async for item in backplanes._json_sse(chunks(), maximum=len(body), provider="benchmark")
    ]
    metrics = {"workload_cpu_ns": process_time_ns() - start}
    metrics.update(resident_bytes())
    metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
    if actual != [json.loads(encoded)] * args.events:
        raise RuntimeError("SSE output differs from stdlib JSON event oracle")
    if args.count:
        if searches <= 0 or scanned < len(body):
            raise RuntimeError("Scan instrumentation did not observe all SSE bytes")
        metrics["scanned_bytes"] = scanned
        metrics["search_calls"] = searches
    metrics["source"] = str(args.source)
    metrics["source_sha256"] = hashlib.sha256(args.source.read_bytes()).hexdigest()
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(
        json.dumps(
            {"events": len(actual), "sha256": hashlib.sha256(encoded).hexdigest()}, sort_keys=True
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--text-size", type=int, default=1024)
    parser.add_argument("--events", type=int, default=100)
    parser.add_argument("--chunk-size", type=int, default=16)
    parser.add_argument("--count", action="store_true")
    args = parser.parse_args()
    if args.text_size <= 0 or args.events <= 0 or args.chunk_size < 0:
        parser.error("--text-size and --events must be positive; --chunk-size must be nonnegative")
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from wreath._agents import backplanes

    args.source = Path(backplanes.__file__).resolve()
    if not args.source.is_relative_to(root):
        raise RuntimeError(f"SSE source {args.source} is outside {root}")
    asyncio.run(measure(args))


if __name__ == "__main__":
    main()
