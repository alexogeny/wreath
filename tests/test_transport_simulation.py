"""Interactive transport simulation composes replay, faults, and Flight."""

from __future__ import annotations

import importlib
from typing import Any

import pytest

import wreath
from wreath import _flight_schema as flight_schema
from wreath.replay import (
    FaultDescriptor,
    FaultKind,
    FaultSchedule,
    record_transport_segments,
    replay_transport,
)
from wreath.server import ServerConfig
from wreath.simulation import (
    SimulatedWebSocketFrame,
    SimulationError,
    TransportSimulator,
    WebSocketSimulator,
)
from wreath.websocket import WebSocket

NativeHttpProtocol = importlib.import_module("wreath._native._server").HttpProtocol
flight = importlib.import_module("wreath._native._flight")

PROTOCOLS = (
    pytest.param(NativeHttpProtocol, id="native"),
)


class AbortingProtocol:
    """Minimal custom protocol proving the selected class owns the transport."""

    config_seen: Any = None

    def __init__(self, app, config, loop, registry) -> None:
        type(self).config_seen = config
        self.transport: Any = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        self.transport.abort()

    def connection_lost(self, error) -> None:
        pass


class MalformedHandshakeProtocol:
    def __init__(self, app, config, loop, registry) -> None:
        self.transport: Any = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        self.transport.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"broken-header\r\n"
            b"sec-websocket-accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n"
        )

    def connection_lost(self, error) -> None:
        pass


class CountingCloseProtocol:
    lost = 0

    def __init__(self, app, config, loop, registry) -> None:
        self.transport: Any = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        pass

    def eof_received(self) -> None:
        pass

    def connection_lost(self, error) -> None:
        type(self).lost += 1


def _app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/health")
    async def health(request: wreath.Request) -> str:
        return "ready"

    @app.post("/echo")
    async def echo(request: wreath.Request) -> bytes:
        return await request.body()

    @app.websocket("/streams/{name}")
    async def stream(socket: WebSocket) -> None:
        await socket.accept(subprotocol="llama-trek.v1")
        async for frame in socket:
            if isinstance(frame, str):
                await socket.send_text(f"{socket.path_params['name']}:{frame}")
            else:
                await socket.send_bytes(frame[::-1])

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_cls", PROTOCOLS)
async def test_raw_transport_is_interactive_and_virtual_time_is_monotonic(
    protocol_cls: type,
) -> None:
    simulator = TransportSimulator(_app(), protocol_cls=protocol_cls)
    await simulator.start()
    await simulator.send(b"GET /health HTTP/1.1\r\nHost: x\r\n", offset_us=10)
    assert await simulator.receive() == b""

    await simulator.send(b"Connection: close\r\n\r\n", offset_us=25)
    response = await simulator.receive()
    result = await simulator.finish()

    assert b"HTTP/1.1 200" in response
    assert response.endswith(b"ready")
    assert simulator.now_us == 25
    assert result.segments_fed == 2
    with pytest.raises(ValueError, match="backwards"):
        simulator.advance_to(24)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_cls", PROTOCOLS)
async def test_websocket_peer_drives_real_frames_in_both_server_modes(
    protocol_cls: type,
) -> None:
    peer = WebSocketSimulator(
        _app(),
        "/streams/ridge",
        subprotocols=("llama-trek.v1",),
        protocol_cls=protocol_cls,
    )

    assert await peer.start() == ()
    assert peer.selected_subprotocol == "llama-trek.v1"
    text = await peer.send_text("seen")
    binary = await peer.send_bytes(b"abc")
    result = await peer.close()

    assert [frame.text() for frame in text] == ["ridge:seen"]
    assert [(frame.opcode, frame.payload) for frame in binary] == [(2, b"cba")]
    assert peer.frames[-1].opcode == 8
    assert result.segments_fed == 4


