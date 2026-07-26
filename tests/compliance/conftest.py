"""Shared harness for the compliance suite.

`drive_request` feeds raw request bytes to the real native HTTP/1 protocol and
returns the raw response bytes, so a test can assert on the actual status line
and headers a client would see. `status_of` / `header_block` parse those bytes.
"""
from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

from wreath.server import ServerConfig


class CapturingTransport:
    """The minimum asyncio.Transport surface the native protocol touches, but it
    keeps every byte written so a test can inspect the response."""

    def __init__(self) -> None:
        self._extra = {"sockname": ("127.0.0.1", 8000), "peername": ("127.0.0.1", 54321)}
        self.data = bytearray()
        self.closed = False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._extra.get(name, default)

    def write(self, data: Any) -> None:
        self.data += bytes(data)

    def writelines(self, chunks: Any) -> None:
        for chunk in chunks:
            self.data += bytes(chunk)

    def pause_reading(self) -> None: ...
    def resume_reading(self) -> None: ...
    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.closed = True


async def _ok_app(scope: dict, receive: Any, send: Any) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect" or not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def drive_request(request: bytes, *, app: Any = None, config: ServerConfig | None = None) -> bytes:
    """Feed `request` to the native HTTP/1 protocol; return the raw response bytes."""
    server = importlib.import_module("wreath._native._server")

    async def run() -> bytes:
        loop = asyncio.get_running_loop()
        protocol = server.HttpProtocol(app or _ok_app, config or ServerConfig(), loop, set())
        transport = CapturingTransport()
        protocol.connection_made(transport)
        protocol.data_received(request)
        for _ in range(20):            # let the app task and response drain
            await asyncio.sleep(0)
        protocol.connection_lost(None)
        await asyncio.sleep(0)
        return bytes(transport.data)

    return asyncio.run(run())


def status_of(response: bytes) -> int:
    """The status code from the first status line (`HTTP/1.1 <code> ...`)."""
    first = response.split(b"\r\n", 1)[0]
    parts = first.split(b" ", 2)
    return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0


def header_block(response: bytes) -> dict[bytes, bytes]:
    """Lower-cased header map from the first response's head (last value wins)."""
    head = response.split(b"\r\n\r\n", 1)[0]
    lines = head.split(b"\r\n")[1:]
    out: dict[bytes, bytes] = {}
    for line in lines:
        if b":" in line:
            name, _, value = line.partition(b":")
            out[name.strip().lower()] = value.strip()
    return out


@pytest.fixture
def drive():
    return drive_request
