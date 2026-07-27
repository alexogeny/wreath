"""Pure-Python reference implementation of Wreath's HTTP/1.1 server protocol.

This module is the executable behavioral specification for the native
``wreath._native._server`` port. It implements an ``asyncio.Protocol`` that parses
HTTP/1.0 and HTTP/1.1 requests, frames fixed-length and chunked bodies, drives
an ASGI application, and encodes responses -- all on top of an asyncio (or
uvloop) transport. Socket polling, TLS, and platform differences stay in the
event loop; only the HTTP hot path lives here.

At most one application request runs per connection at a time. Pipelined bytes
are preserved and the next request is only dispatched after the current
response emits its terminal body message.

The constructor signature and the observable ``asyncio.Protocol`` methods match
the native port exactly. Parity tests compare bytes on the wire, ASGI
scopes/messages, and closure behavior -- never internal object layout.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sys
from collections import deque
from typing import TYPE_CHECKING, Any, cast

from .._codecs import percent_decode
from .._http import parse_request
from .._websocket import build_frame as ws_build_frame
from .._websocket import parse_frame as ws_parse_frame

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ..server import ServerConfig

    Scope = dict[str, Any]
    Message = dict[str, Any]
    ASGIApplication = Callable[
        [Scope, Callable[[], Awaitable[Message]], Callable[[Message], Awaitable[None]]],
        Awaitable[None],
    ]

# --- connection states ------------------------------------------------------
READING_HEAD = "READING_HEAD"
READING_FIXED_BODY = "READING_FIXED_BODY"
READING_CHUNK_SIZE = "READING_CHUNK_SIZE"
READING_CHUNK_DATA = "READING_CHUNK_DATA"
READING_CHUNK_TRAILERS = "READING_CHUNK_TRAILERS"
REQUEST_RUNNING = "REQUEST_RUNNING"
WS_HANDSHAKE = "WS_HANDSHAKE"  # upgrade request dispatched, 101 not yet sent
WS_OPEN = "WS_OPEN"            # WebSocket frames flowing
CLOSING = "CLOSING"

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_TEXT = 0x1
_WS_BINARY = 0x2
_WS_CLOSE = 0x8
_WS_PING = 0x9
_WS_PONG = 0xA

_STATUS_NO_BODY = frozenset({204, 304})
_HOP_BODY_METHODS = frozenset({"HEAD"})

# ASGI extensions advertised in every HTTP scope.  "wreath.response" accepts a
# single one-shot message carrying status, headers, and the complete body.
# Shared across connections; consumers treat scope contents as read-only.
_EXTENSIONS: dict[str, dict[str, object]] = {"wreath.response": {}}

# Precomputed reason phrases for the statuses the server itself emits. Uncommon
# application statuses fall back to a generic "Unknown" phrase; the phrase is
# advisory and never parsed by clients.
_REASONS = {
    200: b"OK",
    204: b"No Content",
    304: b"Not Modified",
    400: b"Bad Request",
    403: b"Forbidden",
    408: b"Request Timeout",
    413: b"Payload Too Large",
    414: b"URI Too Long",
    417: b"Expectation Failed",
    426: b"Upgrade Required",
    431: b"Request Header Fields Too Large",
    500: b"Internal Server Error",
    505: b"HTTP Version Not Supported",
}


def _reason(status: int) -> bytes:
    return _REASONS.get(status, b"Unknown")


class _Disconnect(Exception):
    """Raised inside the app task when the peer disconnects mid-request."""


class HttpProtocol(asyncio.Protocol):
    """One HTTP connection. Owns its buffer, parser/body state, app task,
    receive/backpressure waiters, timeout handles, and response state."""

    def __init__(
        self,
        app: ASGIApplication,
        config: ServerConfig,
        loop: asyncio.AbstractEventLoop,
        connection_registry: set[HttpProtocol],
        recorder: object | None = None,
    ) -> None:
        # The pure server has no native recorder; accept the argument for a
        # uniform factory signature and ignore it (telemetry needs the native
        # _flight worker, which only the native protocols hold).
        self.app = app
        self.config = config
        self.loop = loop
        self.registry = connection_registry

        self.transport: asyncio.Transport | None = None
        self.state = READING_HEAD
        self._buffer = bytearray()
        self._cursor = 0

        # Per-request state (reset by _reset_request).
        self._task: asyncio.Task[None] | None = None
        self._receive_queue: deque[Message] = deque()
        self._receive_waiter: asyncio.Future[None] | None = None
        self._queued_bytes = 0
        self._reading_paused = False
        self._remaining = 0  # bytes left for a fixed-length body
        self._chunk_remaining = 0  # bytes left in the current chunk
        self._disconnected = False
        self._request_more_body = True

        # Incremental head-scan offsets: search only newly-arrived bytes (with a
        # small delimiter overlap) so a byte-at-a-time head stays linear.
        self._head_scan = 0
        self._line_scan = 0
        self._line_end = -1

        # Write backpressure.
        self._write_paused = False
        self._drain_waiter: asyncio.Future[None] | None = None

        # Response state.
        self._response_started = False
        self._response_complete = False
        self._response_keep_alive = True
        self._response_chunked = False
        self._response_suppress_body = False
        self._response_content_length: int | None = None
        self._response_body_sent = 0
        self._head_written = False
        self._resp_status = 200
        self._resp_headers: list[tuple[bytes, bytes]] = []
        self._http_version = "1.1"
        self._request_method = ""
        self._framing_error = False

        # Timers.
        self._request_timer: asyncio.TimerHandle | None = None
        self._keep_alive_timer: asyncio.TimerHandle | None = None

        # WebSocket state (populated when a connection upgrades).
        self._ws_mode = False
        self._ws_key = b""
        self._ws_accepted = False
        self._ws_close_sent = False
        self._ws_frag_opcode: int | None = None
        self._ws_frag_parts: list[bytes] = []
        self._ws_frag_size = 0
        self._ws_frag_count = 0

        self._closing = False
        self._accepting = True  # cleared by the server during graceful shutdown

    # --- asyncio.Protocol callbacks ----------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # create_server always yields a bidirectional Transport here.
        self.transport = cast("asyncio.Transport", transport)
        self.registry.add(self)
        self._start_keep_alive_timer()

    def data_received(self, data: bytes) -> None:
        if self._closing:
            return
        self._buffer += data
        self._drive()

    def eof_received(self) -> bool | None:
        # Peer will send no more data. Any in-flight request loses its body.
        self._disconnected = True
        if self._task is not None and not self._task.done():
            self._deliver_disconnect()
        # Returning False lets asyncio close the transport for us.
        return False

    def connection_lost(self, exc: BaseException | None) -> None:
        self._disconnected = True
        self._closing = True
        self.state = CLOSING
        self._cancel_request_timer()
        self._cancel_keep_alive_timer()
        if self._task is not None and not self._task.done():
            self._deliver_disconnect()
        # Wake a pending drain so the app task can unwind.
        self._resolve_drain()
        self.registry.discard(self)
        self.transport = None

    def pause_writing(self) -> None:
        self._write_paused = True

    def resume_writing(self) -> None:
        self._write_paused = False
        self._resolve_drain()

    # --- driving the state machine -----------------------------------------

    def _drive(self) -> None:
        """Consume as much buffered input as the current state allows."""
        while not self._closing:
            state = self.state
            if state == READING_HEAD:
                if not self._drive_head():
                    return
            elif state == READING_FIXED_BODY:
                if not self._drive_fixed_body():
                    return
            elif state == READING_CHUNK_SIZE:
                if not self._drive_chunk_size():
                    return
            elif state == READING_CHUNK_DATA:
                if not self._drive_chunk_data():
                    return
            elif state == READING_CHUNK_TRAILERS:
                if not self._drive_chunk_trailers():
                    return
            elif state == WS_OPEN:
                if not self._drive_ws_frame():
                    return
            else:
                # REQUEST_RUNNING with no body to read, WS_HANDSHAKE awaiting
                # the application's accept, or CLOSING: wait for the response
                # to finish (which re-drives) or for more data.
                return

    def _pending(self) -> memoryview:
        return memoryview(self._buffer)[self._cursor :]

    def _consume(self, count: int) -> None:
        self._cursor += count
        # Compact only once the consumed prefix is material, to avoid O(n^2)
        # front-deletions on every small read.
        if self._cursor > 65536 and self._cursor * 2 >= len(self._buffer):
            del self._buffer[: self._cursor]
            self._cursor = 0

    # --- head ---------------------------------------------------------------

    def _drive_head(self) -> bool:
        # Search the buffer in place, resuming from where the last fragment left
        # off. Only newly-arrived bytes are examined; a small overlap keeps a
        # delimiter split across data_received calls findable. This makes a
        # byte-at-a-time head linear rather than N*(N+1)/2.
        buf = self._buffer
        base = self._cursor
        total = len(buf) - base  # pending length

        # First "\r\n" (end of the request line), found once and cached.
        if self._line_end < 0:
            line_from = base + (self._line_scan - 1 if self._line_scan >= 1 else 0)
            found = buf.find(b"\r\n", line_from)
            if found >= 0:
                self._line_end = found - base
            else:
                self._line_scan = total

        head_from = base + (self._head_scan - 3 if self._head_scan >= 3 else 0)
        terminator = buf.find(b"\r\n\r\n", head_from)
        if terminator < 0:
            self._head_scan = total
            if self._line_end < 0:
                if total > self.config.max_request_line:
                    self._send_error(414)
                return False
            if self._line_end > self.config.max_request_line:
                self._send_error(414)
                return False
            if total > self.config.max_header_bytes:
                self._send_error(431)
                return False
            return False

        head_end = terminator - base
        if head_end + 4 > self.config.max_header_bytes:
            self._send_error(431)
            return False
        if self._line_end < 0:  # a complete head always has a request line
            self._line_end = buf.find(b"\r\n", base) - base
        if self._line_end > self.config.max_request_line:
            self._send_error(414)
            return False

        try:
            head = parse_request(bytes(buf[base : base + head_end + 4]))
        except ValueError:
            self._send_error(400)
            return False
        if head is None:  # pragma: no cover - head_end found guarantees a head
            return False
        if len(head.headers) > self.config.max_header_count:
            self._send_error(431)
            return False

        self._consume(head.consumed)
        # A request head is arriving/complete: request timeout owns it now.
        self._cancel_keep_alive_timer()
        self._start_request_timer()
        self._begin_request(head)
        return True

    def _begin_request(self, head: Any) -> None:
        method = head.method
        minor = head.minor_version
        http_version = "1.1" if minor == 1 else "1.0"
        self._http_version = http_version
        self._request_method = method

        target = head.target
        raw_path, _, query_string = target.partition(b"?")
        try:
            path = percent_decode(raw_path).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            self._send_error(400)
            return

        if self._is_upgrade_request(head.headers):
            self._begin_websocket(method, minor, path, raw_path, query_string, head.headers)
            return

        # Body framing decision.
        try:
            framing = self._decide_framing(head.headers)
        except _FramingError as exc:
            self._send_error(exc.status)
            return

        scope = self._build_scope(method, http_version, path, raw_path, query_string, head.headers)

        self._reset_response(method, http_version, head.headers)
        self._receive_queue = deque()
        self._receive_waiter = None
        self._queued_bytes = 0
        self._disconnected = False
        self._request_more_body = True

        kind, length = framing
        if kind == "none":
            self._request_more_body = False
            self.state = REQUEST_RUNNING
        elif kind == "fixed":
            self._remaining = length
            self.state = READING_FIXED_BODY if length > 0 else REQUEST_RUNNING
            if length == 0:
                self._request_more_body = False
        else:  # chunked
            self._chunk_remaining = 0
            self.state = READING_CHUNK_SIZE

        if self.state == REQUEST_RUNNING:
            # No request body: hand the app a single terminal message.
            self._receive_queue.append(
                {"type": "http.request", "body": b"", "more_body": False}
            )

        self._task = self.loop.create_task(self._run_app(scope))

    # --- websocket upgrade ----------------------------------------------------

    @staticmethod
    def _is_upgrade_request(headers: list[tuple[bytes, bytes]]) -> bool:
        upgrade = None
        connection = None
        for name, value in headers:
            if name == b"upgrade" and upgrade is None:
                upgrade = value
            elif name == b"connection" and connection is None:
                connection = value
        if upgrade is None or connection is None:
            return False
        if upgrade.strip().lower() != b"websocket":
            return False
        tokens = [t.strip().lower() for t in connection.split(b",")]
        return b"upgrade" in tokens

    def _begin_websocket(
        self,
        method: str,
        minor: int,
        path: str,
        raw_path: bytes,
        query_string: bytes,
        headers: list[tuple[bytes, bytes]],
    ) -> None:
        key = None
        version = None
        protocols: list[str] = []
        for name, value in headers:
            if name == b"sec-websocket-key" and key is None:
                key = value.strip()
            elif name == b"sec-websocket-version" and version is None:
                version = value.strip()
            elif name == b"sec-websocket-protocol":
                protocols.extend(
                    part.strip().decode("latin-1")
                    for part in value.split(b",")
                    if part.strip()
                )
        if method != "GET" or minor != 1 or not key:
            self._send_error(400)
            return
        if version != b"13":
            self._send_error(426)
            return

        transport = self.transport
        assert transport is not None
        server = transport.get_extra_info("sockname")
        client = transport.get_extra_info("peername")
        scheme = "wss" if transport.get_extra_info("sslcontext") is not None else "ws"
        scope: Scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "http_version": "1.1",
            "scheme": scheme,
            "path": path,
            "raw_path": raw_path,
            "query_string": query_string,
            "headers": headers,
            "server": tuple(server[:2]) if server else None,
            "client": tuple(client[:2]) if client else None,
            "root_path": "",
            "subprotocols": protocols,
        }

        self._ws_mode = True
        self._ws_key = key
        self._ws_accepted = False
        self._ws_close_sent = False
        self._ws_frag_opcode = None
        self._ws_frag_parts = []
        self._ws_frag_size = 0
        self._ws_frag_count = 0
        self._receive_queue = deque([{"type": "websocket.connect"}])
        self._receive_waiter = None
        self._queued_bytes = 0
        self._disconnected = False
        # The request timer stays armed until the application accepts, so a
        # handshake the app never answers still times out as a request.
        self.state = WS_HANDSHAKE
        self._task = self.loop.create_task(self._run_ws_app(scope))

    async def _ws_send(self, message: Message) -> None:
        if self._disconnected:
            raise _Disconnect
        message_type = message["type"]
        if message_type == "websocket.send":
            if not self._ws_accepted or self._ws_close_sent:
                raise RuntimeError("websocket is not open")
            text = message.get("text")
            if text is not None:
                frame = ws_build_frame(_WS_TEXT, text.encode("utf-8"))
            else:
                payload = message.get("bytes")
                if not isinstance(payload, bytes):
                    raise TypeError("websocket.send requires 'text' or 'bytes'")
                frame = ws_build_frame(_WS_BINARY, payload)
            self._transport_write(frame)
            await self._maybe_drain()
            return
        if message_type == "websocket.accept":
            if self._ws_accepted:
                raise RuntimeError("websocket already accepted")
            accept = base64.b64encode(hashlib.sha1(self._ws_key + _WS_GUID).digest())
            out = bytearray()
            out += b"HTTP/1.1 101 Switching Protocols\r\n"
            out += b"upgrade: websocket\r\nconnection: Upgrade\r\n"
            out += b"sec-websocket-accept: " + accept + b"\r\n"
            subprotocol = message.get("subprotocol")
            if subprotocol:
                out += b"sec-websocket-protocol: "
                out += subprotocol.encode("latin-1") + b"\r\n"
            for name, value in message.get("headers") or ():
                lname = name.lower()
                if not _valid_header_name(lname) or not _valid_header_value(value):
                    raise RuntimeError("invalid response header")
                out += lname + b": " + value + b"\r\n"
            out += b"\r\n"
            self._transport_write(bytes(out))
            self._ws_accepted = True
            self._cancel_request_timer()
            self._cancel_keep_alive_timer()
            self.state = WS_OPEN
            # Frames may already sit in the buffer behind the handshake.
            if self._cursor < len(self._buffer):
                self.loop.call_soon(self._drive)
            return
        if message_type == "websocket.close":
            if not self._ws_accepted:
                # Rejected handshake: a plain HTTP error response.
                self._send_error(403)
                self._ws_close_sent = True
                return
            if not self._ws_close_sent:
                self._ws_send_close(int(message.get("code") or 1000),
                                    str(message.get("reason") or "").encode("utf-8"))
            self._close()
            return
        raise RuntimeError(f"unexpected ASGI message: {message_type!r}")

    def _ws_send_close(self, code: int, reason: bytes) -> None:
        payload = code.to_bytes(2) + reason if code else b""
        self._transport_write(ws_build_frame(_WS_CLOSE, payload))
        self._ws_close_sent = True

    def _ws_fail(self, code: int) -> None:
        """Protocol error: send a close frame, tell the app, drop the link."""
        # This message can never complete: release the reassembly state rather
        # than holding it until the protocol object is collected.
        self._ws_frag_opcode = None
        self._ws_frag_parts = []
        self._ws_frag_size = 0
        self._ws_frag_count = 0
        if not self._ws_close_sent:
            self._ws_send_close(code, b"")
        self._ws_deliver_disconnect(code)
        self._close()

    def _ws_deliver_disconnect(self, code: int) -> None:
        self._disconnected = True
        self._receive_queue.append({"type": "websocket.disconnect", "code": code})
        self._wake_receive()

    def _drive_ws_frame(self) -> bool:
        pending = bytes(self._pending())
        if len(pending) < 2:
            return False
        try:
            parsed = ws_parse_frame(pending)
        except ValueError:
            self._ws_fail(1002)
            return False
        if parsed is None:
            if len(pending) > self.config.max_body_bytes + 14:
                self._ws_fail(1009)
            return False
        fin, opcode, payload, consumed = parsed
        if not pending[1] & 0x80:
            # Clients must mask every frame (RFC 6455 5.1).
            self._ws_fail(1002)
            return False
        self._consume(consumed)

        if opcode in (_WS_CLOSE, _WS_PING, _WS_PONG):
            if not fin or len(payload) > 125:
                self._ws_fail(1002)
                return False
            if opcode == _WS_PING:
                if not self._ws_close_sent:
                    self._transport_write(ws_build_frame(_WS_PONG, payload))
                return True
            if opcode == _WS_PONG:
                return True
            # Close frame.
            code = 1005
            if len(payload) >= 2:
                code = int.from_bytes(payload[:2])
                if code < 1000 or code in (1004, 1005, 1006, 1015) or (1015 < code < 3000):
                    self._ws_fail(1002)
                    return False
                try:
                    payload[2:].decode("utf-8")
                except UnicodeDecodeError:
                    self._ws_fail(1007)
                    return False
            elif len(payload) == 1:
                self._ws_fail(1002)
                return False
            if not self._ws_close_sent:
                self._ws_send_close(1000 if code == 1005 else code, b"")
            self._ws_deliver_disconnect(code)
            self._close()
            return False

        if opcode in (_WS_TEXT, _WS_BINARY):
            if self._ws_frag_opcode is not None:
                self._ws_fail(1002)
                return False
            if not fin:
                self._ws_frag_opcode = opcode
                self._ws_frag_parts = [payload]
                self._ws_frag_size = len(payload)
                self._ws_frag_count = 1
                if self._ws_frag_size > self.config.max_body_bytes:
                    self._ws_fail(1009)
                    return False
                return True
            return self._ws_deliver_message(opcode, payload)
        if opcode == 0x0:  # continuation
            if self._ws_frag_opcode is None:
                self._ws_fail(1002)
                return False
            # Count every continuation, empty ones included: an empty fragment
            # adds no bytes but still costs a parse and an unmask, so the byte
            # limit alone leaves the per-message work unbounded.
            self._ws_frag_count += 1
            if self._ws_frag_count > self.config.max_ws_fragments:
                self._ws_fail(1009)
                return False
            if payload:
                self._ws_frag_parts.append(payload)
                self._ws_frag_size += len(payload)
            if self._ws_frag_size > self.config.max_body_bytes:
                self._ws_fail(1009)
                return False
            if not fin:
                return True
            opcode = self._ws_frag_opcode
            data = b"".join(self._ws_frag_parts)
            self._ws_frag_opcode = None
            self._ws_frag_parts = []
            self._ws_frag_size = 0
            self._ws_frag_count = 0
            return self._ws_deliver_message(opcode, data)
        self._ws_fail(1002)  # reserved data opcode
        return False

    def _ws_deliver_message(self, opcode: int, payload: bytes) -> bool:
        if len(payload) > self.config.max_body_bytes:
            self._ws_fail(1009)
            return False
        message: Message
        if opcode == _WS_TEXT:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                self._ws_fail(1007)
                return False
            message = {"type": "websocket.receive", "text": text}
            size = len(text)
        else:
            message = {"type": "websocket.receive", "bytes": payload}
            size = len(payload)
        self._receive_queue.append(message)
        self._queued_bytes += size
        # A WebSocket message may be zero bytes: only the count bound applies.
        self._receive_pressure_pause()
        self._wake_receive()
        return True

    async def _run_ws_app(self, scope: Scope) -> None:
        try:
            await self.app(scope, self._receive, self._ws_send)
        except _Disconnect:
            self._abort()
            return
        except Exception:  # noqa: BLE001 -- connection boundary; see below
            # The ASGI application is arbitrary caller code, and one misbehaving
            # WebSocket must not stop the server. Not a swallow: the error goes to
            # the loop's exception handler *and* the peer is closed with 1011, so
            # it is visible from both ends. `CancelledError` deliberately escapes
            # -- a connection being torn down must stay torn down.
            self._log_app_error()
            if not self._ws_accepted:
                if not self._ws_close_sent:
                    self._send_error(500)
            elif not self._ws_close_sent:
                self._ws_send_close(1011, b"")
                self._close()
            return
        if not self._ws_accepted:
            # App returned without accepting: reject the handshake.
            if not self._ws_close_sent:
                self._send_error(403)
        elif not self._ws_close_sent:
            self._ws_send_close(1000, b"")
            self._close()

    # --- framing decision ---------------------------------------------------

    def _decide_framing(self, headers: list[tuple[bytes, bytes]]) -> tuple[str, int]:
        content_lengths: list[bytes] = []
        transfer_encodings: list[bytes] = []
        for name, value in headers:
            if name == b"content-length":
                content_lengths.append(value.strip())
            elif name == b"transfer-encoding":
                transfer_encodings.append(value)

        if transfer_encodings and content_lengths:
            raise _FramingError(400)

        if transfer_encodings:
            codings = b",".join(transfer_encodings).lower()
            parts = [p.strip() for p in codings.split(b",") if p.strip()]
            if not parts or parts[-1] != b"chunked":
                raise _FramingError(400)
            if any(p != b"chunked" for p in parts):
                # Only a single final chunked coding is supported.
                raise _FramingError(400)
            if parts.count(b"chunked") != 1:
                raise _FramingError(400)
            return ("chunked", 0)

        if content_lengths:
            # Accept identical duplicates; reject any conflict.
            unique = set(content_lengths)
            if len(unique) != 1:
                raise _FramingError(400)
            raw = next(iter(unique))
            if not raw or not raw.isdigit():
                raise _FramingError(400)
            length = int(raw)
            if length > self.config.max_body_bytes:
                raise _FramingError(413)
            return ("fixed", length)

        return ("none", 0)

    def _build_scope(
        self,
        method: str,
        http_version: str,
        path: str,
        raw_path: bytes,
        query_string: bytes,
        headers: list[tuple[bytes, bytes]],
    ) -> Scope:
        transport = self.transport
        assert transport is not None
        server = transport.get_extra_info("sockname")
        client = transport.get_extra_info("peername")
        scheme = "https" if transport.get_extra_info("sslcontext") is not None else "http"
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "http_version": http_version,
            "method": method,
            "scheme": scheme,
            "path": path,
            "raw_path": raw_path,
            "query_string": query_string,
            "headers": headers,
            "server": tuple(server[:2]) if server else None,
            "client": tuple(client[:2]) if client else None,
            "root_path": "",
            "extensions": _EXTENSIONS,
        }

    # --- fixed body ---------------------------------------------------------

    def _drive_fixed_body(self) -> bool:
        pending = self._pending()
        if not pending:
            return False
        take = min(self._remaining, len(pending))
        chunk = bytes(pending[:take])
        # `_consume` may compact the buffer, and a bytearray cannot be resized
        # while a memoryview export of it is alive. The copy above is already
        # made, so drop the view before consuming rather than waiting for the
        # local to fall out of scope.
        pending.release()
        self._consume(take)
        self._remaining -= take
        more = self._remaining > 0
        self._enqueue_body(chunk, more)
        if not more:
            self.state = REQUEST_RUNNING
            return False
        return True

    # --- chunked body -------------------------------------------------------

    def _drive_chunk_size(self) -> bool:
        pending = bytes(self._pending())
        line_end = pending.find(b"\r\n")
        if line_end < 0:
            if len(pending) > self.config.max_request_line:
                self._framing_error = True
                self._send_error(400)
            return False
        line = pending[:line_end]
        # Chunk extensions (";" ...) are syntactically valid and ignored.
        size_field = line.split(b";", 1)[0].strip()
        if not size_field or not _is_hex(size_field):
            self._framing_error = True
            self._send_error(400)
            return False
        try:
            size = int(size_field, 16)
        except ValueError:  # pragma: no cover - guarded by _is_hex
            self._framing_error = True
            self._send_error(400)
            return False
        self._consume(line_end + 2)
        if size == 0:
            self.state = READING_CHUNK_TRAILERS
            return True
        # Count decoded bytes against the body limit.
        if self._queued_bytes + size > self.config.max_body_bytes:
            self._framing_error = True
            self._send_error(413)
            return False
        self._chunk_remaining = size
        self.state = READING_CHUNK_DATA
        return True

    def _drive_chunk_data(self) -> bool:
        pending = self._pending()
        if not pending:
            return False
        take = min(self._chunk_remaining, len(pending))
        chunk = bytes(pending[:take])
        pending.release()  # see _drive_fixed_body: _consume may resize the buffer
        self._consume(take)
        self._chunk_remaining -= take
        if chunk:
            self._enqueue_body(chunk, True)
        if self._chunk_remaining == 0:
            # Expect the CRLF that terminates the chunk data.
            trailer = bytes(self._pending()[:2])
            if len(trailer) < 2:
                return False
            if trailer != b"\r\n":
                self._framing_error = True
                self._send_error(400)
                return False
            self._consume(2)
            self.state = READING_CHUNK_SIZE
        return True

    def _drive_chunk_trailers(self) -> bool:
        pending = bytes(self._pending())
        end = pending.find(b"\r\n")
        if end < 0:
            if len(pending) > self.config.max_header_bytes:
                self._framing_error = True
                self._send_error(431)
            return False
        if end == 0:
            # Empty line: trailers finished.
            self._consume(2)
            self._enqueue_body(b"", False)
            self.state = REQUEST_RUNNING
            return False
        # A trailer line: validate under limits but do not expose it.
        line_end = pending.find(b"\r\n\r\n")
        if line_end < 0:
            if len(pending) > self.config.max_header_bytes:
                self._framing_error = True
                self._send_error(431)
            return False
        # Consume all trailer lines at once via the terminating blank line.
        self._consume(line_end + 4)
        self._enqueue_body(b"", False)
        self.state = REQUEST_RUNNING
        return False

    # --- receive plumbing ---------------------------------------------------

    def _receive_pressure_pause(self) -> None:
        """Apply both receive watermarks after the queue grew.

        A queued message may carry zero payload bytes (an empty WebSocket
        message, an empty chunk), so the byte watermark alone cannot bound the
        queue. Either bound may pause reading.
        """
        if self._reading_paused or self.transport is None:
            return
        if (
            self._queued_bytes > self.config.read_high_water
            or len(self._receive_queue) >= self.config.read_high_water_messages
        ):
            self.transport.pause_reading()
            self._reading_paused = True

    def _receive_pressure_resume(self) -> None:
        """Resume only when both measures are at or below half their marks."""
        if not self._reading_paused:
            return
        if (
            self._queued_bytes <= self.config.read_high_water // 2
            and len(self._receive_queue) <= self.config.read_high_water_messages // 2
        ):
            if self.transport is not None:
                self.transport.resume_reading()
            self._reading_paused = False

    def _enqueue_body(self, body: bytes, more: bool) -> None:
        self._receive_queue.append({"type": "http.request", "body": body, "more_body": more})
        self._queued_bytes += len(body)
        if not more:
            self._request_more_body = False
        self._receive_pressure_pause()
        self._wake_receive()

    def _deliver_disconnect(self) -> None:
        if self._ws_mode:
            # 1006: abnormal closure, no close frame from the peer.
            self._receive_queue.append({"type": "websocket.disconnect", "code": 1006})
        else:
            self._receive_queue.append({"type": "http.disconnect"})
        self._wake_receive()

    def _disconnect_message(self) -> Message:
        if self._ws_mode:
            return {"type": "websocket.disconnect", "code": 1006}
        return {"type": "http.disconnect"}

    def _wake_receive(self) -> None:
        waiter = self._receive_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def _receive(self) -> Message:
        if self._receive_queue:
            message = self._receive_queue.popleft()
        else:
            if self._disconnected:
                return self._disconnect_message()
            waiter = self.loop.create_future()
            self._receive_waiter = waiter
            try:
                await waiter
            finally:
                self._receive_waiter = None
            if not self._receive_queue:
                # Woken with nothing queued. Every current waker enqueues before
                # it wakes, so this is a guard rather than a live path -- but an
                # ASGI `receive` must return a message, and popping an empty
                # deque here would surface as an `IndexError` out of the
                # application instead of a disconnect. Synthesising the
                # disconnect keeps the contract total.
                return self._disconnect_message()
            message = self._receive_queue.popleft()

        message_type = message["type"]
        if message_type == "http.request":
            self._queued_bytes -= len(message.get("body", b""))
        elif message_type == "websocket.receive":
            payload = message.get("bytes")
            self._queued_bytes -= len(payload) if payload is not None else len(
                message.get("text") or ""
            )
        else:
            return message
        self._receive_pressure_resume()
        return message

    # --- app task -----------------------------------------------------------

    async def _run_app(self, scope: Scope) -> None:
        try:
            await self.app(scope, self._receive, self._send)
        except _Disconnect:
            self._abort()
            return
        except Exception:  # noqa: BLE001 -- connection boundary; see below
            # Application error. Emit a minimal 500 only if nothing was sent.
            #
            # The ASGI application is arbitrary caller code, and one bad request
            # must not stop the server. Not a swallow: the error reaches the
            # loop's exception handler *and* the peer gets a 500 (or the response
            # is aborted if one had already started), so it is visible from both
            # ends. `CancelledError` deliberately escapes -- a request being
            # unwound must stay unwound.
            if not self._response_started:
                self._write_error(500)
                self._finish_response(keep_alive=False)
            else:
                self._abort()
            self._log_app_error()
            return

        if not self._response_started:
            # App returned without producing a response.
            self._write_error(500)
            self._finish_response(keep_alive=False)
            return
        if not self._response_complete:
            # App finished without a terminal body message.
            self._finish_response(keep_alive=False)

    def _log_app_error(self) -> None:
        self.loop.call_exception_handler(
            {"message": "Exception in ASGI application", "exception": _current_exc()}
        )

    # --- send plumbing ------------------------------------------------------

    async def _send(self, message: Message) -> None:
        if self._disconnected and self._response_started:
            # Peer is gone mid-response; unwind the app.
            raise _Disconnect
        message_type = message["type"]
        if message_type == "wreath.response":
            # One-shot response extension: status, headers, and the complete
            # body in a single message.  Framing and validation are identical
            # to a start+body pair; only the message traffic is halved.
            if self._response_started:
                raise RuntimeError("response already started")
            self._begin_response(message)
            await self._write_body(message)
        elif message_type == "http.response.start":
            if self._response_started:
                raise RuntimeError("response already started")
            self._begin_response(message)
        elif message_type == "http.response.body":
            if not self._response_started:
                raise RuntimeError("body before response start")
            if self._response_complete:
                raise RuntimeError("body after response completed")
            await self._write_body(message)
        else:
            raise RuntimeError(f"unexpected ASGI message: {message_type!r}")

    def _reset_response(
        self, method: str, http_version: str, headers: list[tuple[bytes, bytes]]
    ) -> None:
        self._response_started = False
        self._response_complete = False
        self._response_body_sent = 0
        self._response_content_length = None
        self._response_chunked = False
        self._response_suppress_body = method in _HOP_BODY_METHODS
        self._head_written = False
        self._resp_status = 200
        self._resp_headers = []
        self._framing_error = False
        # Determine whether the peer permits keep-alive.
        conn = b""
        for name, value in headers:
            if name == b"connection":
                conn = value.lower()
                break
        if http_version == "1.1":
            self._response_keep_alive = b"close" not in conn
        else:
            self._response_keep_alive = b"keep-alive" in conn

    def _begin_response(self, message: Message) -> None:
        status = int(message["status"])
        headers = list(message.get("headers", []))
        self._response_started = True

        if status < 200 or status in _STATUS_NO_BODY:
            self._response_suppress_body = True

        content_length: int | None = None
        filtered: list[tuple[bytes, bytes]] = []
        for name, value in headers:
            lname = name.lower()
            if not _valid_header_name(lname) or not _valid_header_value(value):
                raise RuntimeError("invalid response header")
            if lname in (b"content-length", b"transfer-encoding", b"connection"):
                if lname == b"content-length":
                    try:
                        content_length = int(value)
                    except ValueError as exc:
                        raise RuntimeError("invalid content-length") from exc
                # Server owns framing/connection headers.
                continue
            filtered.append((lname, value))

        # The head is written lazily on the first body message so a
        # non-streaming response can carry an exact content-length.
        self._resp_status = status
        self._resp_headers = filtered
        self._response_content_length = content_length
        self._head_written = False

    def _decide_framing_and_write_head(self, first_body: bytes, streaming: bool) -> None:
        content_length = self._response_content_length
        keep_alive = self._response_keep_alive
        if self._response_suppress_body:
            self._response_chunked = False
        elif content_length is not None:
            self._response_chunked = False
        elif not streaming:
            # Complete non-streaming response: supply an exact length.
            content_length = len(first_body)
            self._response_content_length = content_length
            self._response_chunked = False
        elif self._http_version == "1.1":
            self._response_chunked = True
        else:
            # HTTP/1.0 streaming without a length: close-framed.
            self._response_chunked = False
            keep_alive = False
        self._response_keep_alive = keep_alive
        self._write_response_head(self._resp_status, self._resp_headers, content_length)
        self._head_written = True

    def _write_response_head(
        self, status: int, headers: list[tuple[bytes, bytes]], content_length: int | None
    ) -> None:
        out = bytearray()
        out += b"HTTP/1.1 " if self._http_version == "1.1" else b"HTTP/1.0 "
        out += str(status).encode("ascii")
        out += b" "
        out += _reason(status)
        out += b"\r\n"
        supplied = {name.lower() for name, _ in headers}
        for name, value in headers:
            out += name
            out += b": "
            out += value
            out += b"\r\n"
        for name, value in self.config._default_response_headers.headers:
            if name not in supplied:
                out += name
                out += b": "
                out += value
                out += b"\r\n"
        if content_length is not None:
            out += b"content-length: " + str(content_length).encode() + b"\r\n"
        elif self._response_chunked and not self._response_suppress_body:
            out += b"transfer-encoding: chunked\r\n"
        out += (
            b"connection: keep-alive\r\n"
            if self._response_keep_alive
            else b"connection: close\r\n"
        )
        out += b"\r\n"
        self._transport_write(bytes(out))

    async def _write_body(self, message: Message) -> None:
        body = message.get("body", b"")
        more = message.get("more_body", False)
        if not self._head_written:
            self._decide_framing_and_write_head(body, streaming=more)
        if body and not self._response_suppress_body:
            if self._response_content_length is not None:
                self._response_body_sent += len(body)
                if self._response_body_sent > self._response_content_length:
                    raise RuntimeError("response body exceeds content-length")
                self._transport_write(body)
            elif self._response_chunked:
                self._transport_write(f"{len(body):x}".encode() + b"\r\n" + body + b"\r\n")
            else:
                self._transport_write(body)
        if not more:
            if (
                self._response_content_length is not None
                and not self._response_suppress_body
                and self._response_body_sent != self._response_content_length
            ):
                # Short body: framing is now ambiguous, force close.
                self._response_keep_alive = False
            if self._response_chunked and not self._response_suppress_body:
                self._transport_write(b"0\r\n\r\n")
            self._finish_response(keep_alive=self._response_keep_alive)
        else:
            await self._maybe_drain()

    def _maybe_drain(self) -> Awaitable[None]:
        if not self._write_paused:
            return _completed_future(self.loop)
        waiter = self._drain_waiter
        if waiter is None:
            waiter = self._drain_waiter = self.loop.create_future()
        return waiter

    def _resolve_drain(self) -> None:
        waiter = self._drain_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)
        self._drain_waiter = None

    # --- response completion ------------------------------------------------

    def _finish_response(self, keep_alive: bool) -> None:
        if self._response_complete:
            return
        if self._response_started and not self._head_written:
            # Response started but produced no body message: flush the head.
            self._decide_framing_and_write_head(b"", streaming=False)
            keep_alive = False
        self._response_complete = True
        self._cancel_request_timer()
        if self._framing_error:
            keep_alive = False
        if not keep_alive or self._closing or not self._accepting or self._disconnected:
            self._close()
            return
        self._reset_request()
        self.state = READING_HEAD
        self._start_keep_alive_timer()
        # Process any pipelined bytes on the next loop iteration. Draining them
        # re-entrantly here would corrupt the response state that the just
        # finished app task still inspects as it unwinds.
        if self._cursor < len(self._buffer):
            self.loop.call_soon(self._drive)

    def _reset_request(self) -> None:
        self._task = None
        self._receive_queue = deque()
        self._receive_waiter = None
        self._queued_bytes = 0
        self._remaining = 0
        self._chunk_remaining = 0
        self._head_scan = 0
        self._line_scan = 0
        self._line_end = -1
        if self._reading_paused and self.transport is not None:
            self.transport.resume_reading()
            self._reading_paused = False

    # --- errors and closing -------------------------------------------------

    def _send_error(self, status: int) -> None:
        """Emit a minimal error response and close. Never calls the app."""
        if self._response_started:
            self._close()
            return
        self._http_version = self._http_version or "1.1"
        self._write_error(status)
        self._close()

    def _write_error(self, status: int) -> None:
        reason = _reason(status)
        body = reason + b"\n"
        head = bytearray()
        head += b"HTTP/1.1 " if self._http_version == "1.1" else b"HTTP/1.0 "
        head += str(status).encode("ascii") + b" " + reason + b"\r\n"
        head += b"content-type: text/plain; charset=utf-8\r\n"
        head += b"content-length: " + str(len(body)).encode() + b"\r\n"
        for name, value in self.config._default_response_headers.headers:
            head += name + b": " + value + b"\r\n"
        head += b"connection: close\r\n\r\n"
        if self._request_method == "HEAD":
            body = b""
        self._transport_write(bytes(head) + body)

    def _transport_write(self, data: bytes) -> None:
        if self.transport is not None and not self._closing:
            self.transport.write(data)

    def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.state = CLOSING
        self._cancel_request_timer()
        self._cancel_keep_alive_timer()
        if self.transport is not None:
            self.transport.close()

    def _abort(self) -> None:
        if self.transport is not None:
            self.transport.abort()
        self._close()

    # --- timers -------------------------------------------------------------

    def _start_keep_alive_timer(self) -> None:
        self._cancel_keep_alive_timer()
        timeout = self.config.keep_alive_timeout
        if timeout and timeout > 0:
            self._keep_alive_timer = self.loop.call_later(timeout, self._on_keep_alive_timeout)

    def _cancel_keep_alive_timer(self) -> None:
        if self._keep_alive_timer is not None:
            self._keep_alive_timer.cancel()
            self._keep_alive_timer = None

    def _start_request_timer(self) -> None:
        self._cancel_request_timer()
        timeout = self.config.request_timeout
        if timeout and timeout > 0:
            self._request_timer = self.loop.call_later(timeout, self._on_request_timeout)

    def _cancel_request_timer(self) -> None:
        if self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None

    def _on_keep_alive_timeout(self) -> None:
        self._keep_alive_timer = None
        # Idle between requests: close without invoking the application.
        self._close()

    def _on_request_timeout(self) -> None:
        self._request_timer = None
        if self._response_started:
            self._abort()
            return
        self._send_error(408)

    def _replay_fire_timeout(self) -> None:
        """Replay/test only: fire the currently-armed timeout's owned handler,
        bypassing the clock so a virtual-clock TIMEOUT fault is deterministic.
        Mirrors the native ``_replay_fire_timeout``."""
        if self._request_timer is not None:
            self._cancel_request_timer()
            self._on_request_timeout()
        elif self._keep_alive_timer is not None:
            self._cancel_keep_alive_timer()
            self._on_keep_alive_timeout()

    # --- graceful shutdown hook (called by the facade) ----------------------

    def stop_accepting(self) -> None:
        self._accepting = False
        if self.state == READING_HEAD and self._task is None:
            self._close()

    def shutdown(self) -> None:
        self._close()


class _FramingError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


def _is_hex(data: bytes) -> bool:
    return len(data) > 0 and all(c in b"0123456789abcdefABCDEF" for c in data)


_HEADER_NAME_BAD = frozenset(b":\r\n\0 \t")


def _valid_header_name(name: bytes) -> bool:
    return bool(name) and not any(c in _HEADER_NAME_BAD or c < 0x20 for c in name)


def _valid_header_value(value: bytes) -> bool:
    return not any(c in (0x00, 0x0A, 0x0D) for c in value)


def _completed_future(loop: asyncio.AbstractEventLoop) -> asyncio.Future[None]:
    fut: asyncio.Future[None] = loop.create_future()
    fut.set_result(None)
    return fut


def _current_exc() -> BaseException | None:
    return sys.exc_info()[1]


# Http1Protocol is the canonical name; HttpProtocol is retained as an alias so
# the native and pure implementations expose identical names.
Http1Protocol = HttpProtocol
