"""What `HTTPClient.stream` refuses, and what it will not put back in the pool.

The streaming reader is where a proxy meets hostile input: it is the one part
that parses framing supplied by an origin nobody in this process controls. The
happy-path tests in `tests/test_edge.py` left every refusal in `_iter_chunked`
alive under `wreath mutant` -- deleting any one of those five `raise` statements
changed no test's mind, which means the parser could have silently accepted
malformed framing and nothing here would have said so.

So each refusal gets a scripted upstream that produces exactly that malformation
and nothing else, and asserts on the **distinct message** rather than on
`ProtocolError` alone. Asserting the type would pass on whichever branch fired,
which is the failure mode `AGENTS.md` describes for refusal tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from wreath.http_client import (
    ClientLimits,
    DestinationPolicy,
    HTTPClient,
    ProtocolError,
)

pytestmark = pytest.mark.asyncio


async def _serve(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> tuple[asyncio.AbstractServer, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _scripted(response: bytes, *, hang_up: bool = True):
    """An upstream that reads a request head and replies with exactly `response`."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        writer.write(response)
        await writer.drain()
        if hang_up:
            writer.close()

    return handler


async def _drain(port: int, *, limits: ClientLimits | None = None) -> None:
    """Stream `/x` from the scripted upstream, consuming the whole body."""
    client = HTTPClient(
        "s",
        base_url=f"http://127.0.0.1:{port}",
        destination=DestinationPolicy(allow_loopback=True),
        limits=limits or ClientLimits(),
    )
    await client.start()
    try:
        async with client.stream("GET", "/x") as response:
            async for _chunk in response.iter_bytes():
                pass
    finally:
        await client.close()


# --- the five refusals in `_iter_chunked` ------------------------------------


async def test_a_chunk_size_line_over_the_limit_is_refused() -> None:
    """A size line is a few hex digits. Kilobytes of them is an attack, not a size."""
    padding = b"0" * 2048
    server, port = await _serve(
        _scripted(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"
                  + padding + b"1\r\n"))
    try:
        with pytest.raises(ProtocolError, match="chunk line exceeds limit"):
            await _drain(port)
    finally:
        server.close()
        await server.wait_closed()


