from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any


def _memory() -> dict[str, int]:
    readings = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        parts = line.split()
        if parts[0] in {"Rss:", "Pss:"}:
            readings[parts[0][:-1].lower()] = int(parts[1]) * 1024
    if readings.keys() != {"rss", "pss"}:
        raise RuntimeError("resource footprint requires Linux smaps_rollup RSS and PSS")
    return readings


def _kv(options: argparse.Namespace) -> tuple[dict[str, int], dict[str, Any]]:
    from wreath.kv import KV

    case = options.scenario.removeprefix("kv-")
    if case in {"empty", "sparse", "dense"}:
        width = {"empty": 0, "sparse": 1, "dense": options.capacity}[case]
        stores = [KV(max_entries=options.capacity, ttl=options.ttl) for _ in range(options.count)]
        for table in stores:
            for key in range(width):
                table.set(key, key, now=0.0)
        gc.collect()
        resident = sum(table.count(now=0.0) for table in stores)
        if len(stores) != options.count or resident != options.count * width:
            raise RuntimeError("KV footprint workload did not retain the requested entries")
        return _memory(), {"stores": len(stores), "entries": resident}

    table = KV(max_entries=options.capacity, ttl=options.ttl)
    checksum = 0
    if case == "hit":
        table.set("key", 1, now=0.0)
        for _ in range(options.count):
            checksum += table.get("key", now=0.0)
    elif case == "miss":
        for _ in range(options.count):
            checksum += table.get("key", 1, now=0.0)
    else:
        for index in range(options.count):
            key = index if case == "evict" else index % options.capacity
            table.set(key, index, now=0.0)
        resident = min(options.count, options.capacity)
        checksum = sum(table.values(now=0.0))
        expected = resident * (2 * options.count - resident - 1) // 2
        if checksum != expected or table.count(now=0.0) != resident:
            raise RuntimeError("KV write workload retained incorrect values")
    if case in {"hit", "miss"} and checksum != options.count:
        raise RuntimeError("KV read workload returned incorrect values")
    gc.collect()
    return _memory(), {
        "checksum": checksum,
        "entries": table.count(now=0.0),
        "evictions": table.evictions,
    }


def _cached_handlers(options: argparse.Namespace) -> tuple[dict[str, int], dict[str, Any]]:
    from wreath.response_cache import cached

    async def handler(request: Any) -> str:
        return "cached response"

    handlers = [cached(ttl=60)(handler) for _ in range(options.count)]
    gc.collect()
    if len(handlers) != options.count or any(item.cache_store.count() for item in handlers):
        raise RuntimeError("cached-handler workload did not retain the requested empty stores")
    return _memory(), {"handlers": len(handlers), "entries": 0}


async def _asgi_cache(options: argparse.Namespace) -> tuple[dict[str, int], dict[str, Any]]:
    from wreath import Wreath
    from wreath.response_cache import cached
    from wreath.testing import TestClient

    app = Wreath()
    calls = 0

    @cached(ttl=60, max_entries=options.capacity)
    async def handler(request: Any) -> str:
        nonlocal calls
        calls += 1
        return request.query_string.decode("ascii")

    app.get("/value")(handler)
    async with TestClient(app) as client:
        for index in range(options.count):
            query = f"item={index}"
            response = await client.get(f"/value?{query}")
            if response.status != 200 or response.body != query.encode("ascii"):
                raise RuntimeError("ASGI cache workload returned an incorrect response")
        response = await client.get(f"/value?item={options.count - 1}")
        if response.status != 200 or response.body != f"item={options.count - 1}".encode("ascii"):
            raise RuntimeError("ASGI cache workload did not preserve the hot response")
    table = handler.cache_store
    if calls != options.count or table.evictions != max(0, options.count - options.capacity):
        raise RuntimeError("ASGI cache workload did not reach cold writes and capacity eviction")
    gc.collect()
    return _memory(), {"handler_calls": calls, "evictions": table.evictions, "hits": table.hits}


