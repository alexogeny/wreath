"""Measure valid unsafe-request CSRF validation without profiler overhead."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from wreath.middleware import CSRFMiddleware, csrf_token
from wreath.request import Request


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _request(method: str, headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": "/",
            "query_string": b"",
            "headers": headers,
        },
        _receive,
    )


async def _sample(
    middleware: CSRFMiddleware,
    headers: list[tuple[bytes, bytes]],
    iterations: int,
) -> float:
    started = perf_counter_ns()
    for _ in range(iterations):
        result = await middleware.before(_request("POST", headers))
        if result is not None:
            raise AssertionError("valid CSRF token was rejected")
    return (perf_counter_ns() - started) / iterations


async def run(iterations: int, warmup: int, trials: int) -> dict[str, Any]:
    middleware = CSRFMiddleware("s" * 32)
    safe = _request("GET", [(b"host", b"example.test")])
    await middleware.before(safe)
    token = csrf_token(safe)
    headers = [
        (b"host", b"example.test"),
        (b"origin", b"https://example.test"),
        (b"cookie", f"wreath_csrf={token}".encode()),
        (b"x-csrf-token", token.encode()),
    ]
    if warmup:
        await _sample(middleware, headers, warmup)
    raw = [await _sample(middleware, headers, iterations) for _ in range(trials)]
    return {
        "tool": "benchmarks.bench_csrf",
        "schema_version": 1,
        "python": sys.version,
        "platform": platform.platform(),
        "scenario": "valid-origin",
        "iterations_per_trial": iterations,
        "warmup_iterations": warmup,
        "trials": trials,
        "raw_ns_per_validation": raw,
        "median_ns_per_validation": statistics.median(raw),
        "min_ns_per_validation": min(raw),
        "max_ns_per_validation": max(raw),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=20_000)
    parser.add_argument("--warmup", type=int, default=2_000)
    parser.add_argument("--trials", type=int, default=9)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(args.iterations, args.warmup, args.trials))
    text = json.dumps(result, indent=2)
    if args.output is None:
        print(text)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
