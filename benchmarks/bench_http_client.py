"""Decompose outbound HTTP codec and managed keep-alive costs."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
from collections.abc import Callable
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from wreath._client_codec import parse_response_head as selected_parse_response_head
from wreath._client_codec import response_framing
from wreath._client_codec import serialize_request as selected_serialize_request
from wreath._native import _core
from wreath.http_client import DestinationPolicy, HTTPClient

# The two tiers, both C. `_client` is the dedicated outbound extension and wins
# when built; `_core`'s inbound parser covers the same grammar and is the
# fallback. `_client_codec` picks the first, and this measures what that pick
# is worth by driving `_core` directly as the other arm.
core_parse_response_head = _core.http_parse_response


def core_serialize_request(method, target, host, *, headers=(), body=b""):
    return _core.http_serialize_request(method, target, host, tuple(headers), body)

_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"content-type: application/json\r\n"
    b"content-length: 2\r\n"
    b"x-request-id: benchmark\r\n\r\n{}"
)


def _measure(function: Callable[[], Any], iterations: int, trials: int) -> list[float]:
    samples: list[float] = []
    for _ in range(trials):
        start = perf_counter_ns()
        for _iteration in range(iterations):
            function()
        samples.append((perf_counter_ns() - start) / iterations)
    return samples


def _summary(samples: list[float]) -> dict[str, Any]:
    ordered = sorted(samples)
    return {
        "median_ns": statistics.median(samples),
        "p95_ns": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "raw_ns": samples,
    }


async def _loopback(iterations: int) -> float:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            for _ in range(iterations):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(_RESPONSE)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = HTTPClient(
        "benchmark",
        base_url=f"http://127.0.0.1:{port}",
        destination=DestinationPolicy(allow_private=True, allow_loopback=True),
    )
    try:
        await client.start()
        start = perf_counter_ns()
        for _ in range(iterations):
            response = await client.get("/")
            if response.body != b"{}":
                raise RuntimeError("loopback response integrity failure")
        elapsed = perf_counter_ns() - start
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
    return elapsed / iterations


async def run(iterations: int, trials: int, loopback_iterations: int) -> dict[str, Any]:
    request_args = (
        "POST",
        b"/events",
        b"partner.example",
    )
    request_kwargs = {
        "headers": ((b"content-type", b"application/json"),),
        "body": b"{}",
    }
    core_request = lambda: core_serialize_request(  # noqa: E731
        *request_args, **request_kwargs
    )
    selected_request = lambda: selected_serialize_request(  # noqa: E731
        *request_args, **request_kwargs
    )
    core = lambda: core_parse_response_head(_RESPONSE)  # noqa: E731
    selected = lambda: selected_parse_response_head(_RESPONSE)  # noqa: E731
    framing_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", b"2"),
        (b"x-request-id", b"benchmark"),
    ]
    framing = lambda: response_framing(  # noqa: E731
        "GET", 200, framing_headers
    )
    loopback_samples = [
        await _loopback(loopback_iterations)
        for _ in range(trials)
    ]
    # Identical answers are the precondition: two tiers producing different
    # bytes would make the faster one a measurement of a different job.
    if core_parse_response_head(_RESPONSE) != selected_parse_response_head(_RESPONSE):
        raise RuntimeError("the two response parsers disagree")
    if core_request() != selected_request():
        raise RuntimeError("the two request serializers disagree")
    return {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "iterations": iterations,
            "trials": trials,
            "loopback_iterations": loopback_iterations,
            "response_bytes": len(_RESPONSE),
            "selected_parser": selected_parse_response_head.__module__,
        },
        "results": {
            "serialize_request_core": _summary(
                _measure(core_request, iterations, trials)
            ),
            "serialize_request_selected": _summary(
                _measure(selected_request, iterations, trials)
            ),
            "parse_response_core": _summary(_measure(core, iterations, trials)),
            "parse_response_selected": _summary(_measure(selected, iterations, trials)),
            "response_framing_selected": _summary(
                _measure(framing, iterations, trials)
            ),
            "managed_keepalive_loopback": _summary(loopback_samples),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=50_000)
    parser.add_argument("--trials", type=int, default=7)
    parser.add_argument("--loopback-iterations", type=int, default=1_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.iterations, args.trials, args.loopback_iterations) <= 0:
        parser.error("iteration and trial counts must be positive")
    result = asyncio.run(run(args.iterations, args.trials, args.loopback_iterations))
    print(json.dumps(result, indent=2))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
