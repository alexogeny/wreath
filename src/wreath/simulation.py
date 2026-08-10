"""Interactive, socket-free transport simulation over Wreath's real server.

Replay answers a fixed recording.  This module supplies the complementary
interactive seam: feed one transport event, observe what the real protocol
wrote, and choose the next event.  It deliberately reuses replay's virtual
clock, fault schedule, fake transport, and protocol driver.  Passing a native
Flight Recorder reaches the same C completion path as a live connection.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ._websocket import build_frame, parse_frame
from .replay import (
    _FIRE_TIMEOUT,
    FaultSchedule,
    SegmentKind,
    TransportReplayResult,
    VirtualClock,
    _default_protocol_cls,
    _deliver_close,
    _drain,
    _feed,
    _fire_timeout,
    _normalize_response,
    _pump_after_feed,
    _ReplayTransport,
    _TransportFaultPlan,
)
from .server import ServerConfig

_WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_DEFAULT_KEY = b"dGhlIHNhbXBsZSBub25jZQ=="
_DEFAULT_MASK = b"\x01\x02\x03\x04"
_RESERVED_HANDSHAKE_HEADERS = frozenset(
    {
        "connection",
        "host",
        "sec-websocket-key",
        "sec-websocket-protocol",
        "sec-websocket-version",
        "upgrade",
    }
)


class SimulationError(RuntimeError):
    """A simulated transport or WebSocket conversation is invalid."""


class TransportSimulator:
    """One interactive connection driven through a real Wreath protocol.

    `send` is the transport's inbound edge.  Each call is one stable segment
    coordinate for `wreath.replay.FaultSchedule`; `receive` returns
    only bytes written since the preceding receive.  No wall-clock sleep or
    real resource is involved.

    A supplied `recorder` is passed directly to the protocol constructor.
    Native HTTP/1, HTTP/2, and WebSocket sessions therefore emit their normal
    Flight cells from C.  The pure HTTP/1 twin accepts and ignores that uniform
    constructor argument, exactly as it does under the server factory.
    """

    __slots__ = (
        "_clock",
        "_config",
        "_cursor",
        "_data_index",
        "_faults",
        "_finished",
        "_peer_lost",
        "_protocol",
        "_protocol_cls",
        "_recorder",
        "_registry",
        "_result",
        "_segments_fed",
        "_transport",
        "app",
    )

    def __init__(
        self,
        app: Any,
        *,
        config: ServerConfig | None = None,
        protocol_cls: type | None = None,
        faults: FaultSchedule | None = None,
        recorder: object | None = None,
        peername: tuple[str, int] = ("127.0.0.1", 54321),
        sockname: tuple[str, int] = ("127.0.0.1", 8000),
    ) -> None:
        self.app = app
        self._config = config or ServerConfig()
        self._protocol_cls = protocol_cls or _default_protocol_cls()
        self._faults = _TransportFaultPlan(faults) if faults is not None else None
        self._recorder = recorder
        self._transport = _ReplayTransport(peername, sockname)
        self._clock = VirtualClock()
        self._registry: set[Any] = set()
        self._protocol: Any = None
        self._data_index = -1
        self._segments_fed = 0
        self._cursor = 0
        self._peer_lost = False
        self._finished = False
        self._result: TransportReplayResult | None = None

    @property
    def now_us(self) -> int:
        """Current virtual time in microseconds."""
        return self._clock.now_us

    @property
    def closed(self) -> bool:
        """Whether either side has closed or aborted the fake transport."""
        return self._transport.closed or self._peer_lost

    async def start(self) -> None:
        """Create the protocol and deliver `connection_made` once."""
        if self._finished:
            raise SimulationError("a finished transport simulation cannot restart")
        if self._protocol is not None:
            raise SimulationError("the transport simulation is already started")
        loop = asyncio.get_running_loop()
        arguments = (self.app, self._config, loop, self._registry)
        if self._recorder is None:
            self._protocol = self._protocol_cls(*arguments)
        else:
            self._protocol = self._protocol_cls(*arguments, recorder=self._recorder)
        self._protocol.connection_made(self._transport)

    def advance_to(self, offset_us: int) -> None:
        """Advance virtual time without delivering bytes; time never rewinds."""
        if isinstance(offset_us, bool) or not isinstance(offset_us, int):
            raise TypeError("offset_us must be an integer")
        if offset_us < self._clock.now_us:
            raise ValueError("virtual time cannot move backwards")
        self._clock.advance_to(offset_us)

    async def send(self, data: bytes, *, offset_us: int | None = None) -> None:
        """Deliver one inbound byte segment through the configured fault plan."""
        self._require_active()
        if self.closed:
            raise SimulationError("cannot send after the simulated connection closed")
        if not isinstance(data, bytes):
            raise TypeError("transport data must be bytes")
        if offset_us is not None:
            self.advance_to(offset_us)
        self._data_index += 1
        reads: tuple[bytes, ...] = (data,)
        forced_close: int | None = None
        if self._faults is not None:
            reads, forced_close = self._faults.rewrite(self._data_index, self._clock, data)
        fed_any = False
        for read in reads:
            if not read:
                continue
            _feed(self._protocol, read)
            fed_any = True
            await _pump_after_feed(self._transport)
        if fed_any:
            self._segments_fed += 1
        if forced_close == _FIRE_TIMEOUT:
            await _drain(self._transport)
            _fire_timeout(self._protocol)
            await _drain(self._transport)
        elif forced_close is not None:
            await self._lose_peer(forced_close)

    async def receive(self) -> bytes:
        """Return bytes written since the previous receive after a quiet drain."""
        self._require_started()
        await _drain(self._transport)
        end = len(self._transport.buffer)
        data = bytes(self._transport.buffer[self._cursor : end])
        self._cursor = end
        return data

    async def close(self, kind: SegmentKind = SegmentKind.EOF) -> None:
        """Deliver a peer half-close or reset through the real protocol."""
        self._require_active()
        if kind not in {SegmentKind.EOF, SegmentKind.RESET}:
            raise ValueError("transport close kind must be EOF or RESET")
        if not self._peer_lost:
            await self._lose_peer(int(kind))

    async def finish(self) -> TransportReplayResult:
        """Settle the connection, deliver a final EOF if needed, and summarize it."""
        self._require_started()
        if self._result is not None:
            return self._result
        await _drain(self._transport)
        if not self._peer_lost and not self._transport.closed:
            _deliver_close(self._protocol, int(SegmentKind.EOF))
            self._peer_lost = True
            await _drain(self._transport)
        terminal = (
            "aborted"
            if self._transport.aborted
            else ("closed" if self._transport.closed else "open")
        )
        response = bytes(self._transport.buffer)
        self._result = TransportReplayResult(
            response=response,
            normalized=_normalize_response(response),
            terminal=terminal,
            write_count=self._transport.write_count,
            segments_fed=self._segments_fed,
        )
        self._finished = True
        return self._result

    async def _lose_peer(self, kind: int) -> None:
        await _drain(self._transport)
        _deliver_close(self._protocol, kind)
        self._peer_lost = True
        await _drain(self._transport)

    def _require_started(self) -> None:
        if self._protocol is None:
            raise SimulationError("start the transport simulation first")

    def _require_active(self) -> None:
        self._require_started()
        if self._finished:
            raise SimulationError("the transport simulation is finished")


@dataclass(frozen=True, slots=True)
class SimulatedWebSocketFrame:
    """One server-to-peer WebSocket frame decoded from the real wire bytes."""

    fin: bool
    opcode: int
    payload: bytes

    def text(self) -> str:
        """Decode a text frame, refusing other opcodes."""
        if self.opcode != 1:
            raise TypeError("only a text WebSocket frame has text")
        return self.payload.decode("utf-8")


class WebSocketSimulator:
    """Interactive RFC 6455 peer backed by `TransportSimulator`.

    Handshake and client frames are genuine HTTP/1 and masked WebSocket wire
    bytes.  The server's native or pure parser, route, handler, backpressure,
    close handling, and Flight hooks are therefore the code under test; this
    class models only the peer edge.
    """

    __slots__ = (
        "_frame_buffer",
        "_frames",
        "_handshake",
        "_headers",
        "_host",
        "_key",
        "_mask",
        "_path",
        "_selected_subprotocol",
        "_started",
        "_subprotocols",
        "transport",
    )

    def __init__(
        self,
        app: Any,
        path: str,
        *,
        subprotocols: Sequence[str] = (),
        headers: Mapping[str, str] | None = None,
        host: str = "simulation.invalid",
        key: bytes = _DEFAULT_KEY,
        mask: bytes = _DEFAULT_MASK,
        config: ServerConfig | None = None,
        protocol_cls: type | None = None,
        faults: FaultSchedule | None = None,
        recorder: object | None = None,
    ) -> None:
        _validate_token(path, "path", starts_with_slash=True)
        _validate_token(host, "host")
        if len(mask) != 4:
            raise ValueError("a WebSocket client mask must contain four bytes")
        try:
            decoded_key = base64.b64decode(key, validate=True)
        except ValueError as error:
            raise ValueError("the WebSocket key must be valid base64") from error
        if len(decoded_key) != 16:
            raise ValueError("the WebSocket key must encode exactly 16 bytes")
        checked_headers: list[tuple[str, str]] = []
        for name, value in (headers or {}).items():
            _validate_token(name, "header name")
            _validate_token(value, f"header {name!r}")
            if name.lower() in _RESERVED_HANDSHAKE_HEADERS:
                raise ValueError(f"header {name!r} is owned by the WebSocket handshake")
            checked_headers.append((name, value))
        for subprotocol in subprotocols:
            _validate_token(subprotocol, "subprotocol")
        self.transport = TransportSimulator(
            app,
            config=config,
            protocol_cls=protocol_cls,
            faults=faults,
            recorder=recorder,
        )
        self._path = path
        self._host = host
        self._key = key
        self._mask = mask
        self._subprotocols = tuple(subprotocols)
        self._headers = tuple(checked_headers)
        self._handshake = b""
        self._frame_buffer = bytearray()
        self._frames: list[SimulatedWebSocketFrame] = []
        self._selected_subprotocol: str | None = None
        self._started = False

    @property
    def handshake(self) -> bytes:
        """The server's complete HTTP upgrade response head."""
        return self._handshake

    @property
    def selected_subprotocol(self) -> str | None:
        return self._selected_subprotocol

    @property
    def frames(self) -> tuple[SimulatedWebSocketFrame, ...]:
        """Every server frame observed so far."""
        return tuple(self._frames)

    async def start(self) -> tuple[SimulatedWebSocketFrame, ...]:
        """Perform and verify the real WebSocket upgrade."""
        if self._started:
            raise SimulationError("the WebSocket simulation is already started")
        await self.transport.start()
        await self.transport.send(self._upgrade_request())
        response = await self.transport.receive()
        head, marker, tail = response.partition(b"\r\n\r\n")
        if not marker:
            raise SimulationError("the server did not produce a complete WebSocket handshake")
        self._handshake = head + marker
        self._verify_handshake(head)
        self._started = True
        return self._decode(tail)

    async def send_text(
        self, text: str, *, offset_us: int | None = None
    ) -> tuple[SimulatedWebSocketFrame, ...]:
        return await self.send_frame(1, text.encode("utf-8"), offset_us=offset_us)

    async def send_bytes(
        self, payload: bytes, *, offset_us: int | None = None
    ) -> tuple[SimulatedWebSocketFrame, ...]:
        return await self.send_frame(2, payload, offset_us=offset_us)

    async def send_frame(
        self,
        opcode: int,
        payload: bytes = b"",
        *,
        fin: bool = True,
        offset_us: int | None = None,
    ) -> tuple[SimulatedWebSocketFrame, ...]:
        """Send one masked client frame and return newly observed server frames."""
        if not self._started:
            raise SimulationError("start the WebSocket simulation first")
        if opcode not in {0, 1, 2, 8, 9, 10}:
            raise ValueError("unsupported WebSocket opcode")
        await self.transport.send(
            build_frame(opcode, payload, fin, self._mask),
            offset_us=offset_us,
        )
        return await self.receive()

    async def receive(self) -> tuple[SimulatedWebSocketFrame, ...]:
        """Decode server frames written since the preceding operation."""
        if not self._started:
            raise SimulationError("start the WebSocket simulation first")
        return self._decode(await self.transport.receive())

    async def close(self, code: int = 1000, reason: str = "") -> TransportReplayResult:
        """Send a client close frame and settle the whole transport session."""
        if not 1000 <= code <= 4999:
            raise ValueError("WebSocket close code must be between 1000 and 4999")
        if not self.transport.closed:
            payload = code.to_bytes(2, "big") + reason.encode("utf-8")
            await self.send_frame(8, payload)
        result = await self.transport.finish()
        self._decode(await self.transport.receive())
        return result

    def _upgrade_request(self) -> bytes:
        lines = [
            f"GET {self._path} HTTP/1.1",
            f"Host: {self._host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {self._key.decode('ascii')}",
            "Sec-WebSocket-Version: 13",
        ]
        if self._subprotocols:
            lines.append("Sec-WebSocket-Protocol: " + ", ".join(self._subprotocols))
        lines.extend(f"{name}: {value}" for name, value in self._headers)
        return ("\r\n".join((*lines, "", ""))).encode("ascii")

    def _verify_handshake(self, head: bytes) -> None:
        lines = head.split(b"\r\n")
        if not lines[0].startswith(b"HTTP/1.1 101 "):
            status = lines[0].decode("ascii", "replace")
            raise SimulationError(f"WebSocket upgrade was refused: {status}")
        response_headers: dict[bytes, bytes] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(b":")
            if not separator:
                raise SimulationError("the server returned a malformed handshake header")
            response_headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1(self._key + _WEBSOCKET_GUID, usedforsecurity=False).digest()
        )
        if response_headers.get(b"sec-websocket-accept") != expected:
            raise SimulationError("the server returned an invalid WebSocket accept value")
        selected = response_headers.get(b"sec-websocket-protocol")
        if selected is not None:
            self._selected_subprotocol = selected.decode("ascii")
            if self._selected_subprotocol not in self._subprotocols:
                raise SimulationError("the server selected a WebSocket subprotocol not offered")

    def _decode(self, data: bytes) -> tuple[SimulatedWebSocketFrame, ...]:
        self._frame_buffer += data
        fresh: list[SimulatedWebSocketFrame] = []
        while self._frame_buffer:
            parsed = parse_frame(bytes(self._frame_buffer))
            if parsed is None:
                break
            fin, opcode, payload, consumed = parsed
            del self._frame_buffer[:consumed]
            frame = SimulatedWebSocketFrame(fin, opcode, payload)
            self._frames.append(frame)
            fresh.append(frame)
        return tuple(fresh)


def _validate_token(value: str, what: str, *, starts_with_slash: bool = False) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string")
    if not value or "\r" in value or "\n" in value or not value.isascii():
        raise ValueError(f"{what} must be non-empty ASCII without line breaks")
    if starts_with_slash and not value.startswith("/"):
        raise ValueError("WebSocket path must start with '/'")


__all__ = [
    "SimulatedWebSocketFrame",
    "SimulationError",
    "TransportSimulator",
    "WebSocketSimulator",
]
