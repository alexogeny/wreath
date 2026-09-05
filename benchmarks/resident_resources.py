"""Untraced retained RSS/PSS at explicit ownership boundaries, never peak RSS.

Run each frozen source tree in a fresh process with the same scenario and size.
Inputs and declaration setup precede the first sample; validation follows the
second sample. Both samples follow GC. Object-range sampling keeps the iterator
suspended at its first yield; verification drains it only after sampling.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import sys
import tracemalloc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from time import process_time_ns
from types import ModuleType
from typing import Any

DEFAULT_SIZES = {
    "record-batch": 100_000,
    "response-validators": 10_000,
    "route-masks": 4096,
    "application-image": 8000,
    "response-capture": 8 * 1024 * 1024,
    "object-range": 64 * 1024 * 1024,
    "tiny-objects": 1000,
}


def require_untraced() -> None:
    if tracemalloc.is_tracing():
        raise RuntimeError("tracemalloc must be disabled for resident measurements")


def resident_bytes(path: Path = Path("/proc/self/smaps_rollup")) -> dict[str, int]:
    values = {}
    for line in path.read_text().splitlines():
        name, _, value = line.partition(":")
        if name in {"Rss", "Pss"}:
            values[name.lower() + "_bytes"] = int(value.split()[0]) * 1024
    if values.keys() != {"rss_bytes", "pss_bytes"}:
        raise RuntimeError("smaps_rollup must provide Rss and Pss")
    return values


@dataclass
class Case:
    operation: Callable[[], Awaitable[Any]]
    verify: Callable[[Any], Awaitable[dict[str, Any]]]
    phase: str
    modules: tuple[ModuleType, ...]
    sources: tuple[str, ...] = ()


async def prepare(scenario: str, size: int, chunk_bytes: int) -> Case:
    require_untraced()
    if scenario not in DEFAULT_SIZES or size <= 0 or chunk_bytes <= 0:
        raise ValueError("choose a listed scenario and positive size/chunk-bytes")
    if scenario == "record-batch":
        from wreath._native import extension

        pg = extension("_postgres")
        names = ("a", "b", "c", "d")
        expected = (1000, -(2**63), 2**63 - 1, 0)
        payload = b"\x00\x04" + b"".join(
            b"\x00\x00\x00\x08" + n.to_bytes(8, "big", signed=True) for n in expected
        )
        tape = pg._FieldTape(4)
        view = memoryview(payload)
        for _ in range(size):
            tape.append(view, 4)
        plan = pg._compile_decoder_plan((20,) * 4, (1,) * 4, names)

        async def operation():
            return pg._decode_field_tape(plan, tape, "fetch_batch", size)

        async def verify(batch):
            if batch is None or len(batch) != size:
                raise RuntimeError("record batch did not retain the expected rows")
            for row in batch:
                if tuple(row[name] for name in names) != expected:
                    raise RuntimeError("record cells differ from signed integer byte oracle")
            return {"rows": size, "cells": size * 4, "row": expected}

        return Case(
            operation,
            verify,
            "decoded batch and input tape retained",
            (pg,),
            ("wreath/_native/postgres/record.c",),
        )

    if scenario == "response-validators":
        from wreath import binding, response

        def handler(request):
            return request

        annotation = list[dict[str, int]]
        payload = [{"n": 1}, {"n": -2}]
        binding.compile_response_validator(handler, annotation)(payload)

        async def operation():
            return [binding.compile_response_validator(handler, annotation) for _ in range(size)]

        async def verify(wrappers):
            if not isinstance(wrappers, list) or len(wrappers) != size:
                raise RuntimeError("expected one retained response wrapper per declaration")
            for wrapper in wrappers:
                result = wrapper(payload)
                if (
                    not isinstance(result, response._EncodedJSON)
                    or result.body != b'[{"n":1},{"n":-2}]'
                ):
                    raise RuntimeError("response validator did not produce the literal JSON oracle")
            return {"wrappers": size, "body": '[{"n":1},{"n":-2}]'}

        return Case(
            operation,
            verify,
            "compiled response wrappers retained before invocation",
            (binding, response, binding._core),
        )

    if scenario == "route-masks":
        from wreath._native import _core

        if size > 4096:
            raise ValueError("route-masks size must be at most 4096 unique base4 paths")
        table = _core.PolicyRouteTable()
        paths = []
        literals = {(-1, "api")}
        for index in range(size):
            parts = [str((index >> (2 * position)) & 3) for position in range(6)]
            literals.update(enumerate(parts))
            tail = "/".join(parts)
            table.add(f"/api/{{id}}/{tail}", "GET", index, (0,))
            paths.append(f"/api/value/{tail}")

        async def operation():
            table.compile()
            return table

        async def verify(result):
            if result is not table or table.stats()["literal_keys"] != len(literals):
                raise RuntimeError("route masks were not compiled with the declared literal keys")
            if table.stats()["routes"] != size:
                raise RuntimeError("compiled route count differs from declarations")
            for index, path in enumerate(paths):
                if table.match("GET", path, 0) != (index, {"id": "value"}):
                    raise RuntimeError("compiled route match differs from declaration")
            return {
                "routes": size,
                "literal_keys": len(literals),
                "checksum": size * (size - 1) // 2,
            }

        return Case(
            operation,
            verify,
            "compiled literal maps retained before matching",
            (_core,),
            ("wreath/_native/policy_router.c",),
        )

    if scenario == "application-image":
        from wreath import Wreath, app, binding, openapi

        application = Wreath(ai_scraping="allow")

        async def endpoint(limit: int = 10) -> str:
            return str(limit)

        for index in range(size):
            application.get(f"/items/{index}", summary="List items")(endpoint)

        async def operation():
            return openapi.generate_openapi(application)

        async def verify(document):
            expected_paths = {f"/items/{index}" for index in range(size)}
            if not isinstance(document, dict) or set(document.get("paths", {})) != expected_paths:
                raise RuntimeError("OpenAPI paths differ from registered routes")
            if document.get("openapi") != "3.1.0":
                raise RuntimeError("OpenAPI document lacks its protocol version")
            encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            return {"routes": size, "document_sha256": hashlib.sha256(encoded).hexdigest()}

        return Case(
            operation,
            verify,
            "cold OpenAPI document and application image both retained",
            (app, binding, openapi, binding._core),
        )

    if scenario == "response-capture":
        from wreath import _asgi_state

        capture = _asgi_state.ResponseCapture()

        async def operation():
            await capture.send({"type": "http.response.start", "status": 200})
            for index, start in enumerate(range(0, size, chunk_bytes)):
                await capture.send(
                    {
                        "type": "http.response.body",
                        "body": bytes([index % 251]) * min(chunk_bytes, size - start),
                        "more_body": start + chunk_bytes < size,
                    }
                )
            return capture, capture.body

        async def verify(result):
            if not isinstance(result, tuple) or len(result) != 2 or result[0] is not capture:
                raise RuntimeError("capture and completed body must both remain owned")
            body = result[1]
            if not capture.finished or capture.status != 200 or len(body) != size:
                raise RuntimeError("capture did not complete the expected response")
            expected = hashlib.sha256()
            for index, start in enumerate(range(0, size, chunk_bytes)):
                expected.update(bytes([index % 251]) * min(chunk_bytes, size - start))
            digest = hashlib.sha256(body).hexdigest()
            if digest != expected.hexdigest():
                raise RuntimeError("captured bytes differ from submitted chunk oracle")
            return {
                "bytes": size,
                "chunks": (size + chunk_bytes - 1) // chunk_bytes,
                "sha256": digest,
            }

        return Case(
            operation,
            verify,
            "completed capture plus first materialized body retained",
            (_asgi_state,),
        )

    from wreath import objects

    store = objects.MemoryObjectStore(url_secret=b"r" * 32)
    if scenario == "object-range":
        if size <= 2048:
            raise ValueError("object-range size must exceed 2048 bytes")
        payload = (bytes(range(256)) * ((size + 255) // 256))[:size]
        await store.write("payload", payload, content_type="application/octet-stream")
        start, stop = 1024, size // 2

        async def operation():
            iterator = store.read_stream("payload", range=(start, stop - 1))
            first = await anext(iterator)
            return iterator, first

        async def verify(result):
            if not isinstance(result, tuple) or len(result) != 2:
                raise RuntimeError("range iterator must remain suspended with its first chunk")
            iterator, first = result
            if first != payload[start : min(start + 65536, stop)]:
                raise RuntimeError("first range chunk differs from source slice")
            digest = hashlib.sha256(first)
            total = len(first)
            try:
                async for chunk in iterator:
                    digest.update(chunk)
                    total += len(chunk)
            finally:
                await iterator.aclose()
            expected = hashlib.sha256(memoryview(payload)[start:stop]).hexdigest()
            if total != stop - start or digest.hexdigest() != expected:
                raise RuntimeError("range stream differs from independent slice digest")
            return {"bytes": total, "first_bytes": len(first), "sha256": expected}

        return Case(
            operation,
            verify,
            "source object retained; range iterator suspended after first yield",
            (objects,),
        )

    keys = [f"body/{index:04d}" for index in range(size)]
    keys.sort()

    async def operation():
        for key in keys:
            await store.write(key, b"x", content_type="text/plain")
        return store

    async def verify(result):
        if result is not store:
            raise RuntimeError("tiny-object store must remain owned")
        stats = [stat async for stat in store.list("body/")]
        expected_etag = hashlib.md5(b"x", usedforsecurity=False).hexdigest()
        if [stat.key for stat in stats] != keys or any(
            stat.size != 1 or stat.etag != expected_etag or stat.content_type != "text/plain"
            for stat in stats
        ):
            raise RuntimeError("tiny-object metadata differs from stored payload oracle")
        return {"objects": size, "etag": expected_etag}

    return Case(operation, verify, "tiny-object store retained before metadata reads", (objects,))


def artifact_paths(case: Case, source_root: Path) -> list[Path]:
    root = source_root.resolve()
    paths = [Path(module.__file__).resolve() for module in case.modules]
    paths.extend(root / source for source in case.sources)
    for path in paths:
        if not path.is_relative_to(root):
            raise RuntimeError(f"loaded {path} outside requested source {root}")
    return list(dict.fromkeys(paths))


def artifact_manifest(paths: list[Path]) -> list[dict[str, str]]:
    return [
        {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in paths
    ]


async def measure(scenario: str, size: int, source_root: Path, chunk_bytes: int = 65536):
    require_untraced()
    case = await prepare(scenario, size, chunk_bytes)
    paths = artifact_paths(case, source_root)
    gc.collect()
    require_untraced()
    before = resident_bytes()
    started = process_time_ns()
    retained = await case.operation()
    elapsed = process_time_ns() - started
    gc.collect()
    require_untraced()
    after = resident_bytes()
    output = await case.verify(retained)
    require_untraced()
    metrics = {
        "cpu_ns": elapsed,
        "tracing_enabled": False,
        "phase": case.phase,
        **after,
        **{"before_" + key: value for key, value in before.items()},
        **{"growth_" + key: after[key] - value for key, value in before.items()},
        "artifacts": artifact_manifest(paths),
    }
    return metrics, {"scenario": scenario, "size": size, **output}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--scenario", choices=DEFAULT_SIZES, required=True)
    parser.add_argument("--size", type=int)
    parser.add_argument("--chunk-bytes", type=int, default=65536)
    args = parser.parse_args()
    size = DEFAULT_SIZES[args.scenario] if args.size is None else args.size
    require_untraced()
    sys.path.insert(0, str(args.source_root.resolve()))
    metrics, output = asyncio.run(measure(args.scenario, size, args.source_root, args.chunk_bytes))
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
