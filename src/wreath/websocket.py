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

from typing import Any

from ._headers import find_header

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
        return self.scope.get("subprotocols", [])

    @property
    def headers(self) -> list[tuple[bytes, bytes]]:
        """Handshake headers as raw `(name, value)` byte pairs, names lowercased."""
        return self.scope.get("headers", [])

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


__all__ = ["WebSocket", "WebSocketDisconnect"]
