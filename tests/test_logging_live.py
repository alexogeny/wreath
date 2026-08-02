"""Logging, live end-to-end over a real server.

Everything the other logging suites test is reachable by hand. This one asserts
the wiring: that a record written in a handler reaches the ring the recorder
owns, is joined to *its own* request's trace by the projector, and comes out of
the writer carrying the trace and span ids the recorder generated -- without the
handler having said anything about correlation.

That is the whole claim the design rests on, so it is tested against a real
server over a real socket rather than against a fake recorder.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import wreath
from wreath import logging as log
from wreath.server import ServerConfig, serve
from wreath.telemetry import Mode, SamplingPolicy, TelemetryConfig

pytest.importorskip("wreath._native._server", exc_type=ImportError)
pytest.importorskip("wreath._native._flight", exc_type=ImportError)


DENIED = log.event(
    "live.denied",
    "user {user} denied {resource}",
    level=log.WARN,
    fields=(log.field("user", int), log.field("resource", str, log.RAW)),
)
STEP = log.event(
    "live.step",
    "step {name}",
    level=log.DEBUG,
    fields=(log.field("name", str, log.RAW),),
)


def _app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/ok")
    async def ok(request: wreath.Request) -> wreath.Response:
        DENIED(17, "orders")
        log.set_field("tenant_id", 42)
        return wreath.response.TextResponse("ok")

    @app.get("/quiet")
    async def quiet(request: wreath.Request) -> wreath.Response:
        STEP("validate")
        STEP("charge")
        return wreath.response.TextResponse("quiet")

    @app.get("/boom")
    async def boom(request: wreath.Request) -> wreath.Response:
        STEP("before the fall")
        raise RuntimeError("boom")

    return app


def _config(lines: list[str]) -> ServerConfig:
    return ServerConfig(
        host="127.0.0.1",
        port=0,
        lifespan="off",
        telemetry=TelemetryConfig(
            mode=Mode.DETAILED,
            ring_records=512,
            active_requests=32,
            detailed=SamplingPolicy(rate=1.0),
        ),
        log_writer=lines.append,
    )


async def _get(port: int, path: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    body = await asyncio.wait_for(reader.read(), timeout=2.0)
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionResetError, BrokenPipeError):
        pass
    return body


async def _drain(server: object, lines: list[str], *, expect: int) -> list[dict]:
    """Let the projector and writer catch up, then parse what was written."""
    for _ in range(100):
        if len([ln for ln in lines if ln.startswith("{")]) >= expect:
            break
        await asyncio.sleep(0.02)
    return [json.loads(ln) for ln in lines if ln.startswith("{")]


@pytest.mark.asyncio
async def test_a_handler_record_arrives_correlated_to_its_request() -> None:
    lines: list[str] = []
    server = await serve(_app(), _config(lines))
    port = server.sockets[0].getsockname()[1]
    try:
        await _get(port, "/ok")
        records = await _drain(server, lines, expect=1)
    finally:
        await server.close()
        records = [json.loads(ln) for ln in lines if ln.startswith("{")]

    denied = [r for r in records if r.get("event") == "live.denied"]
    assert denied, f"the handler's record never reached the writer: {records}"
    assert denied[0]["message"] == "user 17 denied orders"
    assert denied[0]["severity"] == "WARN"
    # The handler said nothing about correlation; the recorder's ids arrived anyway.
    assert denied[0]["trace_id"] != "0" * 32
    assert len(denied[0]["trace_id"]) == 32
    assert len(denied[0]["span_id"]) == 16


@pytest.mark.asyncio
async def test_debug_records_stay_quiet_on_a_healthy_request() -> None:
    lines: list[str] = []
    server = await serve(_app(), _config(lines))
    port = server.sockets[0].getsockname()[1]
    try:
        await _get(port, "/quiet")
    finally:
        await server.close()
    records = [json.loads(ln) for ln in lines if ln.startswith("{")]
    assert not [r for r in records if r.get("event") == "live.step"]


@pytest.mark.asyncio
async def test_debug_records_are_promoted_when_the_request_fails() -> None:
    """The payoff: verbose history for exactly the request that went wrong."""
    lines: list[str] = []
    server = await serve(_app(), _config(lines))
    port = server.sockets[0].getsockname()[1]
    try:
        await _get(port, "/boom")
        await _drain(server, lines, expect=1)
    finally:
        await server.close()
    records = [json.loads(ln) for ln in lines if ln.startswith("{")]
    steps = [r for r in records if r.get("event") == "live.step"]
    assert steps, f"a failed request published no buffered records: {records}"
    assert steps[0]["message"] == "step before the fall"
    assert "promoted" in steps[0].get("flags", [])


@pytest.mark.asyncio
async def test_two_requests_do_not_share_a_trace() -> None:
    """The join is by request id, so concurrent requests must not cross."""
    lines: list[str] = []
    server = await serve(_app(), _config(lines))
    port = server.sockets[0].getsockname()[1]
    try:
        await asyncio.gather(*(_get(port, "/ok") for _ in range(4)))
        await _drain(server, lines, expect=4)
    finally:
        await server.close()
    records = [json.loads(ln) for ln in lines if ln.startswith("{")]
    denied = [r for r in records if r.get("event") == "live.denied"]
    assert len(denied) == 4
    assert len({r["trace_id"] for r in denied}) == 4


@pytest.mark.asyncio
async def test_logging_is_inert_when_telemetry_is_off() -> None:
    """Off must stay off: no runtime installed, no records, no writer thread."""
    lines: list[str] = []
    config = ServerConfig(host="127.0.0.1", port=0, lifespan="off", log_writer=lines.append)
    server = await serve(_app(), config)
    port = server.sockets[0].getsockname()[1]
    try:
        await _get(port, "/ok")
    finally:
        await server.close()
    assert lines == []


# --- HTTP/2, HTTP/3 and WebSocket ------------------------------------------
#
# The dict-scope transports have no request-context object, so their protocols
# seed the recorder's request id into the scope. If that seeding regresses, a
# record still emits -- it just silently loses its correlation, which is the
# failure these tests exist to make loud.


# HTTP/2 correlation is covered by tests/http2/test_logging.py, which drives the
# native protocol through the in-repo frame harness rather than an optional
# third-party client -- an importorskip here would have been a test that never
# ran on this machine and reported green.


def _ws_app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.websocket("/ws")
    async def socket(ws: wreath.WebSocket) -> None:
        await ws.accept()
        STEP("ws opened")
        DENIED(5, "socket")
        await ws.close()

    @app.websocket("/ws-boom")
    async def boom_socket(ws: wreath.WebSocket) -> None:
        await ws.accept()
        STEP("ws before the fall")
        raise RuntimeError("ws boom")

    return app


async def _ws(port: int, path: str) -> None:
    import base64
    import os

    key = base64.b64encode(os.urandom(16)).decode()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    await writer.drain()
    try:
        await asyncio.wait_for(reader.read(4096), timeout=2.0)
    except TimeoutError:
        pass
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionResetError, BrokenPipeError):
        pass


@pytest.mark.asyncio
async def test_websocket_records_are_correlated() -> None:
    """A session is one recorder context, so its records join one trace."""
    lines: list[str] = []
    server = await serve(_ws_app(), _config(lines))
    port = server.sockets[0].getsockname()[1]
    try:
        await _ws(port, "/ws")
        await _drain(server, lines, expect=1)
    finally:
        await server.close()
    records = [json.loads(ln) for ln in lines if ln.startswith("{")]
    denied = [r for r in records if r.get("event") == "live.denied"]
    assert denied, f"the WebSocket session produced no record: {records}"
    assert len(denied[0]["trace_id"]) == 32
    assert denied[0]["trace_id"] != "0" * 32


@pytest.mark.asyncio
async def test_a_websocket_session_that_raises_promotes_its_records() -> None:
    lines: list[str] = []
    server = await serve(_ws_app(), _config(lines))
    port = server.sockets[0].getsockname()[1]
    try:
        await _ws(port, "/ws-boom")
        await _drain(server, lines, expect=1)
    finally:
        await server.close()
    records = [json.loads(ln) for ln in lines if ln.startswith("{")]
    steps = [r for r in records if r.get("event") == "live.step"]
    assert steps, f"a failed session published no buffered records: {records}"
    assert "promoted" in steps[0].get("flags", [])


@pytest.mark.asyncio
async def test_a_healthy_websocket_session_stays_quiet() -> None:
    lines: list[str] = []
    server = await serve(_ws_app(), _config(lines))
    port = server.sockets[0].getsockname()[1]
    try:
        await _ws(port, "/ws")
    finally:
        await server.close()
    records = [json.loads(ln) for ln in lines if ln.startswith("{")]
    assert not [r for r in records if r.get("event") == "live.step"]


# --- configuration ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_configured_level_is_honoured() -> None:
    """`level` was hardcoded once; this is the test that keeps it plumbed."""
    from wreath.telemetry import LoggingConfig

    lines: list[str] = []
    config = ServerConfig(
        host="127.0.0.1", port=0, lifespan="off",
        telemetry=TelemetryConfig(
            mode=Mode.DETAILED, ring_records=512, active_requests=32,
            detailed=SamplingPolicy(rate=1.0),
            logging=LoggingConfig(level=log.ERROR, capture_level=log.ERROR),
        ),
        log_writer=lines.append,
    )
    server = await serve(_app(), config)
    port = server.sockets[0].getsockname()[1]
    try:
        await _get(port, "/ok")   # emits WARN, below the configured ERROR
    finally:
        await server.close()
    records = [json.loads(ln) for ln in lines if ln.startswith("{")]
    assert not [r for r in records if r.get("event") == "live.denied"]