async def _body(options: argparse.Namespace) -> tuple[dict[str, int], dict[str, Any]]:
    from wreath.request import Request, RequestLimits
    from wreath.state import BODY_CHECK_SLOT

    class MeasuredRequest(Request):
        __slots__ = ("completion_memory",)

        def _check_body(self, body: bytes) -> None:
            super()._check_body(body)
            self.completion_memory = _memory()

    class MeasuredSignedRequest(MeasuredRequest):
        __slots__ = ()

        async def stream(self) -> AsyncIterator[bytes]:
            async for chunk in super().stream():
                self.completion_memory = _memory()
                yield chunk

        def _check_body(self, body: bytes) -> None:
            Request._check_body(self, body)

    if options.scenario == "body-memory":
        kind = MeasuredSignedRequest if options.signed_body else MeasuredRequest
    else:
        kind = Request
    limits = RequestLimits(max_body_bytes=options.size)
    expected_digest = hashlib.sha256()
    if options.signed_body:
        for offset in range(0, options.size, options.chunk_size):
            length = min(options.chunk_size, options.size - offset)
            expected_digest.update(bytes([offset // options.chunk_size % 251]) * length)
    checksum = hashlib.sha256()
    metrics = {}
    total_received = 0
    for iteration in range(options.count):
        received = 0

        async def receive() -> Any:
            nonlocal received, total_received
            length = min(options.chunk_size, options.size - received)
            chunk = bytes([received // options.chunk_size % 251]) * length
            received += length
            total_received += length
            if options.native_messages:
                return chunk, received < options.size, False
            return {"type": "http.request", "body": chunk, "more_body": received < options.size}

        request = kind({"type": "http"}, receive, limits=limits)
        if options.signed_body:
            setattr(request.state, BODY_CHECK_SLOT, ("sha-256", expected_digest.digest()))
        body = await request.body()
        if len(body) != options.size or received != options.size:
            raise RuntimeError("request-body workload did not consume the requested bytes")
        if await request.body() is not body:
            raise RuntimeError("request-body workload did not reuse its cached body")
        if iteration == options.count - 1:
            checksum.update(body)
        if isinstance(request, MeasuredRequest):
            metrics = {
                name: max(metrics.get(name, 0), value)
                for name, value in request.completion_memory.items()
            }
        del body, request
    if not metrics:
        gc.collect()
        metrics = _memory()
    return metrics, {"received_bytes": total_received, "sha256": checksum.hexdigest()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=(
            "kv-empty",
            "kv-sparse",
            "kv-dense",
            "kv-hit",
            "kv-miss",
            "kv-update",
            "kv-evict",
            "cached-handlers",
            "asgi-cache",
            "body-memory",
            "body-read",
        ),
        required=True,
    )
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--capacity", type=int, default=1024)
    parser.add_argument("--ttl", type=float)
    parser.add_argument("--size", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--chunk-size", type=int, default=64 * 1024)
    parser.add_argument("--native-messages", action="store_true")
    parser.add_argument("--signed-body", action="store_true")
    options = parser.parse_args(argv)
    if min(options.count, options.capacity, options.size, options.chunk_size) < 1:
        parser.error("count, capacity, size and chunk-size must be positive")
    if options.source_root is not None:
        options.source_root = options.source_root.resolve()
        if not (options.source_root / "wreath" / "__init__.py").is_file():
            parser.error("source-root must contain the wreath package")
        sys.path.insert(0, str(options.source_root))
    if options.scenario.startswith("kv-"):
        metrics, result = _kv(options)
    elif options.scenario == "cached-handlers":
        metrics, result = _cached_handlers(options)
    elif options.scenario == "asgi-cache":
        metrics, result = asyncio.run(_asgi_cache(options))
    else:
        metrics, result = asyncio.run(_body(options))
    if options.source_root is not None:
        source = Path(sys.modules["wreath"].__file__).resolve()
        native = Path(sys.modules["wreath._native._core"].__file__).resolve()
        if not source.is_relative_to(options.source_root) or not native.is_relative_to(
            options.source_root
        ):
            raise RuntimeError(
                "resource workload imported a source or native artifact outside source-root"
            )
    options.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(
        json.dumps({"scenario": options.scenario, "count": options.count, **result}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
