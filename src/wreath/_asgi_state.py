"""Small ASGI protocol-state primitives shared by in-process drivers.

The production warm driver, test client, and replay engine remain independent
drivers and lifespan oracles. They share only this declarative response state
machine: start once, append body chunks linearly, and optionally refuse messages
after completion.
"""

from __future__ import annotations

from typing import Any


class ResponseCapture:
    """Collect one ASGI HTTP response with configurable strictness."""

    __slots__ = ("_chunks", "finished", "headers", "status", "strict")

    def __init__(self, *, strict: bool = True) -> None:
        self.status: int | None = None
        self.headers: tuple[tuple[bytes, bytes], ...] = ()
        self._chunks: list[bytes] = []
        self.finished = False
        self.strict = strict

    @property
    def body(self) -> bytes:
        """Materialize the collected body once, at the driver's boundary."""
        if len(self._chunks) == 1 and type(self._chunks[0]) is bytes:
            return self._chunks[0]
        if not self._chunks:
            return b""
        body = b"".join(self._chunks)
        self._chunks[:] = [body]
        return body

    async def send(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "wreath.response":
            if self.strict and self.status is not None:
                raise RuntimeError("ASGI app sent two response starts")
            self.status = int(message["status"])
            self.headers = tuple(message.get("headers", ()))
            self._chunks.append(bytes(message.get("body", b"")))
            self.finished = True
            return
        if self.strict and self.finished:
            raise RuntimeError("ASGI app sent a message after the response ended")
        if kind == "http.response.start":
            if self.strict and self.status is not None:
                raise RuntimeError("ASGI app sent two response starts")
            self.status = int(message["status"])
            self.headers = tuple(message.get("headers", ()))
            return
        if kind == "http.response.body":
            if self.strict and self.status is None:
                raise RuntimeError("ASGI app sent a body before response start")
            self._chunks.append(bytes(message.get("body", b"")))
            self.finished = not bool(message.get("more_body"))
            return
        if self.strict:
            raise RuntimeError(f"ASGI app sent unsupported HTTP message {kind!r}")

    def require_complete(self) -> None:
        if self.status is None:
            raise RuntimeError("ASGI app returned without starting a response")
        if not self.finished:
            raise RuntimeError("ASGI app returned before ending the response body")


__all__: tuple[str, ...] = ()
