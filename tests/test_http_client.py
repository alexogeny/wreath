from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import Awaitable, Callable
from typing import cast

import pytest

from wreath import Wreath
from wreath.http_client import (
    ClientClosed,
    ClientLimits,
    ClientTimeout,
    DestinationPolicy,
    DestinationRejected,
    HTTPClient,
    PoolTimeout,
    ProtocolError,
    RedirectError,
    RedirectPolicy,
    RequestTimeout,
    RetryPolicy,
)
from wreath.testing import TestClient


async def _serve(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> tuple[asyncio.AbstractServer, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _local_policy() -> DestinationPolicy:
    return DestinationPolicy(allow_private=True, allow_loopback=True)


@pytest.mark.asyncio
async def test_client_sends_request_and_reads_fixed_response() -> None:
    received: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.readuntil(b"\r\n\r\n"))
        body = await reader.readexactly(2)
        received.append(body)
        writer.write(
            b"HTTP/1.1 202 Accepted\r\n"
            b"content-length: 2\r\n"
            b"content-type: application/json\r\n\r\n{}"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "test",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        response = await client.post(
            "/events",
            headers=((b"content-type", b"application/json"),),
            body=b"{}",
        )
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert response.status == 202
    assert response.body == b"{}"
    assert response.header(b"content-type") == b"application/json"
    assert received[0].startswith(b"POST /events HTTP/1.1\r\n")
    assert received[1] == b"{}"


@pytest.mark.asyncio
async def test_client_reuses_complete_keep_alive_connection() -> None:
    connections = 0
    requests = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections, requests
        connections += 1
        try:
            while requests < 2:
                await reader.readuntil(b"\r\n\r\n")
                requests += 1
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 1\r\n\r\nx")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "test",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        first = await client.get("/one")
        second = await client.get("/two")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert first.body == second.body == b"x"
    assert connections == 1
    assert client.snapshot().requests == 2
    assert client.snapshot().reused == 1


@pytest.mark.asyncio
async def test_client_decodes_chunked_response() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"
            b"2\r\nab\r\n3;ext=yes\r\ncde\r\n0\r\nx-trailer: done\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "test",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        response = await client.get("/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert response.body == b"abcde"


@pytest.mark.asyncio
async def test_client_rejects_loopback_by_default() -> None:
    client = HTTPClient("unsafe", base_url="http://127.0.0.1:8000")
    await client.start()
    with pytest.raises(Exception, match="loopback"):
        await client.get("/")
    await client.close()


@pytest.mark.asyncio
async def test_client_bounds_response_body() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 4\r\n\r\ntoolong")
        await writer.drain()

    server, port = await _serve(handler)
    client = HTTPClient(
        "test",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
        limits=ClientLimits(max_response_bytes=3),
    )
    try:
        await client.start()
        with pytest.raises(Exception, match="response body"):
            await client.get("/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_closed_client_rejects_requests() -> None:
    client = HTTPClient("closed", base_url="https://example.com")
    with pytest.raises(ClientClosed):
        await client.get("/")
    await client.start()
    await client.close()
    with pytest.raises(ClientClosed):
        await client.get("/")


def test_client_configuration_rejects_invalid_limits_and_timeouts() -> None:
    with pytest.raises(ValueError):
        ClientLimits(max_connections=0)
    with pytest.raises(ValueError):
        ClientLimits(dns_cache_ttl=-1)
    with pytest.raises(ValueError):
        ClientTimeout(total=0)


@pytest.mark.asyncio
async def test_app_owns_named_client_lifespan() -> None:
    app = Wreath()
    client = app.http_client(
        "partner",
        base_url="https://example.com",
        destination=DestinationPolicy(hosts=("example.com",)),
    )

    assert app.state.http_partner is client
    async with TestClient(app):
        assert client.started
    assert not client.started

    with pytest.raises(ValueError, match="duplicate HTTP client"):
        app.http_client("partner", base_url="https://example.com")


@pytest.mark.asyncio
async def test_client_follows_bounded_same_origin_redirect() -> None:
    targets: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        for response in (
            b"HTTP/1.1 302 Found\r\nlocation: /final\r\ncontent-length: 0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok",
        ):
            head = await reader.readuntil(b"\r\n\r\n")
            targets.append(head.split(b" ", 2)[1])
            writer.write(response)
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "redirect",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
        redirect=RedirectPolicy(enabled=True, max_hops=2),
    )
    try:
        await client.start()
        response = await client.get("/start")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert response.body == b"ok"
    assert targets == [b"/start", b"/final"]


@pytest.mark.asyncio
async def test_client_rejects_redirect_loop_at_configured_bound() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 302 Found\r\nlocation: /loop\r\ncontent-length: 0\r\n\r\n"
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "redirect",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
        redirect=RedirectPolicy(enabled=True, max_hops=1),
    )
    try:
        await client.start()
        with pytest.raises(RedirectError, match="limit"):
            await client.get("/loop")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_total_timeout_bounds_all_retry_attempts_and_backoff() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 503 Unavailable\r\ncontent-length: 0\r\n\r\n"
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "deadline",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
        retry=RetryPolicy(attempts=10),
        timeout=ClientTimeout(total=0.08),
    )
    try:
        await client.start()
        with pytest.raises(RequestTimeout, match="total"):
            await client.get("/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_client_shutdown_drains_owned_request_before_closing() -> None:
    received = asyncio.Event()
    release = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        received.set()
        await release.wait()
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "drain",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
        timeout=ClientTimeout(total=1),
    )
    await client.start()
    request = asyncio.create_task(client.get("/"))
    await received.wait()
    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    response = await request
    await closing
    server.close()
    await server.wait_closed()

    assert response.body == b"ok"
    assert client.snapshot().active == 0
    assert not client.started


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "wire", "expected_status", "expected_body"),
    [
        (
            "GET",
            b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok",
            200,
            b"ok",
        ),
        (
            "GET",
            b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"
            b"2\r\nok\r\n0\r\n\r\n",
            200,
            b"ok",
        ),
        ("GET", b"HTTP/1.1 200 OK\r\n\r\nclose", 200, b"close"),
        ("GET", b"HTTP/1.1 204 No Content\r\n\r\n", 204, b""),
        ("GET", b"HTTP/1.1 304 Not Modified\r\n\r\n", 304, b""),
        (
            "GET",
            b"HTTP/1.1 100 Continue\r\n\r\n"
            b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok",
            200,
            b"ok",
        ),
        ("HEAD", b"HTTP/1.1 200 OK\r\ncontent-length: 50\r\n\r\n", 200, b""),
    ],
)
async def test_client_accepts_core_response_shapes_fragmented_at_every_byte(
    method: str,
    wire: bytes,
    expected_status: int,
    expected_body: bytes,
) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        for byte in wire:
            writer.write(bytes((byte,)))
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "fragmented",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        response = await client.request(method, "/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
    assert response.status == expected_status
    assert response.body == expected_body


@pytest.mark.asyncio
async def test_client_skips_informational_response_before_final_response() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 100 Continue\r\n\r\n"
            b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "informational",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        response = await client.get("/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
    assert response.status == 200
    assert response.body == b"ok"


@pytest.mark.asyncio
async def test_head_response_does_not_consume_declared_body_length() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 99\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "head",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        response = await client.request("HEAD", "/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
    assert response.body == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire",
    [
        (
            b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n"
            b"transfer-encoding: chunked\r\n\r\n"
        ),
        b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\ncontent-length: 3\r\n\r\n",
        b"HTTP/1.1 200 OK\r\ntransfer-encoding: gzip\r\n\r\n",
        b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\nzz\r\n",
        b"HTTP/1.1 200 OK\r\ncontent-length: 4\r\n\r\nxy",
    ],
)
async def test_client_rejects_ambiguous_malformed_or_truncated_framing(
    wire: bytes,
) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(wire)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "malformed",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        with pytest.raises(ProtocolError):
            await client.get("/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (204, 304))
async def test_bodyless_status_does_not_wait_for_declared_length(status: int) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            f"HTTP/1.1 {status} Bodyless\r\ncontent-length: 99\r\n\r\n".encode()
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "bodyless",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        response = await client.get("/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
    assert response.status == status
    assert response.body == b""


@pytest.mark.asyncio
async def test_close_delimited_response_is_read_but_not_reused() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\n\r\nclose-body")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "close-delimited",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        response = await client.get("/")
        assert client.snapshot().idle == 0
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
    assert response.body == b"close-body"


@pytest.mark.asyncio
async def test_unsolicited_bytes_after_framed_body_are_rejected() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nokEXTRA")
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "extra",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        with pytest.raises(ProtocolError, match="unsolicited"):
            await client.get("/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("headers", "body"))
async def test_cancellation_during_response_closes_owned_connection(phase: str) -> None:
    received = asyncio.Event()
    release = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        if phase == "body":
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 10\r\n\r\nx")
            await writer.drain()
        received.set()
        await release.wait()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "cancel",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
        timeout=ClientTimeout(total=5),
    )
    await client.start()
    request = asyncio.create_task(client.get("/"))
    await received.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    release.set()
    await asyncio.sleep(0)
    assert client.snapshot().active == 0
    assert client.snapshot().idle == 0
    await client.close()
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_cancellation_during_retry_backoff_stops_attempts() -> None:
    responded = asyncio.Event()
    requests = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal requests
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                requests += 1
                writer.write(
                    b"HTTP/1.1 503 Unavailable\r\ncontent-length: 0\r\n\r\n"
                )
                await writer.drain()
                responded.set()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "retry-cancel",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
        retry=RetryPolicy(attempts=10),
        timeout=ClientTimeout(total=5),
    )
    await client.start()
    request = asyncio.create_task(client.get("/"))
    await responded.wait()
    await asyncio.sleep(0)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await asyncio.sleep(0.1)
    assert requests == 1
    await client.close()
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_redirects_resolve_against_effective_base_path_once() -> None:
    targets: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        for response in (
            b"HTTP/1.1 302 Found\r\nlocation: /final\r\ncontent-length: 0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok",
        ):
            head = await reader.readuntil(b"\r\n\r\n")
            targets.append(head.split(b" ", 2)[1])
            writer.write(response)
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "redirect-base",
        base_url=f"http://127.0.0.1:{port}/api",
        destination=_local_policy(),
        redirect=RedirectPolicy(enabled=True, max_hops=1),
    )
    try:
        await client.start()
        assert (await client.get("/start")).body == b"ok"
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert targets == [b"/api/start", b"/final"]


@pytest.mark.asyncio
async def test_connection_close_token_prevents_pool_reuse() -> None:
    connections = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"connection: keep-alive, close\r\n"
                b"content-length: 1\r\n\r\nx"
            )
            await writer.drain()
            try:
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 0.2)
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 1\r\n\r\nx")
                await writer.drain()
            except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
                pass
        finally:
            writer.close()
            await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "connection-tokens",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        assert (await client.get("/first")).body == b"x"
        assert (await client.get("/second")).body == b"x"
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert connections == 2