async def test_a_non_hexadecimal_chunk_size_is_refused() -> None:
    """`zz` is not a length. Accepting it means guessing at a body boundary."""
    server, port = await _serve(
        _scripted(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\nzz\r\nhi\r\n"))
    try:
        with pytest.raises(ProtocolError, match="invalid response chunk size"):
            await _drain(port)
    finally:
        server.close()
        await server.wait_closed()


async def test_an_empty_chunk_size_is_refused() -> None:
    """The same branch, reached by the other half of its condition."""
    server, port = await _serve(
        _scripted(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n\r\nhi\r\n"))
    try:
        with pytest.raises(ProtocolError, match="invalid response chunk size"):
            await _drain(port)
    finally:
        server.close()
        await server.wait_closed()


async def test_trailers_over_the_header_limit_are_refused() -> None:
    """Trailers arrive after the body, so they are a second, later header budget."""
    trailer = b"x-pad: " + b"p" * 400 + b"\r\n"
    server, port = await _serve(
        _scripted(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"
                  b"0\r\n" + trailer * 8 + b"\r\n"))
    try:
        with pytest.raises(ProtocolError, match="trailers exceed configured limit"):
            await _drain(port, limits=ClientLimits(max_response_header_bytes=1024))
    finally:
        server.close()
        await server.wait_closed()


async def test_an_upstream_that_dies_mid_chunk_is_refused() -> None:
    """A short chunk is a truncated body, not a complete one.

    Returning what arrived would hand the caller a prefix and call it the
    response -- which for a proxy means relaying a truncated page as a 200.
    """
    server, port = await _serve(
        _scripted(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"
                  b"10\r\nonly-four"))          # declares 16 bytes, sends 9, closes
    try:
        with pytest.raises(ProtocolError, match="upstream closed mid-chunk"):
            await _drain(port)
    finally:
        server.close()
        await server.wait_closed()


async def test_a_chunk_not_terminated_by_crlf_is_refused() -> None:
    """The CRLF after a chunk is framing, not decoration.

    Without checking it the reader resynchronises on whatever follows, which is
    how a chunked parser can be talked into treating body bytes as a size line.
    """
    server, port = await _serve(
        _scripted(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"
                  b"2\r\nhiXX0\r\n\r\n"))       # "XX" where the CRLF belongs
    try:
        with pytest.raises(ProtocolError, match="chunk not terminated by CRLF"):
            await _drain(port)
    finally:
        server.close()
        await server.wait_closed()


# --- what may go back in the pool --------------------------------------------


async def _reusable_after(response: bytes) -> bool:
    """Stream one scripted response fully; report whether the connection pooled."""
    server, port = await _serve(_scripted(response, hang_up=False))
    client = HTTPClient(
        "s",
        base_url=f"http://127.0.0.1:{port}",
        destination=DestinationPolicy(allow_loopback=True),
    )
    await client.start()
    try:
        async with client.stream("GET", "/x") as streamed:
            async for _chunk in streamed.iter_bytes():
                pass
        # `open` counts connections the pool is holding; a connection that was
        # closed rather than returned leaves nothing behind.
        return client.snapshot().idle > 0
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


async def test_a_clean_http11_response_is_pooled() -> None:
    assert await _reusable_after(
        b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nhi")


async def test_connection_close_is_not_pooled() -> None:
    """The origin said this connection is finished. Believing it is the contract."""
    assert not await _reusable_after(
        b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nhi")


async def test_http10_without_keep_alive_is_not_pooled() -> None:
    """HTTP/1.0 defaults to closing, so silence means close rather than keep.

    This is the clause a mutant removed without any test objecting: dropping it
    makes every 1.0 response look reusable, and the next request on that socket
    goes to an origin that has already hung up.
    """
    assert not await _reusable_after(
        b"HTTP/1.0 200 OK\r\ncontent-length: 2\r\n\r\nhi")


async def test_http10_with_keep_alive_is_pooled() -> None:
    """The other side of the same clause, so neither direction is assumed."""
    assert await _reusable_after(
        b"HTTP/1.0 200 OK\r\ncontent-length: 2\r\nconnection: keep-alive\r\n\r\nhi")


# --- the transport failure on the way in --------------------------------------


async def test_an_upstream_that_hangs_up_before_the_head_is_a_transport_error() -> None:
    """`__aenter__` fails before there is any response to stream.

    The connection is released as unusable on this path; leaving it pooled would
    hand the next caller a socket the origin has already closed.
    """
    from wreath.http_client import ClientError

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.close()                      # no response at all

    server, port = await _serve(handler)
    client = HTTPClient(
        "s",
        base_url=f"http://127.0.0.1:{port}",
        destination=DestinationPolicy(allow_loopback=True),
    )
    await client.start()
    try:
        with pytest.raises(ClientError):
            async with client.stream("GET", "/x"):
                pass
        assert client.snapshot().idle == 0
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


async def test_a_close_delimited_body_is_never_pooled() -> None:
    """When the end of the body *is* the close, there is no connection to keep.

    A close-delimited response has no `Content-Length` and no
    `Transfer-Encoding`: the origin says "this is the body" by hanging up, so
    pooling the socket would pool a closed one.

    This does **not** distinguish `_keeps_alive`'s `framed` operand, and cannot
    -- `_release` refuses a connection at EOF regardless, which is why that
    operand survives mutation. See its docstring. The assertion here is the
    behaviour a caller depends on, not the clause that implements it.
    """

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\n\r\nhi")
        await writer.drain()
        writer.close()                       # the close is the framing

    server, port = await _serve(handler)
    client = HTTPClient(
        "s",
        base_url=f"http://127.0.0.1:{port}",
        destination=DestinationPolicy(allow_loopback=True),
    )
    await client.start()
    try:
        response = await client.request("GET", "/x")
        assert response.body == b"hi"
        assert client.snapshot().idle == 0
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
