from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib
import json
import sys
import time
import tracemalloc
from pathlib import Path


def resident_bytes() -> dict[str, int]:
    fields = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        name, _, value = line.partition(":")
        if name in {"Rss", "Pss"}:
            fields[name.lower() + "_bytes"] = int(value.split()[0]) * 1024
    return fields


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure gzip output reservations and fixed decode CPU work."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    core = importlib.import_module("wreath._native._core")
    if not Path(core.__file__).is_relative_to(args.source.resolve()):
        raise RuntimeError(f"loaded native extension outside source: {core.__file__}")
    workspace = core.gzip_decoder_new()
    metrics = {}
    outputs = []
    for name, size, iterations in (
        ("small", 1100, 30000),
        ("medium", 65536, 3000),
        ("large", 1048576, 200),
    ):
        payload = (b"hello world" * ((size + 10) // 11))[:size]
        encoded = gzip.compress(payload, mtime=0)
        for bound, maximum in (("tight", size), ("broad", 64 << 20)):
            label = f"{name}_{bound}"
            for _ in range(20):
                core.gzip_decompress_with(workspace, encoded, maximum, "unknown")
            tracemalloc.start()
            try:
                decoded = core.gzip_decompress_with(workspace, encoded, maximum, "unknown")
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            if decoded != payload:
                raise RuntimeError(f"incorrect gzip output for {label}")
            metrics[f"{label}_peak_bytes"] = peak
            started = time.process_time_ns()
            for _ in range(iterations):
                decoded = core.gzip_decompress_with(workspace, encoded, maximum, "unknown")
            metrics[f"{label}_cpu_ns"] = time.process_time_ns() - started
            if decoded != payload:
                raise RuntimeError(f"incorrect gzip output for {label}")
            outputs.append((label, iterations, size, hashlib.sha256(decoded).hexdigest()))
    metrics.update(resident_bytes())
    metrics["native_sha256"] = hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest()
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps(outputs))


if __name__ == "__main__":
    main()
