"""Microbenchmark Wreath's native HTTP/1 request-to-handler bridge.

This intentionally uses an in-process transport: it measures parser, request
scope/context construction, routing, handler invocation, and response emission
without kernel/network noise. Raw trial timings are printed for comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import platform
import statistics
import sys
import time
from typing import Any

from wreath import Response, Wreath
from wreath.server import ServerConfig

_native_server: Any = importlib.import_module("wreath._native._server")
HttpProtocol = _native_server.HttpProtocol

_REQUEST = b"GET /plain?x=1 HTTP/1.1\r\nhost: localhost\r\nuser-agent: wreath-bench\r\n\r\n"
_BODY_SIZES = (0, 2, 1024, 16 * 1024, 64 * 1024)


class CountingTransport(asyncio.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.bytes_written = 0
        self.closed = False

    def write(self, data: Any) -> None:
        self.bytes_written += len(data)

    def writelines(self, list_of_data: Any) -> None:
        for item in list_of_data:
            self.bytes_written += len(item)

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "sockname":
            return ("127.0.0.1", 8000)
        if name == "peername":
            return ("127.0.0.1", 50000)
        return default


async def _trial(requests: int, body: bytes) -> tuple[float, int]:
    app = Wreath()
    response = Response(body)
    completed = 0
    done = asyncio.Event()

    @app.get("/plain")
    async def plain(request: Any) -> Response:
        nonlocal completed
        completed += 1
        if completed == requests:
            done.set()
        return response

    loop = asyncio.get_running_loop()
    protocol = HttpProtocol(app, ServerConfig(), loop, set())
    transport = CountingTransport()
    protocol.connection_made(transport)
    started = time.perf_counter()
    for _ in range(requests):
        protocol.data_received(_REQUEST)
    await done.wait()
    elapsed = time.perf_counter() - started
    protocol.connection_lost(None)
    return elapsed, transport.bytes_written


async def run(
    requests: int, warmup: int, trials: int, body_sizes: tuple[int, ...] = _BODY_SIZES
) -> dict[str, Any]:
    cases: dict[str, Any] = {}
    for body_size in body_sizes:
        body = b"x" * body_size
        for _ in range(warmup):
            await _trial(requests, body)
        samples: list[float] = []
        written = 0
        for _ in range(trials):
            elapsed, written = await _trial(requests, body)
            samples.append(elapsed)
        median = statistics.median(samples)
        cases[str(body_size)] = {
            "body_bytes": body_size,
            "response_bytes_last_trial": written,
            "expected_write_calls": requests if body_size <= 16 * 1024 else 0,
            "expected_writelines_calls": requests if body_size > 16 * 1024 else 0,
            "median_seconds": median,
            "requests_per_second": requests / median,
            "raw_seconds": samples,
        }
    return {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "requests_per_trial": requests,
            "warmup": warmup,
            "trials": trials,
            "request_bytes": len(_REQUEST),
            "body_sizes": list(body_sizes),
        },
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=20_000)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--trials", type=int, default=9)
    parser.add_argument("--body-size", type=int, nargs="+", default=list(_BODY_SIZES))
    args = parser.parse_args()
    body_sizes = tuple(dict.fromkeys(args.body_size))
    print(
        json.dumps(
            asyncio.run(run(args.requests, args.warmup, args.trials, body_sizes)),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
