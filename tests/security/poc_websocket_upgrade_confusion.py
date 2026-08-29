from __future__ import annotations

import asyncio
import socket
import threading

from wreath import Wreath
from wreath.reactor import metal_event_loop
from wreath.server import Server, ServerConfig
from wreath.websocket import WebSocket

KEY = b"dGhlIHNhbXBsZSBub25jZQ=="


def _handshake(connection: bytes, framing: bytes = b"") -> bytes:
    return (
        b"GET /ws HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: " + connection + b"\r\n"
        b"Sec-WebSocket-Key: " + KEY + b"\r\n"
        b"Sec-WebSocket-Version: 13\r\n" + framing + b"\r\n"
    )


def _exchange(
    port: int,
    request: bytes,
    output: list[bytes],
    loop: asyncio.AbstractEventLoop,
    finished: asyncio.Event,
) -> None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(request)
            response = bytearray()
            # The PoC decides only from the status line. Waiting for EOF routed
            # a safe HTTP rejection through the connection's five-second
            # keep-alive timeout, making this regression test cost 5.2s for no
            # additional evidence.
            while b"\r\n" not in response:
                part = client.recv(4096)
                if not part:
                    break
                response += part
            output.append(bytes(response))
    finally:
        loop.call_soon_threadsafe(finished.set)


async def _drive(server: Server, port: int, attacks: list[bytes]) -> list[bytes]:
    responses: list[bytes] = []
    loop = asyncio.get_running_loop()
    for attack in attacks:
        finished = asyncio.Event()
        thread = threading.Thread(
            target=_exchange,
            args=(port, attack, responses, loop, finished),
        )
        thread.start()
        await finished.wait()
        thread.join()
    await server.close()
    return responses


def main() -> int:
    app = Wreath()

    @app.websocket("/ws")
    async def websocket(socket: WebSocket) -> None:
        await socket.accept()
        await socket.close()

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

        attacks = [
            # "xupgrade" is one Connection token, not the "upgrade" token.
            _handshake(b"xupgrade"),
            # RFC 9112 forbids this ambiguous request framing.
            _handshake(
                b"Upgrade",
                b"Content-Length: 4\r\nTransfer-Encoding: chunked\r\n",
            ),
        ]
        responses = loop.run_until_complete(_drive(server, port, attacks))
    finally:
        loop.close()

    statuses = [response.partition(b"\r\n")[0] for response in responses]
    for status in statuses:
        print(status.decode("ascii", "replace"))
    vulnerable = all(status == b"HTTP/1.1 101 Switching Protocols" for status in statuses)
    print("VULNERABLE" if vulnerable else "not vulnerable")
    return 0 if vulnerable else 1


if __name__ == "__main__":
    raise SystemExit(main())
