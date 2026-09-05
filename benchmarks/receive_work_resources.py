from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import struct
import sys
import time
from pathlib import Path
from typing import Any


def feed(protocol: Any, wire: bytes, fragment: int) -> None:
    offset = 0
    while offset < len(wire):
        view = protocol.get_buffer(-1)
        count = min(fragment, len(view), len(wire) - offset)
        view[:count] = wire[offset : offset + count]
        view.release()
        protocol.buffer_updated(count)
        offset += count


async def measure(source: Path) -> tuple[dict[str, int], list[tuple[str, int, int]]]:
    sys.path.insert(0, str(source))
    native = importlib.import_module("wreath._native._postgres")
    if native.__file__ is None or not Path(native.__file__).is_relative_to(source):
        raise RuntimeError("loaded PostgreSQL extension outside requested source")
    metrics = {}
    outputs = []
    for size, repetitions in ((32, 20000), (65536, 100), (1 << 20, 20), (8 << 20, 8)):
        payload = (bytes(range(256)) * ((size + 255) // 256))[:size]
        wire = b"d" + struct.pack("!I", len(payload) + 4) + payload
        for fragment in (256, 65536):
            name = f"body_{size}_fragment_{fragment}"
            protocol = native.BufferedProtocol()
            for _ in range(2):
                feed(protocol, wire, fragment)
                if await protocol.read_message() != (b"d", payload):
                    raise RuntimeError("incorrect warmup CopyData payload")
            started = time.process_time_ns()
            for _ in range(repetitions):
                feed(protocol, wire, fragment)
                result = await protocol.read_message()
                if result != (b"d", payload):
                    raise RuntimeError("incorrect CopyData payload")
            metrics[f"{name}_cpu_ns"] = time.process_time_ns() - started
            stats = protocol._receive_stats()
            if stats["active_slabs"] != 0:
                raise RuntimeError("fully consumed message retained active slabs")
            outputs.append((name, len(result[1]), stats["chained_messages"]))
    return metrics, outputs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure fragmented valid PostgreSQL receive work."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    args = parser.parse_args()
    metrics, outputs = asyncio.run(measure(args.source.resolve()))
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps(outputs))


if __name__ == "__main__":
    main()
