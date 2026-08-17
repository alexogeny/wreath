from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest

import wreath.http_client as http_client_module
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


@pytest.mark.parametrize(("over_tls", "keep_open"), [(False, True), (True, False)])
@pytest.mark.asyncio
async def test_client_stream_eof_only_keeps_plaintext_transport_open(
    over_tls: bool,
    keep_open: bool,
) -> None:
    class Transport:
        @staticmethod
        def get_extra_info(name: str, default: object = None) -> object:
            if name == "sslcontext" and over_tls:
                return object()
            return default

    stream = http_client_module._ClientStream()
    stream.connection_made(Transport())

    assert stream.eof_received() is keep_open
    stream.connection_lost(None)


@pytest.mark.asyncio
async def test_reused_connection_skips_condition_when_no_waiter_exists() -> None:
    """The idle pool transition is atomic until an operation actually awaits."""

    class OpenReader:
        @staticmethod
        def at_eof() -> bool:
            return False

    class OpenWriter:
        @staticmethod
        def is_closing() -> bool:
            return False

        @staticmethod
        def close() -> None:
            raise AssertionError("a reusable connection was closed")

    class UnexpectedCondition:
        async def __aenter__(self) -> None:
            raise AssertionError("the uncontended pool path entered the condition")

    client = HTTPClient(
        "fast-pool",
        base_url="http://127.0.0.1:8000",
        destination=_local_policy(),
    )
    connection = http_client_module._Connection(OpenReader(), OpenWriter())
    client._started = True
    client._open = 1
    client._idle.append(connection)
    client._condition = cast(Any, UnexpectedCondition())

    assert await client._acquire() is connection
    assert client.snapshot().active == 1
    await client._release(connection, True)
    assert client.snapshot().active == 0
    assert client.snapshot().idle == 1
    assert client.snapshot().reused == 1


@pytest.mark.asyncio
async def test_acquire_refuses_a_client_that_was_not_started() -> None:
    client = HTTPClient(
        "closed-pool",
        base_url="http://127.0.0.1:8000",
        destination=_local_policy(),
    )

    with pytest.raises(ClientClosed, match="not started"):
        await client._acquire()


@pytest.mark.asyncio
async def test_a_parked_waiter_forces_idle_acquire_through_condition() -> None:
    class OpenReader:
        @staticmethod
        def at_eof() -> bool:
            return False

    class OpenWriter:
        @staticmethod
        def is_closing() -> bool:
            return False

    class CountingCondition:
        entered = 0

        async def __aenter__(self):
            self.entered += 1
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    client = HTTPClient(
        "fair-pool",
        base_url="http://127.0.0.1:8000",
        destination=_local_policy(),
    )
    condition = CountingCondition()
    connection = http_client_module._Connection(OpenReader(), OpenWriter())
    client._started = True
    client._open = 1
    client._waiters = 1
    client._idle.append(connection)
    client._condition = cast(Any, condition)

    assert await client._acquire() is connection
    assert condition.entered == 1


@pytest.mark.parametrize(("closing", "eof"), [(True, False), (False, True)])
@pytest.mark.asyncio
async def test_acquire_discards_each_kind_of_dead_idle_connection(
    monkeypatch: pytest.MonkeyPatch,
    closing: bool,
    eof: bool,
) -> None:
    class Reader:
        def __init__(self, at_eof: bool) -> None:
            self._at_eof = at_eof

        def at_eof(self) -> bool:
            return self._at_eof

    class Writer:
        def __init__(self, is_closing: bool) -> None:
            self._is_closing = is_closing
            self.closed = 0

        def is_closing(self) -> bool:
            return self._is_closing

        def close(self) -> None:
            self.closed += 1

    dead_writer = Writer(closing)
    dead = http_client_module._Connection(Reader(eof), dead_writer)
    replacement = http_client_module._Connection(Reader(False), Writer(False))

    async def connect(_client: HTTPClient):
        return replacement

    monkeypatch.setattr(HTTPClient, "_connect", connect)
    client = HTTPClient(
        "dead-idle",
        base_url="http://127.0.0.1:8000",
        destination=_local_policy(),
    )
    client._started = True
    client._open = 1
    client._idle.append(dead)

    assert await client._acquire() is replacement
    assert dead_writer.closed == 1
    assert client._open == 1


