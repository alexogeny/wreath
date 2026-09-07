from __future__ import annotations

import argparse
import gc
import hashlib
import io
import json
import struct
import sys
import tracemalloc
import uuid
import zlib
from pathlib import Path
from time import process_time_ns


def resident_bytes():
    values = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        name, _, value = line.partition(":")
        if name in {"Rss", "Pss"}:
            values[name.lower() + "_bytes"] = int(value.split()[0]) * 1024
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--routes", type=int, default=10000)
    parser.add_argument("--iterations", type=int, default=16)
    parser.add_argument("--hash-only", action="store_true")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--count", action="store_true")
    args = parser.parse_args()
    if args.routes < 0 or args.iterations < 1:
        parser.error("routes must be nonnegative and iterations positive")
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from wreath import _flight_schema, _recording_format
    from wreath._flight_schema import SCHEMA_VERSION, MetadataImage, RouteMeta

    sources = [Path(module.__file__).resolve() for module in (_flight_schema, _recording_format)]
    if any(not source.is_relative_to(root) for source in sources):
        raise RuntimeError("Recording source outside requested root")
    image = MetadataImage(
        version=1,
        routes=tuple(
            RouteMeta(index, "GET", f"/route/{index}", f"route_{index}", 0, (), (), (), 0, "python")
            for index in range(args.routes)
        ),
        plans=(),
        dependencies=(),
        middleware=(),
        auth_policies=(),
        serializers=(),
        validators=(),
        limits=(),
        clients=(),
        databases=(),
        models=(),
    )
    canonical = image.canonical_bytes()
    digest = hashlib.blake2b(canonical, digest_size=32).digest()[:16]
    recording_id = uuid.UUID(int=42)
    _recording_format.time.time_ns = lambda: 123456
    _recording_format.time.monotonic_ns = lambda: 789
    _recording_format.uuid.uuid4 = lambda: recording_id
    _recording_format._build_id = lambda: 99
    header = struct.pack(
        "<4sBBH16s16sQQQQQ",
        b"WFR1",
        1,
        SCHEMA_VERSION,
        0,
        digest,
        recording_id.bytes,
        123456,
        789,
        123456,
        99,
        0,
    )
    footer = struct.pack("<QQQQQ", 2, 0, 0, 0, 0)
    expected = (
        header
        + struct.pack("<4sII", b"META", len(canonical), zlib.crc32(canonical))
        + canonical
        + struct.pack("<4sII", b"FOOT", len(footer), zlib.crc32(footer))
        + footer
    )
    calls = 0
    original = MetadataImage.canonical_bytes
    if args.count:

        def counted(self):
            nonlocal calls
            calls += 1
            return original(self)

        MetadataImage.canonical_bytes = counted
    gc.collect()
    before = resident_bytes()
    if args.trace:
        tracemalloc.start()
    start = process_time_ns()
    for _ in range(args.iterations):
        if args.hash_only:
            result = image.image_hash_short()
        else:
            output = io.BytesIO()
            writer = _recording_format.WFR1Writer(output, image)
            writer.close()
            result = output.getvalue()
    metrics = {"workload_cpu_ns": process_time_ns() - start}
    if args.trace:
        metrics["retained_bytes"], metrics["peak_bytes"] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    metrics.update(resident_bytes())
    metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
    if result != (digest if args.hash_only else expected):
        raise RuntimeError("Recording differs from independent hash/framing/CRC oracle")
    metrics["canonical_calls"] = calls
    metrics["sources"] = {
        str(source): hashlib.sha256(source.read_bytes()).hexdigest() for source in sources
    }
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(hashlib.sha256(result).hexdigest())


if __name__ == "__main__":
    main()
