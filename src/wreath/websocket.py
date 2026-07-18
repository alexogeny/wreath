"""WebSocket connection API for route handlers.

A handler registered with ``@app.websocket(path)`` receives one
:class:`WebSocket` per connection. The server has already validated the
upgrade handshake; the handler decides whether to :meth:`WebSocket.accept`
or :meth:`WebSocket.close` it, then exchanges messages::

    @app.websocket("/echo")
    async def echo(ws: WebSocket) -> None:
        await ws.accept()
        async for message in ws:
            await ws.send(message)

Iteration ends when the peer disconnects. Explicit ``receive_text()`` /
``receive_bytes()`` raise :class:`WebSocketDisconnect` instead.
"""

from __future__ import annotations

from typing import Any

from ._headers import find_header

Message = dict[str, Any]


class WebSocketDisconnect(Exception):
    """The peer closed or dropped the connection."""

    def __init__(self, code: int = 1006) -> None:
        super().__init__(code)
        self.code = code


class WebSocket:
    """One WebSocket connection, backed directly by the ASGI scope."""

    __slots__ = ("_accepted", "_connected", "_receive", "_send", "path_params", "scope")

    def __init__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        path_params: dict[str, str] | None = None,
    ) -> None:
        self.scope = scope
        self._receive = receive
        self._send = send
        self.path_params = path_params or {}
        self._connected = False
        self._accepted = False

    @property
    def path(self) -> str:
        return self.scope["path"]

    @property
    def query_string(self) -> bytes:
        return self.scope.get("query_string", b"")

    @property
    def subprotocols(self) -> list[str]:
        return self.scope.get("subprotocols", [])

    @property
    def headers(self) -> list[tuple[bytes, bytes]]:
        return self.scope.get("headers", [])

    def header(self, name: str | bytes, default: str | None = None) -> str | None:
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
        await self._ensure_connect()
        message: Message = {"type": "websocket.accept", "subprotocol": subprotocol}
        if headers:
            message["headers"] = headers
        await self._send(message)
        self._accepted = True

    async def receive(self) -> Message:
        """The next raw ASGI message (receive/disconnect)."""
        await self._ensure_connect()
        return await self._receive()

    async def receive_text(self) -> str:
        message = await self.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1006))
        text = message.get("text")
        if text is None:
            raise RuntimeError("expected a text message, received binary")
        return text

    async def receive_bytes(self) -> bytes:
        message = await self.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1006))
        payload = message.get("bytes")
        if payload is None:
            raise RuntimeError("expected a binary message, received text")
        return payload

    async def send(self, data: str | bytes) -> None:
        if isinstance(data, str):
            await self._send({"type": "websocket.send", "text": data})
        else:
            await self._send({"type": "websocket.send", "bytes": data})

    async def send_text(self, data: str) -> None:
        await self._send({"type": "websocket.send", "text": data})

    async def send_bytes(self, data: bytes) -> None:
        await self._send({"type": "websocket.send", "bytes": data})

    async def close(self, code: int = 1000, reason: str = "") -> None:
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