@pytest.mark.asyncio
async def test_existing_transport_fault_schedule_applies_to_interactive_frames() -> None:
    faults = FaultSchedule((FaultDescriptor(int(FaultKind.RESET), segment_index=1),))
    peer = WebSocketSimulator(
        _app(),
        "/streams/ridge",
        subprotocols=("llama-trek.v1",),
        faults=faults,
    )
    await peer.start()

    replies = await peer.send_text("answered before reset")
    assert [frame.text() for frame in replies] == ["ridge:answered before reset"]
    assert peer.transport.closed is True
    result = await peer.close()

    assert result.segments_fed == 2


@pytest.mark.asyncio
async def test_native_interactive_session_emits_the_existing_flight_completion() -> None:
    recorder = flight.Recorder(flight.MODE_PULSE, ring_records=64, active_requests=8)
    peer = WebSocketSimulator(
        _app(),
        "/streams/ridge",
        subprotocols=("llama-trek.v1",),
        protocol_cls=NativeHttpProtocol,
        recorder=recorder,
    )

    await peer.start()
    await peer.send_text("recorded")
    await peer.close()

    assert recorder.completions == 1
    cell = flight_schema.CompletionCell.decode(recorder.drain()[: flight_schema.CELL_SIZE])
    assert cell.protocol is flight_schema.Protocol.WEBSOCKET
    assert cell.status == 101
    assert cell.terminal is flight_schema.TerminalStatus.OK
    assert cell.bytes_in > 0
    assert cell.bytes_out > 0


