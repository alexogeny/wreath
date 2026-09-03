"""WebSocket connection API for route handlers.

A handler registered with `@app.websocket(path)` receives one
`WebSocket` per connection. The server has already validated the
upgrade handshake; the handler decides whether to `WebSocket.accept`
or `WebSocket.close` it, then exchanges messages:

```python
@app.websocket("/echo")
async def echo(ws: WebSocket) -> None:
    await ws.accept()
    async for message in ws:
        await ws.send(message)
```

Iteration ends when the peer disconnects. Explicit `receive_text()` /
`receive_bytes()` raise `WebSocketDisconnect` instead.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from time import monotonic_ns
from typing import Any, Literal

from ._correlation import Pending
from ._flight_markers import COV_PYTHON as _COV_PYTHON
from ._flight_markers import PH_WS_FANOUT as _PH_WS_FANOUT
from ._flight_markers import phase_marker as _phase_marker
from ._headers import find_header
from ._json import dumps as _json_dumps
from ._json import loads as _json_loads
from .temporal import Duration


def _json_text(payload: Any) -> str | bytes:
    """The default outgoing codec: JSON text, which is what most peers speak."""
    return _json_dumps(payload)


def _json_value(frame: str | bytes) -> Any:
    """The default incoming codec, the inverse of `_json_text`."""
    return _json_loads(frame)


Message = dict[str, Any]

#: Close codes an endpoint may put in an outgoing Close frame (RFC 6455 §7.4.1):
#: the assigned 1000-range codes, excluding 1004 (reserved) and 1005/1006/1015
#: (which MUST NOT appear on the wire). 3000-4999 are registered/private-use.
_SENDABLE_CLOSE_CODES = frozenset(
    {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014}
)


def _valid_close_code(code: int) -> bool:
    return code in _SENDABLE_CLOSE_CODES or 3000 <= code <= 4999


class WebSocketDisconnect(Exception):
    """The peer closed or dropped the connection.

    Raised by `WebSocket.receive_text` and `WebSocket.receive_bytes`.
    Iterating a socket does *not* raise it -- iteration stops instead.

    Args:
        code: The peer's close code, or 1006 when it left without a Close frame.
    """

    def __init__(self, code: int = 1006) -> None:
        super().__init__(code)
        self.code = code


class WebSocket:
    """One WebSocket connection, backed directly by the ASGI scope.

    Constructed by the framework, one per connection, and handed to the
    `@app.websocket(path)` handler. The server has already validated the
    upgrade handshake and the route's authentication; what is left to the
    handler is to `accept` or `close`.

    Nothing may be sent before `accept` -- the ASGI server rejects a
    `websocket.send` on an unaccepted connection. The first receive of any kind
    consumes the `websocket.connect` message, so `accept`, `close`,
    and `receive` are each safe as the first call.

    The socket is its own async iterator: `async for message in ws` yields
    `str` for text frames and `bytes` for binary ones, and stops -- rather
    than raising -- when the peer disconnects. A handler that must distinguish a
    clean close from a dropped connection reads the code off
    `WebSocketDisconnect` from an explicit receive instead.

    This class holds no lock and assumes one reader and one writer. Two tasks
    receiving on the same socket interleave frames unpredictably.

    Args:
        path_params: Route captures; `{}` when the route has none.
        identity: The caller when the route declared auth; None on an open route.
    """

    __slots__ = (
        "_accepted",
        "_connected",
        "_receive",
        "_send",
        "identity",
        "path_params",
        "scope",
    )

    def __init__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        path_params: dict[str, str] | None = None,
        *,
        identity: Any = None,
    ) -> None:
        self.scope = scope
        self._receive = receive
        self._send = send
        self.path_params = path_params or {}
        #: The caller, when the route declared an auth requirement the
        #: application enforced before the handshake; None on an open route.
        self.identity = identity
        self._connected = False
        self._accepted = False

    @property
    def path(self) -> str:
        """The percent-decoded request path from the handshake."""
        return self.scope["path"]

    @property
    def query_string(self) -> bytes:
        """The raw, still percent-encoded query string; `b""` when there is none."""
        return self.scope.get("query_string", b"")

    @property
    def subprotocols(self) -> list[str]:
        """Subprotocols the client offered, in its order of preference.

        Advisory: nothing checks that the value passed to `accept` came
        from this list. Selecting one the client did not offer is a protocol
        violation the peer is entitled to fail the connection over.
        """
        try:
            return self.scope["subprotocols"]
        except KeyError:
            return []

    @property
    def headers(self) -> list[tuple[bytes, bytes]]:
        """Handshake headers as raw `(name, value)` byte pairs, names lowercased."""
        try:
            return self.scope["headers"]
        except KeyError:
            return []

    def header(self, name: str | bytes, default: str | None = None) -> str | None:
        """One handshake header, decoded latin-1, or `default` when absent.

        `name` is lowercased before the lookup, and ASGI guarantees the scope's
        header names are already lowercase, so case does not matter. A header
        sent more than once resolves to the first occurrence; read
        `headers` directly for the rest.

        Args:
            name: Header name, `str` or `bytes`; case does not matter.
            default: Returned when the header is absent. Not encoded or validated.
        """
        target = name.encode("latin-1") if isinstance(name, str) else name
        value = find_header(self.headers, target.lower())
        return value.decode("latin-1") if value is not None else default

    async def _ensure_connect(self) -> None:
        if self._connected:
            return
        message = await self._receive()
        if message["type"] != "websocket.connect":
            raise RuntimeError(f"expected websocket.connect, got {message['type']!r}")
        self._connected = True

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        """Complete the handshake. Nothing may be sent before this returns.

        Consumes the `websocket.connect` message if it has not been consumed
        already, then sends `websocket.accept`. Calling it twice is a protocol
        error the server raises on, not something this class guards.

        Args:
            subprotocol: One value for `Sec-WebSocket-Protocol`; None selects none.
            headers: Extra response headers as raw byte pairs, e.g. a `Set-Cookie`.

        Raises:
            RuntimeError: The first message from the peer was not `websocket.connect`.
        """
        if subprotocol is not None and subprotocol not in self.subprotocols:
            raise ValueError(f"WebSocket subprotocol {subprotocol!r} was not offered by the client")
        await self._ensure_connect()
        message: Message = {"type": "websocket.accept", "subprotocol": subprotocol}
        if headers:
            message["headers"] = headers
        await self._send(message)
        self._accepted = True

    async def receive(self) -> Message:
        """The next raw ASGI message (`websocket.receive` or `.disconnect`).

        The escape hatch below the typed helpers: it never raises on disconnect,
        so a handler that wants to inspect the close code, or to accept text and
        binary in one place, reads the dict itself.

        Returns:
            The ASGI message dict, unmodified.

        Raises:
            RuntimeError: The first message from the peer was not `websocket.connect`.
        """
        await self._ensure_connect()
        return await self._receive()

    async def receive_text(self) -> str:
        """The next message, which must be a text frame.

        Strict about the frame type: a binary frame arriving here raises rather
        than being decoded, because a peer sending the wrong opcode is a bug
        worth surfacing. The offending message is consumed and discarded either
        way -- use `receive` to accept both kinds in one place.

        Raises:
            WebSocketDisconnect: The peer closed or dropped; `.code` carries the close code.
            RuntimeError: The peer sent a binary frame.
        """
        message = await self.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1006))
        text = message.get("text")
        if text is None:
            raise RuntimeError("expected a text message, received binary")
        return text

    async def receive_bytes(self) -> bytes:
        """The next message, which must be a binary frame.

        The mirror of `receive_text`, and equally strict: a text frame
        raises rather than being encoded.

        Raises:
            WebSocketDisconnect: The peer closed or dropped; `.code` carries the close code.
            RuntimeError: The peer sent a text frame.
        """
        message = await self.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1006))
        payload = message.get("bytes")
        if payload is None:
            raise RuntimeError("expected a binary message, received text")
        return payload

    async def receive_json(self) -> Any:
        """Decode the next text or binary frame as JSON."""
        message = await self.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1006))
        frame = message.get("text")
        if frame is None:
            frame = message["bytes"]
        return _json_loads(frame)

    async def send(self, data: str | bytes) -> None:
        """Send one message, framed by type: `str` as text, anything else as binary.

        The dispatch is on `str` alone. A `bytearray` or `memoryview` takes the
        binary branch and is then refused by the server, which requires exact
        `bytes` -- convert before sending. Sending before `accept` is also
        the server's refusal, not this method's.

        Args:
            data: `str` is encoded UTF-8 by the server; `bytes` is sent verbatim.
        """
        if isinstance(data, str):
            await self._send({"type": "websocket.send", "text": data})
        else:
            await self._send({"type": "websocket.send", "bytes": data})

    async def send_text(self, data: str) -> None:
        """Send a text frame. Explicit about the frame type, unlike `send`."""
        await self._send({"type": "websocket.send", "text": data})

    async def send_bytes(self, data: bytes) -> None:
        """Send a binary frame. Explicit about the frame type, unlike `send`."""
        await self._send({"type": "websocket.send", "bytes": data})

    async def send_json(self, data: Any) -> None:
        """Encode `data` with Wreath's JSON codec and send one text frame."""
        payload = _json_dumps(data)
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        await self.send_text(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the connection with a Close frame carrying `code`.

        Valid before `accept` too, where it rejects the handshake instead:
        the server answers the upgrade with a plain HTTP error rather than a
        Close frame, so `code` and `reason` are not seen by the peer.

        Only codes an endpoint may put on the wire are accepted (RFC 6455
        §7.4.1): the assigned 1000-range values except 1004, 1005, 1006 and
        1015, plus the 3000-4999 registered and private-use range. The three
        excluded ones exist to *describe* a closure locally and MUST NOT be
        sent, which is why they are refused here rather than forwarded.

        Args:
            code: Close code. 1000 is normal closure; 1011 reports an internal error.
            reason: UTF-8 text for a human; peers are not required to surface it.

        Raises:
            ValueError: `code` is not a code an endpoint may send.
        """
        if not _valid_close_code(code):
            raise ValueError(
                f"invalid WebSocket close code {code!r}: send an assigned 1000-range "
                "code (not 1004/1005/1006/1015) or a 3000-4999 application code "
                "(RFC 6455 7.4.1)"
            )
        await self._ensure_connect()
        await self._send({"type": "websocket.close", "code": code, "reason": reason})

    def __aiter__(self) -> WebSocket:
        return self

    async def __anext__(self) -> str | bytes:
        message = await self.receive()
        if message["type"] == "websocket.disconnect":
            raise StopAsyncIteration
        text = message.get("text")
        if text is not None:
            return text
        return message["bytes"]


class Calls:
    """Request/response over one socket, correlated by an identifier.

    A WebSocket is a frame pipe: frames arrive in order and nothing pairs a
    reply with the request that caused it. Every protocol that puts a
    request/response contract on top of one -- and most do -- grows the same
    three things by hand: an identifier on the way out, a map of what is
    outstanding, and a deadline so a peer that never answers does not pin a
    caller forever.

    `Calls` owns the read loop and demultiplexes. Both directions are declared
    once, as functions over decoded messages, so the protocol lives in one place
    instead of being spread through the handler:

    ```python
    calls = Calls(
        ws,
        reply_to=lambda message: message.get("reply_to"),
        label=lambda identifier, payload: {"id": identifier, **payload},
    )

    @calls.on_request
    async def handle(message: dict) -> dict | None:
        return {"reply_to": message["id"], "ok": True}

    async with calls:
        answer = await calls.call({"op": "read"}, timeout=seconds(30))
    ```

    `reply_to` returns the identifier a frame is answering, or `None` when the
    frame is a peer-initiated request; `label` stamps an outgoing request with
    the identifier a reply will carry back. Those two are the whole protocol
    seam, and neither has a default: a guessed correlation field is a protocol
    this class does not know it is implementing.

    **One reader.** `WebSocket` documents that it holds no lock and assumes one
    reader; this *is* that reader, so a handler using `Calls` must not also
    iterate the socket. Sends are serialised by a lock, because a request and a
    reply genuinely do race.

    On close, every outstanding question fails with `CallsClosed` rather than
    waiting out its own deadline -- an answer that provably cannot arrive should
    not cost a caller its timeout.
    """

    __slots__ = (
        "_decode",
        "_encode",
        "_label",
        "_lock",
        "_on_request",
        "_pending",
        "_reply_to",
        "_task",
        "_timeout",
        "_ws",
    )

    def __init__(
        self,
        ws: WebSocket,
        *,
        reply_to: Callable[[Any], str | None],
        label: Callable[[str, Any], Any],
        encode: Callable[[Any], str | bytes] = _json_text,
        decode: Callable[[str | bytes], Any] = _json_value,
        timeout: Any = 30.0,
        max_pending: int = 256,
    ) -> None:
        self._ws = ws
        self._reply_to = reply_to
        self._label = label
        self._encode = encode
        self._pending = Pending(limit=max_pending)
        self._timeout = Duration.of(timeout).total_seconds()
        self._on_request: Callable[[Any], Awaitable[Any]] | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._decode = decode

    @property
    def refusals(self) -> int:
        """Calls refused because too many were already outstanding."""
        return self._pending.refusals

    @property
    def outstanding(self) -> int:
        """Calls awaiting an answer right now."""
        return len(self._pending)

    def on_request(
        self, handler: Callable[[Any], Awaitable[Any]]
    ) -> Callable[[Any], Awaitable[Any]]:
        """Register the handler for peer-initiated frames.

        Returning a value sends it; returning `None` sends nothing, which is
        what a notification -- a request with no response -- means.
        """
        if self._on_request is not None:
            raise ValueError("this Calls already has a request handler")
        self._on_request = handler
        return handler

    async def __aenter__(self) -> Calls:
        self._task = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Stop reading and fail every outstanding call."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._pending.fail_all(CallsClosed("the socket closed with calls outstanding"))

    async def send(self, payload: Any) -> None:
        """Send one frame, serialised against every other sender."""
        async with self._lock:
            await self._ws.send(self._encode(payload))

    async def call(
        self,
        payload: Any,
        *,
        # ASYNC109 wants the caller to wrap this in `asyncio.timeout`. For a
        # request/response the deadline is part of the protocol -- it is what
        # frees the correlation slot -- and a caller who forgot to wrap would
        # hold one until the socket closed.
        timeout: Any = None,  # noqa: ASYNC109
    ) -> Any:
        """Send `payload` as a request and wait for the reply that names it.

        Raises `TimeoutError` when the deadline passes and `CallsClosed` when
        the socket goes away first.
        """
        deadline = self._timeout if timeout is None else Duration.of(timeout).total_seconds()
        async with self._pending.slot() as (identifier, waiter):
            await self.send(self._label(identifier, payload))
            async with asyncio.timeout(deadline):
                return await waiter

    async def _read(self) -> None:
        try:
            async for frame in self._ws:
                try:
                    message = self._decode(frame)
                except Exception:  # noqa: BLE001 - a peer's bad frame is not ours
                    # A malformed frame must not end the loop: the socket is
                    # still live and the calls outstanding on it are still
                    # answerable. A protocol that wants to close on garbage can
                    # do it from `on_request`.
                    continue
                identifier = self._reply_to(message)
                if identifier is not None:
                    # False here is the ordinary case, not an error: a reply to
                    # a call that already timed out has nobody waiting.
                    self._pending.settle(identifier, message)
                    continue
                if self._on_request is not None:
                    reply = await self._on_request(message)
                    if reply is not None:
                        await self.send(reply)
        except asyncio.CancelledError:
            raise
        except WebSocketDisconnect:
            pass
        finally:
            self._pending.fail_all(CallsClosed("the socket closed with calls outstanding"))


class CallsClosed(Exception):
    """The socket went away while a call was outstanding."""


class ConnectionBackpressure(Exception):
    """A managed connection could not accept another outbound frame."""


class Heartbeat:
    """Protocol-supplied heartbeat frame and acknowledgement contract.

    Wreath does not guess an application subprotocol. The caller supplies the
    frame and the predicate that recognizes its answer; the service supplies
    bounded scheduling, timeout, and closure.
    """

    __slots__ = ("acknowledge", "consume", "frame", "interval", "timeout")

    def __init__(
        self,
        *,
        frame: str | bytes,
        acknowledge: Callable[[str | bytes], bool],
        interval: Any = 30.0,
        timeout: Any = 10.0,
        consume: bool = True,
    ) -> None:
        resolved_interval = Duration.of(interval).total_seconds()
        resolved_timeout = Duration.of(timeout).total_seconds()
        if resolved_interval <= 0 or resolved_timeout <= 0:
            raise ValueError("heartbeat interval and timeout must be positive")
        self.frame = frame
        self.acknowledge = acknowledge
        self.interval = resolved_interval
        self.timeout = resolved_timeout
        self.consume = consume


@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:
    """Point-in-time operational counters for one connection service."""

    active: int
    queued: int
    accepted: int
    closed: int
    capacity_refusals: int
    queue_refusals: int
    protocol_refusals: int
    heartbeat_timeouts: int
    drain_timeouts: int


@dataclass(frozen=True, slots=True)
class _Close:
    code: int
    reason: str


_Frame = str | bytes
_Outbound = _Frame | _Close


class _ManagedConnection:
    __slots__ = (
        "done",
        "closed",
        "heartbeat_ack",
        "key",
        "queue",
        "send_task",
        "service",
        "stopping",
        "websocket",
        "write_lock",
    )

    def __init__(
        self,
        service: WebSocketService,
        key: Hashable,
        websocket: WebSocket,
    ) -> None:
        self.service = service
        self.key = key
        self.websocket = websocket
        self.queue: asyncio.Queue[_Outbound] = asyncio.Queue(maxsize=service.queue_capacity)
        self.send_task: asyncio.Task[None] | None = None
        self.write_lock = asyncio.Lock()
        self.done = asyncio.Event()
        self.closed = False
        self.stopping = asyncio.Event()
        self.heartbeat_ack = asyncio.Event()

    async def close(self, code: int, reason: str) -> None:
        async with self.write_lock:
            if self.closed:
                return
            self.closed = True
            await self.websocket.close(code, reason)

    async def write(self, frame: _Frame) -> None:
        marker = _phase_marker.get(None)
        started = monotonic_ns() if marker is not None else 0
        async with self.write_lock:
            if self.closed:
                return
            await self.websocket.send(frame)
        if marker is not None:
            marker(
                _PH_WS_FANOUT,
                self.queue.qsize(),
                _COV_PYTHON,
                monotonic_ns() - started,
            )


class WebSocketService:
    """Bounded lifecycle and outbound flow control for long-lived sockets.

    The service is protocol-neutral. Applications supply a message handler and
    optional connection key; Wreath supplies capacity admission, one bounded
    outbound queue per connection, serialized writes, shutdown draining, and
    counters. Register it with `app.service(name, service)` and delegate a
    route to `serve`.

    `overflow` is explicit because all three useful policies have different
    operational meanings:

    * `"reject"` refuses the producer immediately;
    * `"backpressure"` waits for queue space, bounded by `enqueue_timeout`;
    * `"disconnect"` closes a peer that cannot keep up, then refuses the send.

    No mode grows memory with traffic. The default is rejection, which keeps a
    slow peer from delaying an unrelated producer.
    """

    __slots__ = (
        "_accepted",
        "_accepting",
        "_capacity_refusals",
        "_closed",
        "_connections",
        "_drain_timeouts",
        "_enqueue_timeout",
        "_heartbeat",
        "_heartbeat_timeouts",
        "_overflow",
        "_protocol_refusals",
        "_queue_refusals",
        "max_connections",
        "queue_capacity",
    )

    def __init__(
        self,
        *,
        max_connections: int = 1024,
        queue_capacity: int = 64,
        overflow: Literal["reject", "backpressure", "disconnect"] = "reject",
        enqueue_timeout: Any = 5.0,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        if max_connections < 1:
            raise ValueError("max_connections must be at least one")
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be at least one")
        if overflow not in {"reject", "backpressure", "disconnect"}:
            raise ValueError("overflow must be 'reject', 'backpressure', or 'disconnect'")
        timeout = Duration.of(enqueue_timeout).total_seconds()
        if timeout <= 0:
            raise ValueError("enqueue_timeout must be positive")
        self.max_connections = max_connections
        self.queue_capacity = queue_capacity
        self._overflow = overflow
        self._enqueue_timeout = timeout
        self._heartbeat = heartbeat
        self._connections: dict[Hashable, _ManagedConnection] = {}
        self._accepting = False
        self._accepted = 0
        self._closed = 0
        self._capacity_refusals = 0
        self._queue_refusals = 0
        self._protocol_refusals = 0
        self._heartbeat_timeouts = 0
        self._drain_timeouts = 0

    async def start(self, supervisor: Any) -> None:
        """Open admission when the application lifespan starts."""
        if self._connections:
            raise RuntimeError("cannot restart a WebSocketService with active connections")
        self._accepting = True

    async def drain(self, deadline: float) -> None:
        """Stop admission, close every peer, and wait until the deadline."""
        self._accepting = False
        connections = tuple(self._connections.values())
        if not connections:
            return
        await asyncio.gather(
            *(connection.close(1001, "service shutting down") for connection in connections)
        )
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            self._drain_timeouts += len(connections)
            return
        try:
            async with asyncio.timeout(remaining):
                await asyncio.gather(*(connection.done.wait() for connection in connections))
        except TimeoutError:
            self._drain_timeouts += sum(not connection.done.is_set() for connection in connections)

    @property
    def snapshot(self) -> ConnectionSnapshot:
        """Current gauges and cumulative refusal/closure counters."""
        return ConnectionSnapshot(
            active=len(self._connections),
            queued=sum(connection.queue.qsize() for connection in self._connections.values()),
            accepted=self._accepted,
            closed=self._closed,
            capacity_refusals=self._capacity_refusals,
            queue_refusals=self._queue_refusals,
            protocol_refusals=self._protocol_refusals,
            heartbeat_timeouts=self._heartbeat_timeouts,
            drain_timeouts=self._drain_timeouts,
        )

    async def serve(
        self,
        websocket: WebSocket,
        handler: Callable[[_Frame], Awaitable[_Frame | None]],
        *,
        key: Hashable | None = None,
        subprotocol: str | None = None,
    ) -> None:
        """Own one connection until the peer leaves or the handler fails.

        The handler is called sequentially, so inbound work has natural
        backpressure. A returned frame enters the same bounded outbound queue as
        `send`; returning `None` sends nothing. Handler failures propagate
        to Wreath's ordinary route error boundary after the connection is
        removed, rather than being swallowed by the service.
        """
        connection_key: Hashable = id(websocket) if key is None else key
        if (
            not self._accepting
            or len(self._connections) >= self.max_connections
            or connection_key in self._connections
        ):
            self._capacity_refusals += 1
            await websocket.close(1013, "connection capacity unavailable")
            return
        if subprotocol is not None and subprotocol not in websocket.subprotocols:
            self._protocol_refusals += 1
            await websocket.close(1002, "subprotocol was not offered")
            return
        connection = _ManagedConnection(self, connection_key, websocket)
        self._connections[connection_key] = connection
        self._accepted += 1
        try:
            await websocket.accept(subprotocol=subprotocol)
            async with asyncio.TaskGroup() as tasks:
                connection.send_task = tasks.create_task(
                    self._send_loop(connection),
                    name=f"wreath.websocket.send:{connection_key}",
                )
                tasks.create_task(
                    self._receive_loop(connection, handler),
                    name=f"wreath.websocket.receive:{connection_key}",
                )
                if self._heartbeat is not None:
                    tasks.create_task(
                        self._heartbeat_loop(connection, self._heartbeat),
                        name=f"wreath.websocket.heartbeat:{connection_key}",
                    )
        finally:
            current = self._connections.get(connection_key)
            if current is connection:
                del self._connections[connection_key]
            self._closed += 1
            connection.done.set()

    async def send(self, key: Hashable, frame: _Frame) -> None:
        """Enqueue one frame under the configured overflow policy."""
        connection = self._connections.get(key)
        if connection is None:
            raise KeyError(f"no active WebSocket connection {key!r}")
        if self._overflow == "backpressure":
            try:
                async with asyncio.timeout(self._enqueue_timeout):
                    await connection.queue.put(frame)
            except TimeoutError as error:
                self._queue_refusals += 1
                raise ConnectionBackpressure(
                    f"connection {key!r} did not free outbound capacity"
                ) from error
            return
        try:
            connection.queue.put_nowait(frame)
        except asyncio.QueueFull as error:
            self._queue_refusals += 1
            if self._overflow == "disconnect":
                send_task = connection.send_task
                if send_task is not None:
                    send_task.cancel()
                    await asyncio.gather(send_task, return_exceptions=True)
                await connection.close(1013, "outbound queue capacity exceeded")
            raise ConnectionBackpressure(f"connection {key!r} outbound queue is full") from error

    async def broadcast(self, frame: _Frame) -> int:
        """Enqueue one frame for each current connection; return acceptances."""
        if self._overflow == "reject":
            delivered = 0
            for connection in tuple(self._connections.values()):
                try:
                    connection.queue.put_nowait(frame)
                except asyncio.QueueFull:
                    self._queue_refusals += 1
                    continue
                delivered += 1
            return delivered
        delivered = 0
        for key in tuple(self._connections):
            try:
                await self.send(key, frame)
            except ConnectionBackpressure:
                continue
            delivered += 1
        return delivered

    async def _send_loop(self, connection: _ManagedConnection) -> None:
        try:
            while True:
                outbound = await connection.queue.get()
                try:
                    if isinstance(outbound, _Close):
                        await connection.close(outbound.code, outbound.reason)
                        return
                    await connection.write(outbound)
                finally:
                    connection.queue.task_done()
        finally:
            connection.stopping.set()

    async def _receive_loop(
        self,
        connection: _ManagedConnection,
        handler: Callable[[_Frame], Awaitable[_Frame | None]],
    ) -> None:
        try:
            async for frame in connection.websocket:
                heartbeat = self._heartbeat
                if heartbeat is not None and heartbeat.acknowledge(frame):
                    connection.heartbeat_ack.set()
                    if heartbeat.consume:
                        continue
                reply = await handler(frame)
                if reply is not None:
                    await self.send(connection.key, reply)
        finally:
            if not connection.stopping.is_set():
                connection.stopping.set()
                await connection.queue.put(_Close(1000, ""))

    async def _heartbeat_loop(
        self,
        connection: _ManagedConnection,
        heartbeat: Heartbeat,
    ) -> None:
        while not connection.stopping.is_set():
            try:
                async with asyncio.timeout(heartbeat.interval):
                    await connection.stopping.wait()
                return
            except TimeoutError:
                pass
            connection.heartbeat_ack.clear()
            await self.send(connection.key, heartbeat.frame)
            heartbeat_wait = asyncio.create_task(connection.heartbeat_ack.wait())
            stopping_wait = asyncio.create_task(connection.stopping.wait())
            try:
                async with asyncio.timeout(heartbeat.timeout):
                    done, _pending = await asyncio.wait(
                        (heartbeat_wait, stopping_wait),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stopping_wait in done:
                        return
            except TimeoutError:
                self._heartbeat_timeouts += 1
                connection.stopping.set()
                send_task = connection.send_task
                if send_task is not None:
                    send_task.cancel()
                    await asyncio.gather(send_task, return_exceptions=True)
                await connection.close(1011, "heartbeat timed out")
                return
            finally:
                heartbeat_wait.cancel()
                stopping_wait.cancel()
                await asyncio.gather(heartbeat_wait, stopping_wait, return_exceptions=True)


__all__ = [
    "Calls",
    "CallsClosed",
    "ConnectionBackpressure",
    "ConnectionSnapshot",
    "Heartbeat",
    "WebSocket",
    "WebSocketDisconnect",
    "WebSocketService",
]