@pytest.mark.asyncio
async def test_malformed_response_head_is_protocol_error_and_is_not_retried() -> None:
    requests = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal requests
        await reader.readuntil(b"\r\n\r\n")
        requests += 1
        writer.write(b"not-http\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "malformed-head",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
        retry=RetryPolicy(attempts=3),
    )
    try:
        await client.start()
        with pytest.raises(ProtocolError):
            await client.get("/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert requests == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("size", (b"+1", b"0x1", b"1_0"))
async def test_client_rejects_non_rfc_chunk_sizes(size: bytes) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"
            + size
            + b"\r\nx\r\n0\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "chunk-size",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        with pytest.raises(ProtocolError, match="chunk size"):
            await client.get("/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trailer",
    (b" bad: value", b"bad name: value", b"x-test: bad\x01value"),
)
async def test_client_rejects_malformed_response_trailers(trailer: bytes) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"
            b"1\r\nx\r\n0\r\n"
            + trailer
            + b"\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "trailers",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        with pytest.raises(ProtocolError, match="header|folding"):
            await client.get("/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


def test_destination_policy_rejects_non_global_shared_addresses() -> None:
    with pytest.raises(DestinationRejected, match="non-global"):
        DestinationPolicy().validate_address("100.64.0.1")


@pytest.mark.asyncio
async def test_client_caches_dns_and_tls_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()
    resolutions = 0
    contexts = 0
    connection_limits: list[int] = []

    async def getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        nonlocal resolutions
        resolutions += 1
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]

    def create_default_context() -> ssl.SSLContext:
        nonlocal contexts
        contexts += 1
        return cast(ssl.SSLContext, object())

    async def open_connection(
        *_args: object, **kwargs: object
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        connection_limits.append(cast(int, kwargs["limit"]))
        return cast(asyncio.StreamReader, object()), cast(asyncio.StreamWriter, object())

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(ssl, "create_default_context", create_default_context)
    # DNS/TLS-setup caching is transport-agnostic; spoof the streams seam.
    monkeypatch.setattr("wreath.http_client._NativeClientStream", None)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)

    limits = ClientLimits(read_high_water=12345, dns_cache_ttl=30)
    client = HTTPClient("cached", base_url="https://example.com", limits=limits)
    await client._connect()
    await client._connect()

    assert resolutions == 1
    assert contexts == 1
    assert connection_limits == [12345, 12345]


@pytest.mark.asyncio
async def test_client_races_resolved_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()
    slow = asyncio.Event()
    attempted: list[str] = []

    async def getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 80)),
        ]

    async def open_connection(
        address: str, _port: int, **_kwargs: object
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        attempted.append(address)
        if address == "8.8.8.8":
            await slow.wait()
        return cast(asyncio.StreamReader, object()), cast(asyncio.StreamWriter, object())

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    # Address racing is transport-agnostic; spoof the streams seam.
    monkeypatch.setattr("wreath.http_client._NativeClientStream", None)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)

    client = HTTPClient("race", base_url="http://example.com")
    await asyncio.wait_for(client._connect(), 1)

    assert attempted == ["8.8.8.8", "1.1.1.1"]


@pytest.mark.asyncio
async def test_pool_bounds_waiters_and_cancellation_removes_waiter() -> None:
    received = asyncio.Event()
    release = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        received.set()
        await release.wait()
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "bounded",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
        limits=ClientLimits(
            max_connections=1,
            max_keepalive_connections=1,
            max_waiters=1,
        ),
        timeout=ClientTimeout(pool=1, total=2),
    )
    await client.start()
    first = asyncio.create_task(client.get("/first"))
    await received.wait()
    waiting = asyncio.create_task(client.get("/waiting"))
    for _ in range(20):
        if client.snapshot().waiters == 1:
            break
        await asyncio.sleep(0)
    assert client.snapshot().waiters == 1
    with pytest.raises(PoolTimeout, match="waiter limit"):
        await client.get("/rejected")
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert client.snapshot().waiters == 0
    release.set()
    assert (await first).body == b"ok"
    await client.close()
    server.close()
    await server.wait_closed()
