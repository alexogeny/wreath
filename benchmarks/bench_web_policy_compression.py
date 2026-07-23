"""Focused browser-policy and gzip benchmarks with retained raw trials."""

from __future__ import annotations

import argparse
import gzip
import importlib
import json
import os
import platform
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wreath._pure import compression as pure_compression
from wreath._pure import webpolicy as pure_policy

try:
    native_core = importlib.import_module("wreath._native._core")
except ImportError:
    native_core = None
try:
    native_compression = importlib.import_module("wreath._native._compression")
except ImportError:
    native_compression = None


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def _measure(
    operation: Callable[[], Any], warmup: int, trials: int, iterations: int
) -> dict[str, Any]:
    for _ in range(warmup):
        for _iteration in range(iterations):
            operation()
    raw: list[float] = []
    result: Any = None
    for _ in range(trials):
        started = time.perf_counter_ns()
        for _iteration in range(iterations):
            result = operation()
        elapsed = (time.perf_counter_ns() - started) / 1e9
        raw.append(elapsed / iterations)
    record: dict[str, Any] = {
        "raw_seconds": raw,
        "median_seconds": statistics.median(raw),
        "p95_seconds": _percentile(raw, 0.95),
    }
    if isinstance(result, bytes):
        record["output_bytes"] = len(result)
    return record


def run(warmup: int, trials: int) -> dict[str, Any]:
    text_1k = (b'{"message":"wreath browser policy"}' * 40)[:1024]
    text_16k = (text_1k * 16)[: 16 * 1024]
    text_1m = (text_16k * 64)[: 1024 * 1024]
    random_16k = os.urandom(16 * 1024)
    cases: dict[str, Any] = {}

    policy_backends = {"pure": pure_policy}
    if native_core is not None and hasattr(native_core, "select_content_encoding"):
        policy_backends["native"] = native_core
    for name, backend in policy_backends.items():
        cases[f"accept-encoding-selection:{name}"] = _measure(
            lambda b=backend: b.select_content_encoding(b"br;q=1, gzip;q=0.8, *;q=0.1"),
            warmup,
            trials,
            100_000,
        )
        cases[f"origin-match:{name}"] = _measure(
            lambda b=backend: b.origin_matches(
                b"https://api.example.test:443/", (b"https://api.example.test",)
            ),
            warmup,
            trials,
            50_000,
        )
        for header_count in (64, 128, 256, 512):
            existing = [(f"x-existing-{i}".encode(), b"value") for i in range(header_count)]
            additions = tuple(
                (f"x-added-{i}".encode(), b"first") for i in range(header_count)
            )
            additions += tuple((key.upper(), b"duplicate") for key, _ in additions)
            record = _measure(
                lambda b=backend, h=existing, a=additions: b.append_missing_headers(h.copy(), a),
                warmup,
                trials,
                max(50, 20_000 // header_count),
            )
            record["existing_headers"] = header_count
            record["additions"] = len(additions)
            record["duplicate_additions"] = header_count
            cases[f"append-missing-headers:{header_count}:{name}"] = record

    compressors: dict[str, Callable[[bytes, int], bytes]] = {
        "pure": pure_compression.gzip_compress,
        "stdlib": lambda data, level: gzip.compress(data, compresslevel=level, mtime=0),
    }
    if native_compression is not None:
        compressors["native"] = native_compression.gzip_compress
    for backend_name, compressor in compressors.items():
        for case_name, payload, iterations in (
            ("compress-1k-text", text_1k, 2_000),
            ("compress-16k-text", text_16k, 500),
            ("compress-1m-text", text_1m, 10),
            ("compress-16k-incompressible", random_16k, 200),
        ):
            for level in (1, 5, 9):
                record = _measure(
                    lambda c=compressor, p=payload, level=level: c(p, level),
                    warmup,
                    trials,
                    iterations,
                )
                record["input_bytes"] = len(payload)
                record["level"] = level
                record["ratio"] = record["output_bytes"] / len(payload)
                cases[f"{case_name}:{backend_name}:level-{level}"] = record

    return {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "executable": sys.executable,
            "warmup": warmup,
            "trials": trials,
            "native_core": getattr(native_core, "__file__", None),
            "native_compression": getattr(native_compression, "__file__", None),
            "zlib_compile": getattr(native_compression, "ZLIB_VERSION", None),
            "zlib_runtime": getattr(native_compression, "ZLIB_RUNTIME_VERSION", None),
        },
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--trials", type=int, default=9)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.warmup, args.trials)
    rendered = json.dumps(result, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