@pytest.mark.parametrize(
    "blocked_by",
    ["stopped", "waiter", "closing", "eof", "keepalive-limit"],
)
@pytest.mark.asyncio
async def test_release_fast_path_checks_every_pool_state(blocked_by: str) -> None:
    class Reader:
        def at_eof(self) -> bool:
            return blocked_by == "eof"

    class Writer:
        closed = 0

        def is_closing(self) -> bool:
            return blocked_by == "closing"

        def close(self) -> None:
            self.closed += 1

        async def wait_closed(self) -> None:
            return None

    class CountingCondition:
        entered = 0

        async def __aenter__(self):
            self.entered += 1
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def notify(self, count: int) -> None:
            return None

        def notify_all(self) -> None:
            return None

    client = HTTPClient(
        "release-guard",
        base_url="http://127.0.0.1:8000",
        limits=ClientLimits(max_connections=2, max_keepalive_connections=1),
        destination=_local_policy(),
    )
    condition = CountingCondition()
    connection = http_client_module._Connection(Reader(), Writer())
    client._started = blocked_by != "stopped"
    client._waiters = int(blocked_by == "waiter")
    client._open = 1
    client._active.add(connection)
    client._condition = cast(Any, condition)
    if blocked_by == "keepalive-limit":
        client._idle.append(http_client_module._Connection(Reader(), Writer()))

    await client._release(connection, True)

    assert condition.entered == 1


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
    assert response.header(b"Content-Type") == b"application/json"
    assert received[0].startswith(b"POST /events HTTP/1.1\r\n")
    assert received[1] == b"{}"


