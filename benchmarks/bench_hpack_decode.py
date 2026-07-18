"""Whole-protocol HPACK decode benchmark with retained raw trials.

The timed region feeds complete HEADERS frames into the native HTTP/2 protocol.
It therefore includes frame dispatch and stream creation; compare only equivalent
runs and establish an A/A noise floor before attributing a decoder delta.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from tests.http2 import support
from tests.http2.conftest import H2Driver


async def _app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


async def _measure(block: bytes, warmup: int, trials: int, iterations: int) -> dict[str, Any]:
    driver = H2Driver(_app)
    await driver.preface()
    stream_id = 1

    async def batch(count: int) -> float:
        nonlocal stream_id
        started = time.perf_counter_ns()
        for _ in range(count):
            frame = support.encode_frame(
                support.HEADERS,
                support.FLAG_END_HEADERS | support.FLAG_END_STREAM,
                stream_id,
                block,
            )
            driver.feed(frame)
            stream_id += 2
        elapsed = (time.perf_counter_ns() - started) / 1e9
        await driver.settle()
        return elapsed / count

    for _ in range(warmup):
        await batch(iterations)
    raw = [await batch(iterations) for _ in range(trials)]
    errors = int(driver.transport.aborted)
    driver.close()
    return {
        "raw_seconds": raw,
        "median_seconds": statistics.median(raw),
        "p95_seconds": _percentile(raw, 0.95),
        "errors": errors,
        "compressed_bytes": len(block),
        "iterations_per_trial": iterations,
    }


async def run(warmup: int, trials: int) -> dict[str, Any]:
    payloads = {
        "common-path": b"/api/v1/items/42",
        "short-ascii": b"wreath-hpack",
        "cookie": b"session=" + b"abcdef0123456789" * 16,
        "1k-ascii": (b"common-value-0123456789" * 64)[:1024],
        "16k-ascii": (b"common-value-0123456789" * 1024)[: 16 * 1024],
        "mixed-codes": bytes(range(32, 127)) * 8,
    }
    cases: dict[str, Any] = {}
    for name, value in payloads.items():
        encoder = support.HpackEncoder()
        headers = support.request_headers(extra=[(b"x-benchmark", value)])
        block = encoder.encode(headers, huffman=True, index=False)
        iterations = 16 if len(value) >= 16 * 1024 else 64
        cases[name] = await _measure(block, warmup, trials, iterations)

    bad = support.encode_integer(1, 7, 0x80) + bytes([0x00])
    malformed = bytes([0x00]) + support.encode_string(b":method-x") + bad
    driver = H2Driver(_app)
    await driver.preface()
    driver.feed(
        support.encode_frame(
            support.HEADERS,
            support.FLAG_END_HEADERS | support.FLAG_END_STREAM,
            1,
            malformed,
        )
    )
    await driver.settle()
    malformed_errors = sum(
        frame.type == support.GOAWAY
        and support.parse_goaway(frame.payload)[1] == support.COMPRESSION_ERROR
        for frame in driver.frames()
    )
    driver.close()

    return {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "executable": sys.executable,
            "warmup": warmup,
            "trials": trials,
            "compiler": platform.python_compiler(),
            "native_server": getattr(
                sys.modules.get(driver.protocol.__class__.__module__), "__file__", None
            ),
        },
        "cases": cases,
        "malformed_compression_errors": malformed_errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--trials", type=int, default=9)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(args.warmup, args.trials))
    rendered = json.dumps(result, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
