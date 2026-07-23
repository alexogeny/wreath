"""The outbound HTTP client as a stream-fusion implementer.

On a metal loop the client's plaintext connections must fuse: wire bytes land
in the native stream protocol's C buffer with no Python calling convention per
read, and query dispatch leaves through the transport C API. Framing semantics
(content-length, chunked with trailers, close-delimited) must be identical to
the asyncio-streams path they replace.
"""
from __future__ import annotations

import asyncio
import errno
import importlib

import pytest

from wreath.http_client import DestinationPolicy, HTTPClient


def _client(port: int) -> HTTPClient:
    return HTTPClient(
        "fused-test",
        base_url=f"http://127.0.0.1:{port}",
        destination=DestinationPolicy(allow_private=True, allow_loopback=True),
    )


def _metal_loop_or_skip():
    reactor = importlib.import_module("wreath.reactor")
    try:
        return reactor.metal_event_loop(diagnostics=True)
    except OSError as exc:
        assert exc.errno in {errno.ENOSYS, errno.EPERM, errno.EACCES, errno.ENOMEM}
        pytest.skip("io_uring unavailable")


async def _serve(responses: list[bytes]):
    """One canned response per accepted connection, then close."""
    index = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        nonlocal index
        await reader.readuntil(b"\r\n\r\n")
        body = responses[min(index, len(responses) - 1)]
        index += 1
        writer.write(body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _run(loop, coro, transports):
    original = loop._make_socket_transport

    def capture(*args, **kwargs):
        transport = original(*args, **kwargs)
        transports.append(transport)
        return transport

    loop._make_socket_transport = capture
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_client_ingress_fuses_on_metal_loop() -> None:
    loop = _metal_loop_or_skip()
    transports: list = []

    async def exercise():
        server, port = await _serve([
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello",
        ])
        try:
            async with _client(port) as client:
                response = await client.get("/")
            return response
        finally:
            server.close()
            await server.wait_closed()

    response = _run(loop, exercise(), transports)
    assert response.status == 200
    assert response.body == b"hello"

    fused = [
        t for t in transports
        if getattr(t, "_fused_stream", None) == "wreath._native._client"
    ]
    assert fused, [getattr(t, "_fused_stream", None) for t in transports]
    assert all(t._fused_http1 is False for t in fused)


def test_client_framing_parity_on_metal_loop() -> None:
    """Chunked with trailers, content-length, and close-delimited bodies all
    decode identically through the fused reader."""
    loop = _metal_loop_or_skip()
    transports: list = []

    chunked = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"5\r\nhello\r\n6\r\n world\r\n0\r\nX-Trailer: 1\r\n\r\n"
    )
    sized = b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\nhello world"
    closed = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nhello world"

    async def exercise():
        bodies = []
        for canned in (chunked, sized, closed):
            server, port = await _serve([canned])
            try:
                async with _client(port) as client:
                    response = await client.get("/")
                bodies.append(response.body)
            finally:
                server.close()
                await server.wait_closed()
        return bodies

    bodies = _run(loop, exercise(), transports)
    assert bodies == [b"hello world", b"hello world", b"hello world"]
    assert any(
        getattr(t, "_fused_stream", None) == "wreath._native._client"
        for t in transports
    )


def test_client_large_body_spans_many_reads_on_metal_loop() -> None:
    """A body far larger than one 16 KiB provided buffer arrives intact."""
    loop = _metal_loop_or_skip()
    transports: list = []
    body = bytes(range(256)) * 1024  # 256 KiB, byte-position sensitive

    async def exercise():
        canned = (
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        server, port = await _serve([canned])
        try:
            async with _client(port) as client:
                response = await client.get("/")
            return response.body
        finally:
            server.close()
            await server.wait_closed()

    received = _run(loop, exercise(), transports)
    assert received == body
    assert any(
        getattr(t, "_fused_stream", None) == "wreath._native._client"
        for t in transports
    )
