"""Completion-gate coverage for the neutral workload suite.

Runs the workload verifier in-process (native and, via a separate env, pure)
and confirms the workload app is an ordinary ASGI application that a
third-party server can drive — the app never requires the native server.
"""

from __future__ import annotations

import pytest

from benchmarks.workloads._fakepg import FakePostgres
from benchmarks.workloads.app import build_app
from benchmarks.workloads.verify import _run


@pytest.mark.asyncio
async def test_workload_properties_hold() -> None:
    assert await _run() == 0


@pytest.mark.asyncio
async def test_app_is_plain_asgi() -> None:
    # Drive the app through the raw ASGI callable (no Wreath server involved),
    # proving the workload endpoints work behind any conforming ASGI server.
    server = FakePostgres()
    dsn = await server.start()
    app = build_app(dsn)

    # Lifespan protocol.
    lifespan_events = iter(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )
    sent: list[dict] = []

    async def receive_lifespan() -> dict:
        return next(lifespan_events)

    async def send_lifespan(message: dict) -> None:
        sent.append(message)

    await app({"type": "lifespan"}, receive_lifespan, send_lifespan)
    assert {"type": "lifespan.startup.complete"} in sent

    # One HTTP request through the bare ASGI interface.
    app2 = build_app(dsn)
    startup = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    await app2({"type": "lifespan"}, lambda: _next(startup), lambda m: _noop())

    messages: list[dict] = []
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/plaintext",
        "raw_path": b"/plaintext",
        "query_string": b"",
        "headers": [],
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await app2(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = next(m for m in messages if m["type"] == "http.response.body")
    assert start["status"] == 200
    assert body["body"] == b"Hello, World!"

    await server.close()


async def _next(iterator):
    return next(iterator)


async def _noop() -> None:
    return None
