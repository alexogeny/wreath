"""Stage 1 server integration: the native HTTP/1 protocol emits Pulse cells.

Drives the native Http1Protocol over a fake transport (the established harness in
test_server_protocol) with a recorder attached, and asserts one completion cell
per request. Skips cleanly when the native server or _flight is not built.
"""

from __future__ import annotations

import asyncio

import pytest

from wreath import _flight_schema as fs
from wreath.server import ServerConfig

# exc_type=ImportError, not the default: an extension that is present but
# refuses to initialise raises plain ImportError, and pytest only auto-skips on
# ModuleNotFoundError -- so without this these modules fail collection instead
# of skipping as the docstring says.
_native_server = pytest.importorskip("wreath._native._server", exc_type=ImportError)
_flight = pytest.importorskip("wreath._native._flight", exc_type=ImportError)

if not hasattr(_native_server, "HttpProtocol"):
    pytest.skip("native HTTP/1 server not built", allow_module_level=True)

from tests._server_ingest import feed  # noqa: E402
from tests.test_server_protocol import FakeTransport, _settle  # noqa: E402


async def _app(scope: dict, receive, send) -> None:
    assert scope["type"] == "http"
    await send({"type": "http.response.start", "status": 201, "headers": []})
    await send({"type": "http.response.body", "body": b"hello-flight"})


async def _drive_one(recorder, request: bytes) -> FakeTransport:
    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = _native_server.HttpProtocol(
        _wrap(_app), ServerConfig(), loop, set(), recorder=recorder
    )
    protocol.connection_made(transport)
    feed(protocol, request)
    await _settle()
    return transport


def _wrap(app):
    # The native protocol reads app._wreath_http lazily; a bare ASGI callable is
    # fine (it just has no native fast path).
    return app


_GET = b"GET /hello HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"


@pytest.mark.asyncio
async def test_pulse_records_a_completion_per_request() -> None:
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    transport = await _drive_one(recorder, _GET)
    assert recorder.requests == 1
    assert recorder.completions == 1
    blob = recorder.drain()
    assert len(blob) == fs.CELL_SIZE
    cell = fs.CompletionCell.decode(blob)
    assert cell.status == 201
    assert cell.protocol is fs.Protocol.HTTP1
    assert cell.terminal is fs.TerminalStatus.OK
    assert cell.request_id == 1
    assert cell.connection_id >= 1
    # bytes_out is every byte written to the transport for this response (head +
    # body); bytes_in is the request-body length (zero here).
    assert cell.bytes_in == 0
    assert cell.bytes_out == len(bytes(transport.buffer))
    assert cell.bytes_out > 0


_POST = (
    b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 11\r\n\r\nhello world"
)


@pytest.mark.asyncio
async def test_request_body_is_counted_as_bytes_in() -> None:
    async def echo(scope: dict, receive, send) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = _native_server.HttpProtocol(
        echo, ServerConfig(), loop, set(), recorder=recorder
    )
    protocol.connection_made(transport)
    feed(protocol, _POST)
    await _settle()
    cell = fs.CompletionCell.decode(recorder.drain())
    assert cell.bytes_in == 11  # "hello world"
    assert cell.bytes_out == len(bytes(transport.buffer))


@pytest.mark.asyncio
async def test_keep_alive_records_each_request() -> None:
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = _native_server.HttpProtocol(_app, ServerConfig(), loop, set(), recorder=recorder)
    protocol.connection_made(transport)
    for _ in range(3):
        feed(protocol, _GET)
        await _settle()
    assert recorder.completions == 3
    assert recorder.active_count == 0  # every slot released
    assert len(recorder.drain()) == 3 * fs.CELL_SIZE


@pytest.mark.asyncio
async def test_off_recorder_attached_is_inert() -> None:
    recorder = _flight.Recorder(_flight.MODE_OFF)
    await _drive_one(recorder, _GET)
    assert recorder.requests == 0
    assert recorder.drain() == b""


@pytest.mark.asyncio
async def test_no_recorder_is_the_default() -> None:
    # No recorder attached: the protocol runs exactly as before, no telemetry.
    transport = await _drive_one(None, _GET)
    assert b"201" in transport.buffer


_TRACED = (
    b"GET /hello HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n"
    b"traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01\r\n\r\n"
)


