"""Stage 1: the native HTTP/2 protocol emits a Pulse completion per stream."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from wreath import _flight_schema as fs
from wreath.server import ServerConfig

from . import support
from .conftest import FakeTransport, _settle

_flight = pytest.importorskip("wreath._native._flight")
try:
    from wreath._native._server import Http2Protocol
except ImportError:  # pragma: no cover -- the native h2 build is optional
    Http2Protocol = None

pytestmark = pytest.mark.skipif(Http2Protocol is None, reason="native h2 not built")


async def _ok_app(scope: dict, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 201, "headers": []})
    await send({"type": "http.response.body", "body": b"h2-flight"})


async def _drive(recorder, app=_ok_app, streams=(1,)):
    loop = asyncio.get_event_loop()
    transport = FakeTransport()
    protocol = Http2Protocol(app, ServerConfig(protocols=("h2",)), loop, set(),
                             recorder=recorder)
    protocol.connection_made(transport)
    await _settle()
    protocol.data_received(support.PREFACE)
    protocol.data_received(support.encode_settings({}))
    await _settle()
    for sid in streams:
        protocol.data_received(
            support.build_headers_frame(sid, support.request_headers(
                method=b"GET", path=b"/x", authority=b"example.com"))
        )
        await _settle()
    return protocol


@pytest.mark.asyncio
async def test_h2_records_a_completion_per_stream() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=16)
    await _drive(rec, streams=(1, 3, 5))
    assert rec.completions == 3
    assert rec.active_count == 0
    blob = rec.drain()
    assert len(blob) == 3 * fs.CELL_SIZE
    cell = fs.CompletionCell.decode(blob[: fs.CELL_SIZE])
    assert cell.status == 201
    assert cell.protocol is fs.Protocol.HTTP2
    assert cell.terminal is fs.TerminalStatus.OK
    # Response DATA payload framed for the stream (b"h2-flight" == 9 bytes); the
    # GET carried no request body.
    assert cell.bytes_out == 9
    assert cell.bytes_in == 0


@pytest.mark.asyncio
async def test_h2_off_recorder_is_inert() -> None:
    rec = _flight.Recorder(_flight.MODE_OFF)
    await _drive(rec, streams=(1,))
    assert rec.requests == 0
    assert rec.drain() == b""


@pytest.mark.asyncio
async def test_h2_route_attribution_stamps_metadata_ids() -> None:
    # HTTP/2 dispatches through the dict-scope path (no _RequestContext), so the
    # native protocol seeds `_wreath_flight` into the scope, Wreath dispatch
    # overwrites it with (route_id, plan_id), and C reads it back off the
    # retained scope before it emits the completion cell.
    from wreath import Response, Wreath
    from wreath._flight_metadata import build_metadata_image

    app = Wreath()

    @app.get("/widgets/{widget_id}")
    async def widget(request: object) -> Response:
        return Response(b"ok")

    app._compile_routes()
    image = build_metadata_image(app)
    expected = {(r.method, r.path): (r.route_id, r.plan_id) for r in image.routes}
    route_id, plan_id = expected[("GET", "/widgets/{widget_id}")]
    assert route_id != 0  # a real id was assigned

    loop = asyncio.get_event_loop()
    transport = FakeTransport()
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=16)
    protocol = Http2Protocol(app, ServerConfig(protocols=("h2",)), loop, set(),
                             recorder=rec)
    protocol.connection_made(transport)
    await _settle()
    protocol.data_received(support.PREFACE)
    protocol.data_received(support.encode_settings({}))
    await _settle()
    protocol.data_received(
        support.build_headers_frame(1, support.request_headers(
            method=b"GET", path=b"/widgets/42", authority=b"example.com"))
    )
    await _settle()

    assert rec.completions == 1
    cell = fs.CompletionCell.decode(rec.drain()[: fs.CELL_SIZE])
    assert cell.route_id == route_id
    assert cell.plan_id == plan_id


@pytest.mark.asyncio
async def test_h2_native_ai_refusal_is_a_structured_completion() -> None:
    from wreath import Wreath
    from wreath.metrics import collect

    app = Wreath()
    reached = False

    @app.get("/")
    async def handler(request):
        nonlocal reached
        reached = True
        return "not reached"

    loop = asyncio.get_event_loop()
    transport = FakeTransport()
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=16)
    protocol = Http2Protocol(
        app, ServerConfig(protocols=("h2",)), loop, set(), recorder=rec
    )
    protocol.connection_made(transport)
    await _settle()
    protocol.data_received(support.PREFACE)
    protocol.data_received(support.encode_settings({}))
    await _settle()
    protocol.data_received(
        support.build_headers_frame(
            1,
            support.request_headers(
                path=b"/", extra=[(b"user-agent", b"GPTBot/1.0")]
            ),
        )
    )
    await _settle()

    assert reached is False
    assert rec.requests == 1
    assert rec.completions == 1
    cell = fs.CompletionCell.decode(rec.drain())
    assert cell.protocol is fs.Protocol.HTTP2
    assert cell.status == 403
    assert cell.terminal is fs.TerminalStatus.OK
    assert cell.flags & fs.FLAG_POLICY_REFUSED
    assert cell.flags & fs.FLAG_AI_SCRAPING_REFUSED
    readings = {(row.subsystem, row.instance): row.values for row in collect(app)}
    assert readings[("ai_scraping_policy", "default")] == {"refused": 1}


@pytest.mark.asyncio
async def test_h2_native_refusal_resets_open_request_without_closing_connection() -> None:
    from wreath import Response, Wreath

    app = Wreath()
    reached: list[str] = []

    @app.post("/")
    async def refused_handler(request):
        reached.append("refused")
        return Response(b"not reached")

    @app.get("/healthy")
    async def healthy_handler(request):
        reached.append("healthy")
        return Response(b"ok")

    loop = asyncio.get_event_loop()
    transport = FakeTransport()
    protocol = Http2Protocol(app, ServerConfig(protocols=("h2",)), loop, set())
    protocol.connection_made(transport)
    await _settle()
    protocol.data_received(support.PREFACE)
    protocol.data_received(support.encode_settings({}))
    await _settle()

    protocol.data_received(
        support.build_headers_frame(
            1,
            support.request_headers(
                method=b"POST",
                path=b"/",
                extra=[(b"user-agent", b"GPTBot/1.0")],
            ),
            end_stream=False,
        )
    )
    await _settle()
    protocol.data_received(
        support.encode_frame(support.DATA, support.FLAG_END_STREAM, 1, b"in flight")
    )
    await _settle()
    protocol.data_received(
        support.build_headers_frame(
            3,
            support.request_headers(method=b"GET", path=b"/healthy"),
        )
    )
    await _settle()

    frames = support.FrameParser()
    frames.feed(bytes(transport.buffer))
    emitted = frames.frames()
    assert not [frame for frame in emitted if frame.type == support.GOAWAY]
    resets = [
        frame for frame in emitted
        if frame.type == support.RST_STREAM and frame.stream_id == 1
    ]
    assert resets
    assert int.from_bytes(resets[0].payload, "big") == support.NO_ERROR
    assert transport.closed is False
    assert reached == ["healthy"]


@pytest.mark.asyncio
async def test_h2_error_terminal_is_recorded() -> None:
    async def boom(scope, receive, send):
        raise RuntimeError("boom")

    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=16)
    await _drive(rec, app=boom, streams=(1,))
    assert rec.completions == 1
    cell = fs.CompletionCell.decode(rec.drain())
    assert cell.terminal is fs.TerminalStatus.ERROR