@pytest.mark.asyncio
async def test_client_hands_taskless_entry_to_a_task_before_total_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model the native server's eager first step in every execution mode."""

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "taskless-entry",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    real_current_task = asyncio.current_task
    real_sleep = asyncio.sleep
    handed_off = False

    def task_after_handoff(
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> asyncio.Task[Any] | None:
        if not handed_off:
            return None
        return real_current_task(loop)

    async def handoff_sleep(delay: float, result: Any = None) -> Any:
        nonlocal handed_off
        value = await real_sleep(delay, result)
        handed_off = True
        return value

    try:
        await client.start()
        monkeypatch.setattr(asyncio, "current_task", task_after_handoff)
        monkeypatch.setattr(asyncio.tasks, "current_task", task_after_handoff)
        monkeypatch.setattr(asyncio, "sleep", handoff_sleep)
        response = await client.get("/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert response.body == b"ok"
    assert handed_off


@pytest.mark.asyncio
async def test_client_does_not_yield_when_the_request_already_owns_a_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The native handoff is exceptional; ordinary asyncio calls stay direct."""

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def unexpected_handoff(delay: float, result: Any = None) -> Any:
        raise AssertionError(f"task-owned request yielded through sleep({delay}, {result!r})")

    server, port = await _serve(handler)
    client = HTTPClient(
        "task-owned-entry",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        monkeypatch.setattr(asyncio, "sleep", unexpected_handoff)
        response = await client.get("/")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert response.body == b"ok"


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
    # Awaiting the cancelled request is the lifecycle boundary: no retry code
    # owned by that task can continue after it has finished unwinding.
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


@pytest.mark.parametrize(
    ("address", "reason"),
    (
        ("169.254.169.254", "link-local"),
        ("10.0.0.1", "private"),
        ("0.0.0.0", "special"),
        ("::", "special"),
        ("::1", "loopback"),
        ("224.0.0.1", "special"),
        ("100.64.0.1", "non-global"),
        ("64:ff9b::a9fe:a9fe", "link-local"),
    ),
)
def test_destination_policy_rejects_non_global_addresses(
    address: str, reason: str
) -> None:
    with pytest.raises(DestinationRejected, match=reason):
        DestinationPolicy().validate_address(address)


@pytest.mark.parametrize(
    ("address", "reason"),
    (
        ("::127.0.0.1", "loopback"),
        ("::169.254.169.254", "link-local"),
        ("::10.0.0.1", "private"),
    ),
)
def test_ipv4_compatible_ipv6_cannot_hide_a_restricted_destination(
    address: str, reason: str
) -> None:
    """Deprecated IPv4-compatible literals still encode an IPv4 destination.

    `ipaddress` classifies these IPv6 spellings as global and does not expose
    them through `ipv4_mapped`. A stack or translator that honours the embedded
    address must not turn that classification mismatch into SSRF.
    """
    with pytest.raises(DestinationRejected, match=reason):
        DestinationPolicy().validate_address(address)


def test_ipv4_compatible_ipv6_preserves_a_public_destination() -> None:
    DestinationPolicy().validate_address("::8.8.8.8")


def test_ipv4_compatible_ipv6_keeps_policy_exceptions_independent() -> None:
    DestinationPolicy(allow_loopback=True).validate_address("::127.0.0.1")
    DestinationPolicy(allow_link_local=True).validate_address("::169.254.169.254")
    DestinationPolicy(allow_private=True).validate_address("::10.0.0.1")

    with pytest.raises(DestinationRejected, match="loopback"):
        DestinationPolicy(allow_private=True).validate_address("::127.0.0.1")
    with pytest.raises(DestinationRejected, match="link-local"):
        DestinationPolicy(allow_private=True).validate_address("::169.254.169.254")


def test_destination_policy_exceptions_do_not_widen_each_other() -> None:
    DestinationPolicy().validate_address("2001:4860:4860::8888")
    DestinationPolicy(allow_loopback=True).validate_address("127.0.0.1")
    DestinationPolicy(allow_link_local=True).validate_address("169.254.169.254")
    DestinationPolicy(allow_private=True).validate_address("10.0.0.1")

    with pytest.raises(DestinationRejected, match="loopback"):
        DestinationPolicy(allow_private=True).validate_address("127.0.0.1")
    with pytest.raises(DestinationRejected, match="link-local"):
        DestinationPolicy(allow_private=True).validate_address("169.254.169.254")


@pytest.mark.asyncio
async def test_nat64_cannot_translate_a_public_dns_answer_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model the network translation that made the default policy bypassable.

    The listener is real.  Only the NAT64 hop is replaced: before the guard,
    the client sent its request to this loopback service after accepting the
    attacker's globally classified AAAA answer.
    """
    reached = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        reached.set()
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 6\r\n\r\nsecret")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, internal_port = await _serve(handler)
    loop = asyncio.get_running_loop()
    create_connection = loop.create_connection

    async def malicious_dns(
        *_args: object, **_kwargs: object
    ) -> list[tuple[object, ...]]:
        return [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("64:ff9b::7f00:1", 80, 0, 0),
            )
        ]

    async def nat64_connect(
        factory: object, _address: str, _port: int, **kwargs: object
    ) -> tuple[asyncio.BaseTransport, asyncio.BaseProtocol]:
        kwargs.pop("family", None)
        kwargs.pop("flags", None)
        return await create_connection(factory, "127.0.0.1", internal_port, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", malicious_dns)
    monkeypatch.setattr(loop, "create_connection", nat64_connect)
    client = HTTPClient("nat64", base_url="http://attacker.example")
    try:
        await client.start()
        with pytest.raises(DestinationRejected, match="loopback"):
            await client.get("/latest/meta-data")
        assert not reached.is_set()
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_one_unsafe_dns_answer_refuses_the_whole_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy Eyeballs must not race a public answer beside an internal one."""
    connected = False
    loop = asyncio.get_running_loop()

    async def mixed_dns(
        *_args: object, **_kwargs: object
    ) -> list[tuple[object, ...]]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("8.8.8.8", 80),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 80),
            ),
        ]

    async def forbidden_connect(*_args: object, **_kwargs: object) -> None:
        nonlocal connected
        connected = True
        raise AssertionError("the client connected before validating every DNS answer")

    monkeypatch.setattr(loop, "getaddrinfo", mixed_dns)
    monkeypatch.setattr(loop, "create_connection", forbidden_connect)
    client = HTTPClient("mixed-dns", base_url="http://attacker.example")
    try:
        await client.start()
        with pytest.raises(DestinationRejected, match="loopback"):
            await client.get("/")
    finally:
        await client.close()

    assert not connected


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

    def build_ssl_context(_self: HTTPClient) -> ssl.SSLContext:
        nonlocal contexts
        contexts += 1
        return cast(ssl.SSLContext, object())

    class Stream:
        def __init__(self, *, limit: int) -> None:
            connection_limits.append(limit)

    async def create_connection(
        factory: Callable[[], object], *_args: object, **_kwargs: object
    ) -> tuple[asyncio.Transport, object]:
        return cast(asyncio.Transport, object()), factory()

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    # Counted at the client's own builder rather than at `ssl` module level:
    # the context is native where the reactor can provide one, so the module
    # function is no longer the seam. What is under test -- built once, not per
    # request -- is unchanged.
    monkeypatch.setattr(HTTPClient, "_build_ssl_context", build_ssl_context)
    monkeypatch.setattr("wreath.http_client._ClientStream", Stream)
    monkeypatch.setattr(loop, "create_connection", create_connection)

    limits = ClientLimits(read_high_water=12345, dns_cache_ttl=30)
    client = HTTPClient("cached", base_url="https://example.com", limits=limits)
    await client._connect()
    await client._connect()

    assert resolutions == 1
    assert contexts == 1
    assert connection_limits == [12345, 12345]


@pytest.mark.asyncio
async def test_dns_cache_ttl_starts_after_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    clock = 10.0
    resolutions = 0

    async def getaddrinfo(
        *_args: object, **_kwargs: object
    ) -> list[tuple[object, ...]]:
        nonlocal clock, resolutions
        resolutions += 1
        clock += 2.0
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 80),
            )
        ]

    monkeypatch.setattr(loop, "time", lambda: clock)
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    client = HTTPClient(
        "dns-ttl",
        base_url="http://example.com",
        limits=ClientLimits(dns_cache_ttl=30),
    )

    await client._resolve()

    assert client._dns_expires_at == 42.0
    clock = 41.0
    await client._resolve()
    assert resolutions == 1
    clock = 42.0
    await client._resolve()
    assert resolutions == 2


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

    class Stream:
        def __init__(self, *, limit: int) -> None:
            self.limit = limit

    async def create_connection(
        factory: Callable[[], object], address: str, _port: int, **_kwargs: object
    ) -> tuple[asyncio.Transport, object]:
        attempted.append(address)
        if address == "8.8.8.8":
            await slow.wait()
        return cast(asyncio.Transport, object()), factory()

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr("wreath.http_client._ClientStream", Stream)
    # Winner selection is the contract here, not the production stagger.  A
    # separate slow first attempt still proves the second address is raced.
    monkeypatch.setattr("wreath.http_client._HAPPY_EYEBALLS_DELAY", 0)
    monkeypatch.setattr(loop, "create_connection", create_connection)

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


def test_the_public_surface_exports_nothing_unraisable() -> None:
    """`ProxyError` was exported by a client with no proxy support.

    An exception nothing can raise is a `except` clause that never fires, and
    a reader cannot tell it apart from one that guards a real failure mode.
    """
    import wreath.http_client as http_client

    assert "ProxyError" not in http_client.__all__
    assert not hasattr(http_client, "ProxyError")


def test_redirect_policy_offers_no_flag_it_does_not_honour() -> None:
    """`allow_cross_origin` changed the message and nothing else."""
    with pytest.raises(TypeError):
        RedirectPolicy(enabled=True, max_hops=2, allow_cross_origin=True)  # type: ignore[call-arg]

    assert not hasattr(RedirectPolicy(), "allow_cross_origin")


@pytest.mark.asyncio
async def test_cross_origin_redirect_is_refused_with_one_reason() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 302 Found\r\nlocation: http://127.0.0.1:9/elsewhere\r\n"
                    b"content-length: 0\r\n\r\n"
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "crossorigin",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
        redirect=RedirectPolicy(enabled=True, max_hops=2),
    )
    try:
        await client.start()
        with pytest.raises(RedirectError, match="separately configured client"):
            await client.get("/go")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