@pytest.mark.asyncio
async def test_route_attribution_stamps_metadata_ids() -> None:
    # A real Wreath app takes the native fast path (app._wreath_http), so the
    # request scope is a _RequestContext carrying the recorder context and
    # dispatch stamps the matched route's metadata IDs onto the completion cell.
    from wreath import Response, Wreath
    from wreath._flight_metadata import build_metadata_image

    app = Wreath()

    @app.get("/widgets/{widget_id}")
    async def widget(request: object) -> Response:
        return Response(b"ok")

    app._compile_routes()
    image = build_metadata_image(app)
    expected = {
        (r.method, r.path): (r.route_id, r.plan_id) for r in image.routes
    }
    route_id, plan_id = expected[("GET", "/widgets/{widget_id}")]
    assert route_id != 0  # a real id was assigned

    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = _native_server.HttpProtocol(app, ServerConfig(), loop, set(), recorder=recorder)
    protocol.connection_made(transport)
    feed(protocol, b"GET /widgets/42 HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
    await _settle()

    assert recorder.completions == 1
    cell = fs.CompletionCell.decode(recorder.drain()[: fs.CELL_SIZE])
    assert cell.route_id == route_id
    assert cell.plan_id == plan_id


@pytest.mark.asyncio
async def test_off_does_not_stamp_or_flag_the_context() -> None:
    # With telemetry off, the native context's `flight` flag stays 0 so dispatch
    # never attributes -- Off must be branch-free on the route path.
    from wreath import Response, Wreath

    app = Wreath()

    @app.get("/x")
    async def handler(request: object) -> Response:
        return Response(b"ok")

    app._compile_routes()
    recorder = _flight.Recorder(_flight.MODE_OFF)
    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = _native_server.HttpProtocol(app, ServerConfig(), loop, set(), recorder=recorder)
    protocol.connection_made(transport)
    feed(protocol, b"GET /x HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
    await _settle()
    assert recorder.requests == 0  # Off records nothing
    assert b"200" in bytes(transport.buffer)


@pytest.mark.asyncio
async def test_incoming_traceparent_produces_a_correlation_cell() -> None:
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    await _drive_one(recorder, _TRACED)
    blob = recorder.drain()
    assert len(blob) == 2 * fs.CELL_SIZE  # completion + correlation
    correlation = fs.CorrelationCell.decode(blob[fs.CELL_SIZE :])
    assert correlation.trace_id == (0x4BF92F3577B34DA6 << 64) | 0xA3CE929D0E0E4736
    assert correlation.parent_span_id == 0x00F067AA0BA902B7
    assert correlation.span_id != 0


# --- WebSocket completion cells ---------------------------------------------
#
# A WebSocket connection maps to one completion cell for the whole session,
# emitted when the app task ends. protocol is WEBSOCKET; status carries the
# handshake disposition (101 established, else the rejection status); terminal
# carries how the session ended; bytes_in/out accumulate over the session.

from wreath._websocket import build_frame  # noqa: E402

_WS_KEY = b"dGhlIHNhbXBsZSBub25jZQ=="
_WS_MASK = b"\x01\x02\x03\x04"


def _ws_upgrade(path: bytes = b"/ws") -> bytes:
    return (
        b"GET " + path + b" HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Key: " + _WS_KEY +
        b"\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )


async def _drive_ws(recorder, app, chunks: list[bytes]) -> FakeTransport:
    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = _native_server.HttpProtocol(app, ServerConfig(), loop, set(), recorder=recorder)
    protocol.connection_made(transport)
    for chunk in chunks:
        feed(protocol, chunk)
        await _settle()
    await _settle()
    return transport


async def _echo_ws(scope: dict, receive, send) -> None:
    assert scope["type"] == "websocket"
    assert (await receive())["type"] == "websocket.connect"
    await send({"type": "websocket.accept"})
    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            return
        await send({"type": "websocket.send", "text": message["text"]})


@pytest.mark.asyncio
async def test_ws_pulse_records_one_completion_for_the_session() -> None:
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    transport = await _drive_ws(
        recorder,
        _echo_ws,
        [
            _ws_upgrade(),
            build_frame(1, b"ping", True, _WS_MASK),  # client text
            build_frame(8, (1000).to_bytes(2), True, _WS_MASK),  # client close
        ],
    )
    assert recorder.completions == 1
    cell = fs.CompletionCell.decode(recorder.drain()[: fs.CELL_SIZE])
    assert cell.protocol is fs.Protocol.WEBSOCKET
    assert cell.status == 101  # handshake was accepted (101 Switching Protocols)
    # The app handled websocket.disconnect and returned cleanly.
    assert cell.terminal is fs.TerminalStatus.OK
    # bytes_in is the application payload of every received frame: "ping" (4) plus
    # the 2-byte close code. bytes_out is every byte written (handshake + echo +
    # close frame).
    assert cell.bytes_in == 6
    assert cell.bytes_out == len(bytes(transport.buffer))
    assert cell.bytes_out > 0


@pytest.mark.asyncio
async def test_ws_rejected_handshake_is_recorded() -> None:
    async def _reject(scope: dict, receive, send) -> None:
        await receive()  # websocket.connect
        # returns without accepting -> the server rejects the handshake with 403

    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=16)
    await _drive_ws(recorder, _reject, [_ws_upgrade()])
    assert recorder.completions == 1
    cell = fs.CompletionCell.decode(recorder.drain()[: fs.CELL_SIZE])
    assert cell.protocol is fs.Protocol.WEBSOCKET
    assert cell.status == 403
    assert cell.terminal is fs.TerminalStatus.OK


@pytest.mark.asyncio
async def test_ws_error_terminal_is_recorded() -> None:
    async def _boom(scope: dict, receive, send) -> None:
        await receive()
        await send({"type": "websocket.accept"})
        raise RuntimeError("boom")

    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=16)
    await _drive_ws(recorder, _boom, [_ws_upgrade()])
    assert recorder.completions == 1
    cell = fs.CompletionCell.decode(recorder.drain()[: fs.CELL_SIZE])
    assert cell.protocol is fs.Protocol.WEBSOCKET
    assert cell.terminal is fs.TerminalStatus.ERROR
    assert cell.status == 101  # socket was established before the handler raised


@pytest.mark.asyncio
async def test_ws_off_recorder_is_inert() -> None:
    recorder = _flight.Recorder(_flight.MODE_OFF)
    await _drive_ws(
        recorder,
        _echo_ws,
        [_ws_upgrade(), build_frame(8, (1000).to_bytes(2), True, _WS_MASK)],
    )
    assert recorder.requests == 0
    assert recorder.drain() == b""


@pytest.mark.asyncio
async def test_detailed_telemetry_config_arms_completion_cells() -> None:
    # End-to-end: a DETAILED TelemetryConfig with a full sample rate flows through
    # the server's recorder factory and arms every completion cell.
    from wreath.server import _create_recorder
    from wreath.telemetry import Mode, SamplingPolicy, TelemetryConfig

    telemetry = TelemetryConfig(mode=Mode.DETAILED, detailed=SamplingPolicy(rate=1.0))
    recorder = _create_recorder(ServerConfig(telemetry=telemetry))
    assert recorder is not None

    for _ in range(3):
        await _drive_one(recorder, _GET)
    blob = recorder.drain()
    cells = [
        fs.CompletionCell.decode(blob[i * fs.CELL_SIZE : (i + 1) * fs.CELL_SIZE])
        for i in range(len(blob) // fs.CELL_SIZE)
    ]
    assert len(cells) == 3
    assert all(c.flags & fs.FLAG_DETAILED_ARMED for c in cells)


@pytest.mark.asyncio
async def test_ws_route_attribution_stamps_metadata_ids() -> None:
    # A real Wreath app dispatches the websocket scope through _handle_websocket,
    # which stamps the matched WEBSOCKET route's IDs into the retained scope; C
    # reads them off it at completion. WS routes carry no HTTP plan (plan_id 0).
    from wreath import Wreath
    from wreath._flight_metadata import build_metadata_image
    from wreath.websocket import WebSocket

    app = Wreath()

    @app.websocket("/ws/feed")
    async def feed(ws: WebSocket) -> None:
        await ws.accept()
        await ws.close()

    app._compile_routes()
    image = build_metadata_image(app)
    expected = {(r.method, r.path): (r.route_id, r.plan_id) for r in image.routes}
    route_id, plan_id = expected[("WEBSOCKET", "/ws/feed")]
    assert route_id != 0  # a real id was assigned
    assert plan_id == 0  # WS handlers carry no HTTP endpoint plan

    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    await _drive_ws(recorder, app, [_ws_upgrade(b"/ws/feed")])
    assert recorder.completions == 1
    cell = fs.CompletionCell.decode(recorder.drain()[: fs.CELL_SIZE])
    assert cell.protocol is fs.Protocol.WEBSOCKET
    assert cell.route_id == route_id
    assert cell.plan_id == plan_id


# --- Stage 3 slice 2b: request-path phase markers ---------------------------


def _phase_cells(blob: bytes) -> list:
    out = []
    for i in range(len(blob) // fs.CELL_SIZE):
        cell = blob[i * fs.CELL_SIZE : (i + 1) * fs.CELL_SIZE]
        if cell[1] == fs.EventKind.PHASE:
            out.append(fs.PhaseBatchCell.decode(cell))
    return out


async def _drive_app(recorder, app, request: bytes) -> None:
    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = _native_server.HttpProtocol(
        app, ServerConfig(), loop, set(), recorder=recorder
    )
    protocol.connection_made(transport)
    feed(protocol, request)
    await _settle()


@pytest.mark.asyncio
async def test_armed_request_emits_handler_and_serialize_phases() -> None:
    # Detailed at rate 1.0 arms every request: dispatch times the handler body
    # and response coercion and records one phase for each. The completion cell
    # still leads the drained blob (phases commit behind it).
    from wreath import Response, Wreath

    app = Wreath()
    seen_flight: list[int] = []

    @app.get("/x")
    async def handler(request) -> Response:
        seen_flight.append(request._context.flight)
        return Response(b"ok")

    app._compile_routes()
    recorder = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=64, active_requests=8,
        detailed_sample_rate=1.0, phase_slots=4,
    )
    await _drive_app(
        recorder, app, b"GET /x HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
    )

    assert seen_flight == [2]  # armed state was visible to dispatch
    blob = recorder.drain()
    assert blob[1] == fs.EventKind.COMPLETION
    records = [r for b in _phase_cells(blob) for r in b.records]
    assert [r.phase_id for r in records] == [
        fs.PhaseKind.HANDLER, fs.PhaseKind.SERIALIZE
    ]
    assert all(r.coverage is fs.PhaseCoverage.PYTHON for r in records)
    completion = fs.CompletionCell.decode(blob[: fs.CELL_SIZE])
    batches = _phase_cells(blob)
    assert all(b.request_id == completion.request_id for b in batches)


@pytest.mark.asyncio
async def test_pulse_request_stays_unarmed_and_emits_no_phases() -> None:
    # Pulse never arms: the context's flight flag stays 1, dispatch binds no
    # phase marker, and the drained blob carries only the completion cell.
    from wreath import Response, Wreath

    app = Wreath()
    seen_flight: list[int] = []

    @app.get("/x")
    async def handler(request) -> Response:
        seen_flight.append(request._context.flight)
        return Response(b"ok")

    app._compile_routes()
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    await _drive_app(
        recorder, app, b"GET /x HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
    )

    assert seen_flight == [1]  # recording, not armed
    blob = recorder.drain()
    assert len(blob) == fs.CELL_SIZE  # exactly one completion, no phase cells
    assert blob[1] == fs.EventKind.COMPLETION


@pytest.mark.asyncio
async def test_armed_protected_request_emits_an_auth_phase() -> None:
    # A protected route authenticates before resolve; an armed request times
    # that call and records an AUTH phase ahead of HANDLER/SERIALIZE.
    from wreath import Response, Wreath
    from wreath.auth import BearerTokenBackend, Identity
    from wreath.authorization import roles

    async def verify(token: str) -> Identity | None:
        return Identity(token, roles=frozenset({"admin"}))

    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/admin")
    @roles("admin")
    async def admin(request) -> Response:
        return Response(b"ok")

    app._compile_routes()
    recorder = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=64, active_requests=8,
        detailed_sample_rate=1.0, phase_slots=4,
    )
    await _drive_app(
        recorder,
        app,
        b"GET /admin HTTP/1.1\r\nHost: x\r\n"
        b"Authorization: Bearer admin\r\nContent-Length: 0\r\n\r\n",
    )

    blob = recorder.drain()
    records = [r for b in _phase_cells(blob) for r in b.records]
    kinds = [r.phase_id for r in records]
    assert kinds[0] is fs.PhaseKind.AUTH
    assert fs.PhaseKind.HANDLER in kinds and fs.PhaseKind.SERIALIZE in kinds
    assert [r.sequence for r in records] == list(range(len(records)))


@pytest.mark.asyncio
async def test_armed_request_propagates_marker_and_severs_it_at_completion() -> None:
    # Dispatch binds the ContextVar marker for an armed request so dependency
    # seams can record phases; completion severs the context, so a binding that
    # escaped the request is an inert no-op instead of a stale write.
    from wreath import Response, Wreath
    from wreath._flight_markers import phase_marker

    app = Wreath()
    seen: dict = {}

    @app.get("/x")
    async def handler(request) -> Response:
        seen["marker"] = phase_marker.get(None)
        seen["context"] = request._context
        return Response(b"ok")

    app._compile_routes()
    recorder = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=64, active_requests=8,
        detailed_sample_rate=1.0, phase_slots=4,
    )
    await _drive_app(
        recorder, app, b"GET /x HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
    )

    assert seen["marker"] is not None  # the seam-visible binding existed
    context = seen["context"]
    assert context.flight == 0  # severed at completion
    before = recorder.drain()
    records = [r for b in _phase_cells(before) for r in b.records]
    assert len(records) == 2  # HANDLER + SERIALIZE from dispatch
    # The escaped binding no-ops: no crash, no new cells, no gauge movement.
    seen["marker"](int(fs.PhaseKind.DB_QUERY), 0, 0, 1000)
    assert recorder.drain() == b""
    assert recorder.phase_in_use == 0


@pytest.mark.asyncio
async def test_pulse_request_does_not_bind_the_marker() -> None:
    from wreath import Response, Wreath
    from wreath._flight_markers import phase_marker

    app = Wreath()
    seen: dict = {}

    @app.get("/x")
    async def handler(request) -> Response:
        seen["marker"] = phase_marker.get(None)
        return Response(b"ok")

    app._compile_routes()
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    await _drive_app(
        recorder, app, b"GET /x HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
    )
    assert seen["marker"] is None
