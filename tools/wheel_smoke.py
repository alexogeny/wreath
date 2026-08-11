"""Import and serve one request from an installed release wheel."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any


async def _serve_request() -> None:
    from wreath import Request, Wreath
    from wreath.response import TextResponse
    from wreath.server import ServerConfig, serve

    app = Wreath()

    @app.get("/")
    async def index(request: Request) -> TextResponse:
        return TextResponse("wheel-ok")

    server = await serve(
        app,
        ServerConfig(host="127.0.0.1", port=0, lifespan="off"),
    )
    try:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: wheel.test\r\nConnection: close\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=5.0)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()

    if b"HTTP/1.1 200" not in response or not response.endswith(b"wheel-ok"):
        raise RuntimeError(f"installed wheel returned an invalid response: {response!r}")


def _extension(name: str) -> Any | None:
    from wreath._native import extension

    return extension(name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", action="store_true")
    parser.add_argument("--reactor", action="store_true")
    parser.add_argument("--http3", action="store_true")
    args = parser.parse_args()

    for name in ("_core", "_client", "_edge", "_server", "_postgres"):
        if _extension(name) is None:
            raise RuntimeError(f"installed wheel is missing required extension {name}")

    reactor = _extension("_reactor")
    http3 = _extension("_http3")
    if args.base and (reactor is not None or http3 is not None):
        raise RuntimeError("the base wheel contains a platform extra")
    if args.reactor and reactor is None:
        raise RuntimeError("wreath-linux did not install the io_uring reactor")
    if args.http3 and http3 is None:
        raise RuntimeError("wreath-http3 did not install the HTTP/3 extension")

    asyncio.run(_serve_request())


if __name__ == "__main__":
    main()
