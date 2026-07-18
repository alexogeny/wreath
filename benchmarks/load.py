"""A dependency-free HTTP/1.1 load generator for development feedback.

This client is intentionally not presented as an independent, publication-grade benchmark tool.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from wreath._websocket import build_frame as _ws_build_frame
from wreath._websocket import parse_frame as _ws_parse_frame

#: Identity of this built-in, dependency-free HTTP/1.1 development generator.
#: It is deliberately not a publication-grade cross-protocol tool; results from
#: it must be labeled and never ranked against a different generator's rows.
LOAD_GENERATOR = "builtin"
LOAD_GENERATOR_VERSION = "wreath-dev-1"


@dataclass(slots=True)
class Result:
    requests: int
    errors: int
    duration_seconds: float
    requests_per_second: float
    latency_ms_median: float
    latency_ms_p95: float
    latency_ms_p99: float


async def _read_response(reader: asyncio.StreamReader) -> None:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_parts = lines[0].split(b" ", 2)
    if len(status_parts) < 2:
        raise RuntimeError("server returned a malformed HTTP status line")
    status = int(status_parts[1])
    if status >= 500:
        raise RuntimeError(f"server returned HTTP {status}")

    content_length = 0
    chunked = False
    for line in lines[1:]:
        key, separator, value = line.partition(b":")
        if not separator:
            continue
        key = key.lower()
        if key == b"content-length":
            content_length = int(value.strip())
        elif key == b"transfer-encoding" and b"chunked" in value.lower():
            chunked = True
    if chunked:
        while True:
            size_line = await reader.readuntil(b"\r\n")
            size = int(size_line[:-2].split(b";", 1)[0], 16)
            if size == 0:
                while await reader.readuntil(b"\r\n") != b"\r\n":
                    pass
                return
            await reader.readexactly(size + 2)
    elif content_length:
        await reader.readexactly(content_length)


def _build_request(
    host: str,
    port: int,
    path: str,
    method: str,
    body: bytes = b"",
    headers: tuple[tuple[str, str], ...] = (),
) -> bytes:
    lines = [
        f"{method} {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Accept: */*",
        "Connection: keep-alive",
        *(f"{name}: {value}" for name, value in headers),
    ]
    if body and not any(name.lower() == "content-length" for name, _ in headers):
        lines.append(f"Content-Length: {len(body)}")
    return "\r\n".join(lines).encode("ascii") + b"\r\n\r\n" + body


def _build_ws_upgrade(host: str, port: int, path: str) -> bytes:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    return (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")


async def _ws_worker(
    host: str,
    port: int,
    path: str,
    body: bytes,
    deadline: float | None,
    samples: list[float] | None,
    request_limit: int | None = None,
    progress_counts: list[int] | None = None,
) -> tuple[int, int]:
    """One connection: upgrade once, then measured text-echo roundtrips."""
    frame = _ws_build_frame(0x1, body or b"ping", True, os.urandom(4))
    completed = 0
    errors = 0
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    buffer = b""

    while (deadline is None or time.perf_counter() < deadline) and (
        request_limit is None or completed + errors < request_limit
    ):
        started = time.perf_counter_ns()
        try:
            if writer is None or writer.is_closing():
                reader, writer = await asyncio.open_connection(host, port)
                writer.write(_build_ws_upgrade(host, port, path))
                await writer.drain()
                head = await reader.readuntil(b"\r\n\r\n")
                if not head.startswith(b"HTTP/1.1 101"):
                    raise RuntimeError("websocket upgrade refused")
                buffer = b""
            writer.write(frame)
            await writer.drain()
            assert reader is not None
            while True:
                parsed = _ws_parse_frame(buffer)
                if parsed is not None:
                    break
                chunk = await reader.read(65536)
                if not chunk:
                    raise RuntimeError("connection closed mid-roundtrip")
                buffer += chunk
            _, opcode, _, consumed = parsed
            buffer = buffer[consumed:]
            if opcode != 0x1:
                raise RuntimeError(f"unexpected reply opcode {opcode}")
        except (OSError, asyncio.IncompleteReadError, RuntimeError, ValueError):
            errors += 1
            if progress_counts is not None:
                progress_counts[1] += 1
            if writer is not None:
                writer.close()
            reader = None
            writer = None
            continue
        completed += 1
        if progress_counts is not None:
            progress_counts[0] += 1
        if samples is not None:
            samples.append((time.perf_counter_ns() - started) / 1_000_000)

    if writer is not None:
        writer.close()
        await writer.wait_closed()
    return completed, errors


async def _worker(
    host: str,
    port: int,
    path: str,
    method: str,
    body: bytes,
    headers: tuple[tuple[str, str], ...],
    deadline: float | None,
    samples: list[float] | None,
    request_limit: int | None = None,
    progress_counts: list[int] | None = None,
) -> tuple[int, int]:
    request = _build_request(host, port, path, method, body, headers)
    completed = 0
    errors = 0
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None

    while (deadline is None or time.perf_counter() < deadline) and (
        request_limit is None or completed + errors < request_limit
    ):
        started = time.perf_counter_ns()
        try:
            if writer is None or writer.is_closing():
                reader, writer = await asyncio.open_connection(host, port)
            writer.write(request)
            await writer.drain()
            assert reader is not None
            await _read_response(reader)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, RuntimeError):
            errors += 1
            if progress_counts is not None:
                progress_counts[1] += 1
            if writer is not None:
                writer.close()
            reader = None
            writer = None
            continue
        completed += 1
        if progress_counts is not None:
            progress_counts[0] += 1
        if samples is not None:
            samples.append((time.perf_counter_ns() - started) / 1_000_000)

    if writer is not None:
        writer.close()
        await writer.wait_closed()
    return completed, errors


async def _phase(
    host: str,
    port: int,
    path: str,
    method: str,
    body: bytes,
    headers: tuple[tuple[str, str], ...],
    duration: float | None,
    concurrency: int,
    *,
    record: bool,
    total_requests: int | None = None,
    progress: Callable[[int, int, float], None] | None = None,
    progress_interval: float = 1.0,
    websocket: bool = False,
) -> tuple[int, int, list[float]]:
    samples: list[float] = []
    progress_counts = [0, 0]
    deadline = time.perf_counter() + duration if duration is not None else None
    base, remainder = divmod(total_requests or 0, concurrency)
    workers = [
        asyncio.create_task(
            _ws_worker(
                host,
                port,
                path,
                body,
                deadline,
                samples if record else None,
                None if total_requests is None else base + (worker < remainder),
                progress_counts,
            )
            if websocket
            else _worker(
                host,
                port,
                path,
                method,
                body,
                headers,
                deadline,
                samples if record else None,
                None if total_requests is None else base + (worker < remainder),
                progress_counts,
            )
        )
        for worker in range(concurrency)
    ]
    started = time.perf_counter()
    pending = set(workers)
    while pending:
        _, pending = await asyncio.wait(pending, timeout=progress_interval)
        if progress is not None and total_requests is not None:
            progress(sum(progress_counts), total_requests, time.perf_counter() - started)
    results = [worker.result() for worker in workers]
    return sum(item[0] for item in results), sum(item[1] for item in results), samples


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    index = math.ceil((len(sorted_values) - 1) * percentile)
    return sorted_values[index]


async def measure(
    host: str,
    port: int,
    path: str,
    duration: float,
    warmup: float,
    concurrency: int,
    requests: int | None = 100,
    warmup_requests: int = 10,
    progress: Callable[[int, int, float], None] | None = None,
    method: str = "GET",
    body: bytes = b"",
    headers: tuple[tuple[str, str], ...] = (),
    websocket: bool = False,
) -> Result:
    if warmup_requests > 0:
        await _phase(
            host,
            port,
            path,
            method,
            body,
            headers,
            None,
            concurrency,
            record=False,
            total_requests=warmup_requests,
            websocket=websocket,
        )
    elif warmup > 0:
        await _phase(
            host,
            port,
            path,
            method,
            body,
            headers,
            warmup,
            concurrency,
            record=False,
            websocket=websocket,
        )
    started = time.perf_counter()
    completed, errors, samples = await _phase(
        host,
        port,
        path,
        method,
        body,
        headers,
        None if requests is not None else duration,
        concurrency,
        record=True,
        total_requests=requests,
        progress=progress,
        websocket=websocket,
    )
    elapsed = time.perf_counter() - started
    samples.sort()
    return Result(
        requests=completed,
        errors=errors,
        duration_seconds=elapsed,
        requests_per_second=completed / elapsed,
        latency_ms_median=_percentile(samples, 0.50),
        latency_ms_p95=_percentile(samples, 0.95),
        latency_ms_p99=_percentile(samples, 0.99),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/")
    parser.add_argument("--method", default="GET")
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--warmup", type=float, default=0.0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--warmup-requests", type=int, default=10)
    args = parser.parse_args()
    result = asyncio.run(measure(**vars(args)))
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