@pytest.mark.asyncio
async def test_fixed_transport_replay_can_emit_into_the_same_flight_recorder() -> None:
    recorder = flight.Recorder(flight.MODE_PULSE, ring_records=64, active_requests=8)
    recording = record_transport_segments(
        [b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"]
    )

    result = await replay_transport(
        _app(),
        recording,
        protocol_cls=NativeHttpProtocol,
        recorder=recorder,
    )

    assert b"HTTP/1.1 200" in result.response
    cell = flight_schema.CompletionCell.decode(recorder.drain()[: flight_schema.CELL_SIZE])
    assert cell.protocol is flight_schema.Protocol.HTTP1
    assert cell.status == 200


@pytest.mark.asyncio
async def test_custom_protocol_and_aborted_terminal_are_not_replaced_by_default() -> None:
    config = ServerConfig()
    simulator = TransportSimulator(_app(), config=config, protocol_cls=AbortingProtocol)
    await simulator.start()
    await simulator.send(b"anything")

    assert (await simulator.finish()).terminal == "aborted"
    assert AbortingProtocol.config_seen is config


@pytest.mark.asyncio
async def test_peer_loss_is_delivered_once_and_an_open_custom_transport_is_named() -> None:
    CountingCloseProtocol.lost = 0
    simulator = TransportSimulator(_app(), protocol_cls=CountingCloseProtocol)
    await simulator.start()
    await simulator.close()
    first = await simulator.finish()
    second = await simulator.finish()

    assert CountingCloseProtocol.lost == 1
    assert first.terminal == "open"
    assert second is first


@pytest.mark.asyncio
async def test_short_read_and_timeout_use_the_existing_fault_implementation() -> None:
    dropped = TransportSimulator(
        _app(),
        faults=FaultSchedule(
            (FaultDescriptor(int(FaultKind.SHORT_READ), segment_index=0, value=0),)
        ),
    )
    await dropped.start()
    await dropped.send(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
    assert (await dropped.finish()).segments_fed == 0

    timed_out = TransportSimulator(
        _app(),
        faults=FaultSchedule((FaultDescriptor(int(FaultKind.TIMEOUT), segment_index=0),)),
    )
    await timed_out.start()
    await timed_out.send(b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\nx")
    assert b"HTTP/1.1 408" in await timed_out.receive()


def test_transport_and_websocket_boundaries_refuse_invalid_input() -> None:
    invalid_path: Any = 17
    with pytest.raises(TypeError, match="path must be a string"):
        WebSocketSimulator(_app(), invalid_path)
    with pytest.raises(ValueError, match="start with"):
        WebSocketSimulator(_app(), "relative")
    with pytest.raises(ValueError, match="line breaks"):
        WebSocketSimulator(_app(), "/stream", host="bad\nhost")
    with pytest.raises(ValueError, match="four bytes"):
        WebSocketSimulator(_app(), "/stream", mask=b"short")
    with pytest.raises(ValueError, match="valid base64"):
        WebSocketSimulator(_app(), "/stream", key=b"!!!!")
    with pytest.raises(ValueError, match="16 bytes"):
        WebSocketSimulator(_app(), "/stream", key=b"YQ==")
    with pytest.raises(ValueError, match="line breaks"):
        WebSocketSimulator(_app(), "/stream", subprotocols=("bad\nprotocol",))
    with pytest.raises(ValueError, match="non-empty"):
        WebSocketSimulator(_app(), "/stream", subprotocols=("",))
    with pytest.raises(ValueError, match="line breaks"):
        WebSocketSimulator(_app(), "/stream", headers={"bad\nname": "value"})
    with pytest.raises(ValueError, match="owned"):
        WebSocketSimulator(_app(), "/stream", headers={"Host": "elsewhere"})


@pytest.mark.asyncio
async def test_started_state_close_code_and_transport_bytes_are_guarded() -> None:
    peer = WebSocketSimulator(_app(), "/streams/ridge", subprotocols=("llama-trek.v1",))
    await peer.start()
    with pytest.raises(SimulationError) as repeated:
        await peer.start()
    assert str(repeated.value) == "the WebSocket simulation is already started"
    with pytest.raises(ValueError, match="close code"):
        await peer.close(999)
    await peer.close()

    transport = TransportSimulator(_app())
    await transport.start()
    invalid: Any = "not bytes"
    with pytest.raises(TypeError, match="must be bytes"):
        await transport.send(invalid)
    await transport.finish()


def test_only_text_frames_decode_as_text() -> None:
    assert SimulatedWebSocketFrame(True, 1, b"ridge").text() == "ridge"
    with pytest.raises(TypeError, match="text WebSocket frame"):
        SimulatedWebSocketFrame(True, 2, b"ridge").text()


@pytest.mark.asyncio
async def test_no_selected_subprotocol_is_preserved_as_none() -> None:
    app = wreath.Wreath()

    @app.websocket("/quiet")
    async def quiet(socket: WebSocket) -> None:
        await socket.accept()
        await socket.close()

    peer = WebSocketSimulator(app, "/quiet", subprotocols=("camera-trap.v1",))
    await peer.start()

    assert peer.selected_subprotocol is None
    await peer.close()


@pytest.mark.asyncio
async def test_malformed_server_handshake_header_is_refused() -> None:
    peer = WebSocketSimulator(_app(), "/stream", protocol_cls=MalformedHandshakeProtocol)

    with pytest.raises(SimulationError, match="malformed handshake header"):
        await peer.start()


@pytest.mark.asyncio
async def test_rejected_upgrade_names_status_and_unoffered_selection_is_refused() -> None:
    app = wreath.Wreath()

    @app.websocket("/reject")
    async def reject(socket: WebSocket) -> None:
        await socket.close()

    @app.websocket("/wrong-protocol")
    async def wrong_protocol(socket: WebSocket) -> None:
        await socket.accept(subprotocol="not-offered.v1")
        await socket.close()

    rejected = WebSocketSimulator(app, "/reject")
    with pytest.raises(SimulationError, match="HTTP/1.1 403"):
        await rejected.start()

    wrong = WebSocketSimulator(
        app,
        "/wrong-protocol",
        subprotocols=("camera-trap.v1",),
    )
    with pytest.raises(SimulationError, match="not offered"):
        await wrong.start()
