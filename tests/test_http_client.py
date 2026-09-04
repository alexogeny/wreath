from __future__ import annotations

import asyncio
import random
import socket
import ssl
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import urlsplit

import pytest

import wreath.http_client as http_client_module
from wreath import Wreath
from wreath.http_client import (
    ClientClosed,
    ClientError,
    ClientLimits,
    ClientTimeout,
    ClientTLS,
    DestinationPolicy,
    DestinationRejected,
    DNSFailure,
    HTTPClient,
    PoolTimeout,
    ProtocolError,
    RatePolicy,
    RedirectError,
    RedirectPolicy,
    RequestTimeout,
    RetryPolicy,
    TracePolicy,
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


def _prepare_native_default(client: HTTPClient, target: str, method: str = "GET") -> object:
    return http_client_module._client_codec.request_default(
        client,
        method,
        target,
        http_client_module.ClientResponse,
        http_client_module.ProtocolError,
        http_client_module.ResponseTooLarge,
        http_client_module.ResponseTimeout,
        http_client_module._TransportError,
        http_client_module.ClientError,
        http_client_module.RequestTimeout,
        (),
        b"",
    )


def _buffered_reader(wire: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(wire)
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_timed_handles_bytes_finished_futures_and_coroutines() -> None:
    loop = asyncio.get_running_loop()
    finished = loop.create_future()
    finished.set_result(b"future")
    pending = loop.create_future()
    loop.call_soon(pending.set_result, b"pending")

    async def later() -> bytes:
        return b"coroutine"

    assert await http_client_module._timed(b"bytes", 1) == b"bytes"
    assert await http_client_module._timed(finished, 1) == b"future"
    assert await http_client_module._timed(pending, 1) == b"pending"
    assert await http_client_module._timed(later(), 1) == b"coroutine"


@pytest.mark.asyncio
async def test_request_timed_enters_a_configured_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered: list[float] = []

    class Deadline:
        def __init__(self, seconds: float) -> None:
            self.seconds = seconds

        async def __aenter__(self) -> None:
            entered.append(self.seconds)

        async def __aexit__(self, *_exc: object) -> None:
            return None

    async def request_flow(*_args: object, **_kwargs: object) -> object:
        return object()

    monkeypatch.setattr(http_client_module.asyncio, "timeout", Deadline)
    monkeypatch.setattr(HTTPClient, "_request_flow", request_flow)
    client = HTTPClient(
        "total-deadline-unit",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
        timeout=ClientTimeout(total=0.25),
    )

    await client._request_timed("GET", "/", headers=(), body=b"", idempotency_key=None)

    assert entered == [0.25]


def test_ipv6_socket_scope_is_preserved_exactly_once() -> None:
    assert HTTPClient._address(socket.AF_INET6, ("fe80::1", 443, 0, 7)) == "fe80::1%7"
    assert HTTPClient._address(socket.AF_INET6, ("fe80::1%eth0", 443, 0, 7)) == ("fe80::1%eth0")
    assert HTTPClient._address(socket.AF_INET6, ("2001:db8::1", 443, 0, 0)) == ("2001:db8::1")


@pytest.mark.asyncio
async def test_python_response_head_reader_translates_codec_refusals() -> None:
    client = HTTPClient(
        "malformed-head-unit",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
    )
    reader = _buffered_reader(b"not-http\r\n\r\n")

    with pytest.raises(ProtocolError, match="response|status|HTTP"):
        await client._read_head(reader)


@pytest.mark.asyncio
async def test_python_response_body_reader_keeps_content_length_framing() -> None:
    client = HTTPClient(
        "length-body-unit",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
    )
    reader = _buffered_reader(b"body")

    body, framed = await client._read_body(
        reader,
        "GET",
        200,
        [(b"content-length", b"4")],
    )

    assert body == b"body"
    assert framed is True


@pytest.mark.asyncio
async def test_python_chunk_reader_refuses_a_malformed_chunk_terminator() -> None:
    client = HTTPClient(
        "chunk-unit",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
    )
    reader = _buffered_reader(b"1\r\nxNO")

    with pytest.raises(ProtocolError, match="malformed response chunk terminator"):
        await client._read_chunked(reader)


@pytest.mark.asyncio
async def test_chunk_iterator_stops_at_the_empty_trailer_line() -> None:
    client = HTTPClient(
        "chunk-trailer-unit",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
    )

    assert [chunk async for chunk in client._iter_chunked(_buffered_reader(b"0\r\n\r\n"))] == []


@pytest.mark.parametrize("name", (b"content-length", b"transfer-encoding"))
@pytest.mark.asyncio
async def test_buffered_chunk_reader_refuses_framing_fields_in_trailers(name: bytes) -> None:
    client = HTTPClient(
        "chunk-framing-trailer-unit",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
    )

    with pytest.raises(ProtocolError, match="framing field"):
        await client._read_chunked(_buffered_reader(b"0\r\n" + name + b": 1\r\n\r\n"))


@pytest.mark.asyncio
async def test_chunk_iterator_yields_buffered_payload_before_the_chunk_is_complete() -> None:
    client = HTTPClient(
        "chunk-stream-unit",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
    )
    reader = asyncio.StreamReader()
    reader.feed_data(b"10000\r\npartial")
    chunks = client._iter_chunked(reader)

    try:
        assert await asyncio.wait_for(anext(chunks), 0.1) == b"partial"
    finally:
        await chunks.aclose()


@pytest.mark.asyncio
async def test_response_head_skips_informational_status_and_returns_the_final_head() -> None:
    client = HTTPClient(
        "response-head-unit",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
    )
    reader = _buffered_reader(
        b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\ncontent-length: 0\r\n\r\n"
    )

    minor, status, reason, headers = await client._read_head(reader)

    assert (minor, status, reason, headers) == (1, 200, b"OK", [(b"content-length", b"0")])


@pytest.mark.asyncio
async def test_response_head_bounds_informational_responses() -> None:
    client = HTTPClient(
        "informational-bound-unit",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
    )
    reader = _buffered_reader(
        b"HTTP/1.1 103 Early Hints\r\n\r\n" * 17
        + b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\n\r\n"
    )

    with pytest.raises(ProtocolError, match="too many informational responses"):
        await client._read_head(reader)


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
async def test_non_reusable_release_closes_the_connection() -> None:
    class Reader:
        @staticmethod
        def at_eof() -> bool:
            return False

    class Writer:
        closed = False
        waited = False

        @staticmethod
        def is_closing() -> bool:
            return False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            self.waited = True

    writer = Writer()
    connection = http_client_module._Connection(Reader(), writer)
    client = HTTPClient(
        "release-close",
        base_url="http://127.0.0.1:8000",
        destination=_local_policy(),
    )
    client._started = True
    client._open = 1
    client._active.add(connection)

    await client._release(connection, False)

    assert writer.closed
    assert writer.waited


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
async def test_reused_default_client_fuses_headers_and_body_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[tuple[bytes, bytes]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        for _ in range(2):
            head = await reader.readuntil(b"\r\n\r\n")
            content_length = 0
            for field in head.split(b"\r\n")[1:]:
                name, separator, value = field.partition(b":")
                if separator and name.lower() == b"content-length":
                    content_length = int(value)
            body = await reader.readexactly(content_length)
            received.append((head, body))
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "fused-preparation",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    try:
        await client.start()
        assert (await client.get("/warm")).body == b"ok"

        async def unexpected_python_preparation(*args: object, **kwargs: object) -> None:
            raise AssertionError("request preparation returned to Python")

        monkeypatch.setattr(HTTPClient, "_request_timed", unexpected_python_preparation)
        response = await client.post(
            "/events",
            headers=((b"content-type", b"application/json"), (b"x-sequence", b"7")),
            body=bytearray(b'{"ready":true}'),
        )
        plan_count = len(client._request_plans)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert response.body == b"ok"
    assert received[1][0].startswith(b"POST /events HTTP/1.1\r\n")
    assert b"content-type: application/json\r\n" in received[1][0]
    assert b"x-sequence: 7\r\n" in received[1][0]
    assert received[1][1] == b'{"ready":true}'
    assert plan_count == 1


@pytest.mark.asyncio
async def test_client_hands_taskless_entry_to_a_task_before_total_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 10**1000])
def test_client_configuration_rejects_non_finite_timing_bounds(value: float | int) -> None:
    factories = (
        lambda: ClientLimits(dns_cache_ttl=value),
        lambda: ClientTimeout(connect=value),
        lambda: RetryPolicy(backoff_base=value),
        lambda: RetryPolicy(backoff_cap=value),
        lambda: RatePolicy(enabled=True, rate=value, capacity=1, max_wait=1),
        lambda: RatePolicy(enabled=True, rate=1, capacity=value, max_wait=1),
        lambda: RatePolicy(enabled=True, rate=1, capacity=1, max_wait=value),
    )
    for factory in factories:
        with pytest.raises(ValueError, match="finite"):
            factory()


def test_retry_statuses_are_snapshotted_at_policy_construction() -> None:
    statuses = {503}
    policy = RetryPolicy(statuses=cast(Any, statuses))

    statuses.clear()
    statuses.add(200)

    assert policy.statuses == frozenset({503})


def test_destination_allowlist_is_snapshotted_at_policy_construction() -> None:
    schemes = {"https"}
    hosts = ["example.com"]
    ports = {443}
    policy = DestinationPolicy(
        schemes=cast(Any, schemes),
        hosts=cast(Any, hosts),
        ports=cast(Any, ports),
    )

    schemes.add("http")
    hosts.append("127.0.0.1")
    ports.add(80)

    with pytest.raises(DestinationRejected):
        policy.validate_url(urlsplit("http://127.0.0.1"))
    assert policy.schemes == frozenset({"https"})
    assert policy.hosts == ("example.com",)
    assert policy.ports == frozenset({443})


@pytest.mark.parametrize(
    ("factory", "message"),
    (
        (lambda: DestinationPolicy(schemes=cast(Any, {1})), "schemes.*strings"),
        (lambda: DestinationPolicy(hosts=cast(Any, (1,))), "hosts.*strings"),
        (lambda: DestinationPolicy(ports=cast(Any, {443.0})), "ports.*integers"),
    ),
)
def test_destination_allowlist_elements_require_declared_types(
    factory: Callable[[], object], message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        factory()


@pytest.mark.parametrize("port", (0, 65536))
def test_destination_allowlist_ports_use_wire_port_range(port: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 65535"):
        DestinationPolicy(ports=frozenset({port}))


@pytest.mark.parametrize(
    "factory",
    (
        lambda: ClientLimits(max_connections=cast(Any, 1.5)),
        lambda: RetryPolicy(attempts=cast(Any, 1.5)),
        lambda: RedirectPolicy(max_hops=cast(Any, 1.5)),
    ),
)
def test_integer_resource_bounds_require_integers(factory: Callable[[], object]) -> None:
    with pytest.raises(TypeError, match="must be an integer"):
        factory()


@pytest.mark.parametrize(
    "factory",
    (
        lambda: ClientTLS(verify=cast(Any, 0)),
        lambda: RetryPolicy(idempotent_only=cast(Any, 0)),
        lambda: RetryPolicy(jitter=cast(Any, "false")),
        lambda: RatePolicy(enabled=cast(Any, "false")),
        lambda: RedirectPolicy(enabled=cast(Any, "false"), max_hops=1),
        lambda: TracePolicy(propagate=cast(Any, "false")),
        lambda: DestinationPolicy(allow_loopback=cast(Any, "false")),
    ),
)
def test_security_flags_require_booleans(factory: Callable[[], object]) -> None:
    with pytest.raises(TypeError, match="must be a boolean"):
        factory()


@pytest.mark.parametrize(
    "factory",
    (
        lambda: ClientLimits(dns_cache_ttl=cast(Any, True)),
        lambda: ClientTimeout(connect=cast(Any, True)),
        lambda: RetryPolicy(backoff_base=cast(Any, True)),
        lambda: RatePolicy(enabled=True, capacity=cast(Any, True), rate=1),
    ),
)
def test_timing_and_rate_bounds_refuse_booleans(factory: Callable[[], object]) -> None:
    with pytest.raises(TypeError, match="must be an int or float"):
        factory()


def test_retry_statuses_require_integer_status_codes() -> None:
    with pytest.raises(TypeError, match="status.*integers"):
        RetryPolicy(statuses=cast(Any, {503.0}))


def test_positive_limit_guard_is_required() -> None:
    with pytest.raises(ValueError, match="positive"):
        ClientLimits(max_connections=0)


def test_valid_trace_policy_does_not_enter_type_refusal() -> None:
    assert TracePolicy(propagate=True, tracestate=False).tracestate is False


@pytest.mark.asyncio
async def test_oversized_request_headers_are_refused_before_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def serialize(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("oversized headers reached the allocating serializer")

    monkeypatch.setattr(http_client_module._client_codec, "serialize_request", serialize)
    client = HTTPClient(
        "request-header-bound",
        base_url="https://example.com",
        limits=ClientLimits(max_request_header_bytes=64),
    )
    await client.start()

    with pytest.raises(ClientError, match="request headers exceed configured limit"):
        await client._request_flow(
            "GET",
            "/",
            headers=((b"x-large", b"x" * 65),),
            body=b"",
            idempotency_key=None,
        )


@pytest.mark.asyncio
async def test_non_bytes_header_buffers_are_refused_before_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def serialize(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("oversized header buffer reached the allocating serializer")

    monkeypatch.setattr(http_client_module._client_codec, "serialize_request", serialize)
    client = HTTPClient(
        "request-header-buffer-bound",
        base_url="https://example.com",
        limits=ClientLimits(max_request_header_bytes=64),
        retry=RetryPolicy(attempts=2),
    )
    await client.start()
    name = memoryview(bytearray(72)).cast("Q")
    value = memoryview(bytearray(72)).cast("Q")

    with pytest.raises(TypeError, match="header names and values must be bytes"):
        await client.get("/", headers=((b"x", cast(Any, value)),))
    with pytest.raises(TypeError, match="header names and values must be bytes"):
        await client.get("/", headers=((cast(Any, name), b"x"),))


@pytest.mark.asyncio
async def test_default_path_bounds_request_headers_before_native_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def request_default(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("oversized headers reached the native allocator")

    monkeypatch.setattr(http_client_module._client_codec, "request_default", request_default)
    client = HTTPClient(
        "native-request-header-bound",
        base_url="https://example.com",
        limits=ClientLimits(max_request_header_bytes=64),
    )
    await client.start()

    with pytest.raises(ClientError, match="request headers exceed configured limit"):
        await client.get("/", headers=((b"x-large", b"x" * 65),))


def test_default_path_bounds_request_target_before_native_allocation() -> None:
    client = HTTPClient(
        "native-request-target-bound",
        base_url="https://example.com",
        limits=ClientLimits(max_request_header_bytes=64),
    )
    with pytest.raises(ClientError, match="request headers exceed configured limit"):
        _prepare_native_default(client, "/" + "x" * 65)

    assert client._request_plans == {}


@pytest.mark.asyncio
async def test_default_headerless_path_skips_aggregate_header_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def validate(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ordinary headerless request entered aggregate-size scan")

    async def response() -> object:
        return http_client_module.ClientResponse(200, (), b"", "1.1")

    def request_default(*_args: object, **_kwargs: object) -> object:
        return response()

    monkeypatch.setattr(HTTPClient, "_validate_request_head_inputs", validate)
    monkeypatch.setattr(http_client_module._client_codec, "request_default", request_default)
    client = HTTPClient("native-request-common", base_url="https://example.com")
    await client.start()

    result = await client.get("/")

    assert result.status == 200


def test_stream_bounds_method_before_uppercase_allocation() -> None:
    client = HTTPClient(
        "stream-method-bound",
        base_url="https://example.com",
        limits=ClientLimits(max_request_header_bytes=64),
    )

    with pytest.raises(ClientError, match="request headers exceed configured limit"):
        client.stream("M" * 65, "/")


@pytest.mark.asyncio
async def test_stream_bounds_headers_before_serialization(monkeypatch: pytest.MonkeyPatch) -> None:
    def serialize(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("oversized stream headers reached the allocating serializer")

    monkeypatch.setattr(http_client_module._client_codec, "serialize_request", serialize)
    client = HTTPClient(
        "stream-header-bound",
        base_url="https://example.com",
        limits=ClientLimits(max_request_header_bytes=64),
    )
    await client.start()

    with pytest.raises(ClientError, match="request headers exceed configured limit"):
        async with client.stream("GET", "/", headers=((b"x-large", b"x" * 65),)):
            pass


@pytest.mark.asyncio
async def test_idempotency_key_is_bounded_before_encoding() -> None:
    class OversizedKey:
        def __bool__(self) -> bool:
            return True

        def __len__(self) -> int:
            return 65

        def encode(self, *_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("oversized idempotency key reached encoding")

    client = HTTPClient(
        "idempotency-key-bound",
        base_url="https://example.com",
        limits=ClientLimits(max_request_header_bytes=64),
    )
    await client.start()

    with pytest.raises(ClientError, match="request headers exceed configured limit"):
        await client._request_flow(
            "POST",
            "/",
            headers=(),
            body=b"",
            idempotency_key=cast(Any, OversizedKey()),
        )


@pytest.mark.asyncio
async def test_policy_path_bounds_method_before_uppercase_allocation() -> None:
    class OversizedMethod:
        def __len__(self) -> int:
            return 65

        def upper(self) -> str:
            raise AssertionError("oversized method reached uppercase allocation")

    client = HTTPClient(
        "policy-method-bound",
        base_url="https://example.com",
        limits=ClientLimits(max_request_header_bytes=64),
    )
    await client.start()

    with pytest.raises(ClientError, match="request headers exceed configured limit"):
        await client._request_flow(
            cast(Any, OversizedMethod()),
            "/",
            headers=(),
            body=b"",
            idempotency_key=None,
        )


def test_request_target_is_bounded_before_ascii_allocation() -> None:
    client = HTTPClient(
        "request-target-bound",
        base_url="https://example.com/api",
        limits=ClientLimits(max_request_header_bytes=64),
    )

    with pytest.raises(ClientError, match="request headers exceed configured limit"):
        client._request_target("/" + "x" * 64)


@pytest.mark.parametrize("target", ("/safe/../admin", "/safe\\admin", "/%2e%2e/admin"))
@pytest.mark.asyncio
async def test_default_native_path_refuses_ambiguous_request_targets(target: str) -> None:
    reached = False

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal reached
        reached = True
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\n\r\n")
        await writer.drain()

    server, port = await _serve(handler)
    client = HTTPClient(
        "native-target-policy",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    await client.start()
    try:
        with pytest.raises(ValueError, match="separator|dot segment|backslash"):
            await client.get(target)
        assert not reached
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "target",
    (
        "/safe/../admin",
        "/safe\\admin",
        "/%2e%2e/admin",
        "/%2Fadmin",
        "/%5cadmin",
    ),
)
def test_fused_native_request_refuses_ambiguous_targets_before_caching(target: str) -> None:
    client = HTTPClient("native-target-unit", base_url="https://example.com")

    with pytest.raises(ValueError, match="separator|dot segment|backslash"):
        _prepare_native_default(client, target)

    assert client._request_plans == {}


def test_fused_native_target_policy_randomized_parity() -> None:
    rng = random.Random(20260903)
    alphabet = "abcXYZ012./\\%?# =&é"
    client = HTTPClient("native-target-parity", base_url="https://example.com")

    for _ in range(2_000):
        target = "/" + "".join(rng.choice(alphabet) for _ in range(rng.randrange(24)))
        try:
            target_bytes = client._request_target(target)
            http_client_module._client_codec.serialize_request(
                "GET", target_bytes, b"example.com", headers=(), body=b""
            )
        except (ClientError, ValueError):
            python_accepts = False
        else:
            python_accepts = True
        try:
            _prepare_native_default(client, target)
        except (ClientError, ValueError):
            native_accepts = False
        else:
            native_accepts = True

        assert native_accepts is python_accepts, target


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
                writer.write(b"HTTP/1.1 302 Found\r\nlocation: /loop\r\ncontent-length: 0\r\n\r\n")
                await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
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
        timeout=ClientTimeout(total=0.25),
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
async def test_redirect_limit_is_checked_before_another_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def redirect(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return http_client_module.ClientResponse(
            302,
            ((b"location", b"/loop"),),
            b"",
            "1.1",
        )

    monkeypatch.setattr(HTTPClient, "_send_with_retries", redirect)
    client = HTTPClient(
        "redirect-limit-unit",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
        redirect=RedirectPolicy(enabled=True, max_hops=1),
    )
    client._started = True

    with pytest.raises(RedirectError, match="limit"):
        await asyncio.wait_for(
            client._request_flow("GET", "/", headers=(), body=b"", idempotency_key=None),
            0.1,
        )

    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redirect", "status"),
    (
        (RedirectPolicy(enabled=False), 302),
        (RedirectPolicy(enabled=True, max_hops=1), 200),
    ),
)
async def test_redirect_flow_requires_both_an_enabled_policy_and_redirect_status(
    monkeypatch: pytest.MonkeyPatch,
    redirect: RedirectPolicy,
    status: int,
) -> None:
    calls = 0

    async def respond(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return http_client_module.ClientResponse(
            status,
            ((b"location", b"/unexpected"),),
            b"",
            "1.1",
        )

    monkeypatch.setattr(HTTPClient, "_send_with_retries", respond)
    monkeypatch.setattr(HTTPClient, "_request_once", respond)
    client = HTTPClient(
        "redirect-gate-unit",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
        redirect=redirect,
    )
    client._started = True

    response = await client._request_flow(
        "GET", "/", headers=(), body=b"", idempotency_key=None
    )

    assert response.status == status
    assert calls == 1


@pytest.mark.asyncio
async def test_total_timeout_bounds_all_retry_attempts_and_backoff() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 503 Unavailable\r\ncontent-length: 0\r\n\r\n")
                await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
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
            await asyncio.wait_for(client.get("/"), 0.25)
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
            b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n2\r\nok\r\n0\r\n\r\n",
            200,
            b"ok",
        ),
        ("GET", b"HTTP/1.1 200 OK\r\n\r\nclose", 200, b"close"),
        ("GET", b"HTTP/1.1 204 No Content\r\n\r\n", 204, b""),
        ("GET", b"HTTP/1.1 304 Not Modified\r\n\r\n", 304, b""),
        (
            "GET",
            b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok",
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
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
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
        (b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\ntransfer-encoding: chunked\r\n\r\n"),
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
        writer.write(f"HTTP/1.1 {status} Bodyless\r\ncontent-length: 99\r\n\r\n".encode())
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
async def test_concurrent_streams_do_not_share_framing_completion_state() -> None:
    release = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        target = head.split(b" ", 2)[1]
        if target == b"/slow":
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\na")
        else:
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
        await writer.drain()
        await release.wait()
        writer.close()
        await writer.wait_closed()

    server, port = await _serve(handler)
    client = HTTPClient(
        "stream-framing-owner",
        base_url=f"http://127.0.0.1:{port}",
        destination=_local_policy(),
    )
    await client.start()
    try:
        async with client.stream("GET", "/slow") as slow:
            chunks = slow.iter_bytes().__aiter__()
            assert await chunks.__anext__() == b"a"
            async with client.stream("GET", "/fast") as fast:
                assert b"".join([chunk async for chunk in fast.iter_bytes()]) == b"ok"

        assert client.snapshot().idle == 1
    finally:
        release.set()
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
                writer.write(b"HTTP/1.1 503 Unavailable\r\ncontent-length: 0\r\n\r\n")
                await writer.drain()
                responded.set()
        except asyncio.IncompleteReadError, ConnectionError:
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
async def test_empty_idempotency_key_cannot_enable_non_idempotent_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = False

    async def send(*_args: object, **_kwargs: object) -> object:
        nonlocal sent
        sent = True
        raise AssertionError("an empty idempotency key reached the transport")

    monkeypatch.setattr(HTTPClient, "_request_once", send)
    client = HTTPClient(
        "empty-idempotency-key",
        base_url="https://example.com",
        retry=RetryPolicy(attempts=2),
    )
    await client.start()

    with pytest.raises(ValueError, match="idempotency_key cannot be empty"):
        await client.post("/charge", idempotency_key="")

    assert not sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "idempotency_key"),
    (
        (RetryPolicy(attempts=2), "charge-1"),
        (RetryPolicy(attempts=2, idempotent_only=False), None),
    ),
)
async def test_non_idempotent_retry_requires_its_configured_safety_condition(
    monkeypatch: pytest.MonkeyPatch,
    policy: RetryPolicy,
    idempotency_key: str | None,
) -> None:
    calls = 0

    async def respond(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return http_client_module.ClientResponse(
            503 if calls == 1 else 200,
            (),
            b"",
            "1.1",
        )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(HTTPClient, "_request_once", respond)
    monkeypatch.setattr(http_client_module.asyncio, "sleep", no_sleep)
    client = HTTPClient(
        "retry-safety-unit",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
        retry=policy,
    )

    response = await client._send_with_retries(
        "POST", b"request", idempotency_key=idempotency_key
    )

    assert response.status == 200
    assert calls == 2


@pytest.mark.asyncio
async def test_idempotency_key_parameter_cannot_duplicate_a_caller_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = False

    async def send(*_args: object, **_kwargs: object) -> object:
        nonlocal sent
        sent = True
        raise AssertionError("ambiguous idempotency keys reached the transport")

    monkeypatch.setattr(HTTPClient, "_request_once", send)
    client = HTTPClient("duplicate-idempotency-key", base_url="https://example.com")
    await client.start()

    with pytest.raises(ValueError, match="both idempotency_key.*header"):
        await client.post(
            "/charge",
            headers=((b"Idempotency-Key", b"caller-value"),),
            idempotency_key="parameter-value",
        )

    assert not sent


@pytest.mark.asyncio
async def test_nonempty_idempotency_key_is_sent_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[bytes] = []

    async def send(_client: HTTPClient, _method: str, request: bytes) -> object:
        requests.append(request)
        return http_client_module.ClientResponse(200, (), b"", "1.1")

    monkeypatch.setattr(HTTPClient, "_request_once", send)
    client = HTTPClient("idempotency-key", base_url="https://example.com")
    await client.start()

    await client.post("/charge", idempotency_key="charge-1")

    assert requests[0].count(b"idempotency-key: charge-1\r\n") == 1


@pytest.mark.asyncio
async def test_redirects_resolve_against_effective_base_path_once() -> None:
    targets: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        for response in (
            b"HTTP/1.1 302 Found\r\nlocation: /api/final\r\ncontent-length: 0\r\n\r\n",
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

    assert targets == [b"/api/start", b"/api/final"]


@pytest.mark.parametrize(
    "location",
    [
        b"/../admin",
        b"/%2e%2e/admin",
        b"/api/%2E%2E/admin",
        b"../admin",
        b"/admin",
        b"/apiary",
        b"https://example.com/../admin",
        b"https://example.com/%2e%2e/admin",
        b"https://example.com/admin",
    ],
)
def test_redirect_target_cannot_escape_configured_base_path(location: bytes) -> None:
    client = HTTPClient("redirect-base", base_url="https://example.com/api")

    with pytest.raises(RedirectError, match="redirect"):
        client._redirect_target("/api/start", location)


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (b"next", "/api/next"),
        (b"/api/final", "/api/final"),
        (b"https://example.com/api/final?ok=1", "/api/final?ok=1"),
    ],
)
def test_redirect_target_preserves_same_origin_base_path(location: bytes, expected: str) -> None:
    client = HTTPClient("redirect-base", base_url="https://example.com/api")

    assert client._redirect_target("/api/start", location) == expected


@pytest.mark.parametrize(
    "base_url",
    (
        "\x00https://example.com",
        " https://example.com",
        "https://exa\tmple.com",
        "https://example.com/\napi",
    ),
)
def test_base_url_refuses_characters_urlsplit_would_discard(base_url: str) -> None:
    with pytest.raises(ValueError, match="control character or space"):
        HTTPClient("ambiguous-url", base_url=base_url)


def test_base_url_refuses_non_ascii_path_at_construction() -> None:
    with pytest.raises(ValueError, match="base_url path must be ASCII/percent-encoded"):
        HTTPClient("unicode-base-path", base_url="https://example.com/café")


@pytest.mark.parametrize(
    "location",
    (
        b"\t/api/final",
        b"/api/fi\tnal",
        b"https://exa\tmple.com/api/final",
    ),
)
def test_redirect_refuses_characters_urlsplit_would_discard(location: bytes) -> None:
    client = HTTPClient("ambiguous-redirect", base_url="https://example.com/api")

    with pytest.raises(RedirectError, match="control character or space"):
        client._redirect_target("/api/start", location)


@pytest.mark.asyncio
async def test_connection_close_token_prevents_pool_reuse() -> None:
    connections = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nconnection: keep-alive, close\r\ncontent-length: 1\r\n\r\nx"
            )
            await writer.drain()
            try:
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 0.2)
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 1\r\n\r\nx")
                await writer.drain()
            except TimeoutError, asyncio.IncompleteReadError, ConnectionError:
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
            b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n" + size + b"\r\nx\r\n0\r\n\r\n"
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
async def test_buffered_chunk_reader_refuses_an_oversized_chunk_line() -> None:
    client = HTTPClient(
        "chunk-line-bound-unit",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
    )
    reader = _buffered_reader(b"1" * 1023 + b"\r\n")

    with pytest.raises(ProtocolError, match="chunk line exceeds limit"):
        await client._read_chunked(reader)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trailer",
    (
        b" bad: value",
        b"bad name: value",
        b"x-test: bad\x01value",
        b"content-length: 1",
        b"transfer-encoding: chunked",
    ),
)
async def test_client_rejects_malformed_response_trailers(trailer: bytes) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"
            b"1\r\nx\r\n0\r\n" + trailer + b"\r\n\r\n"
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
        with pytest.raises(ProtocolError, match="header|folding|framing field"):
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
def test_destination_policy_rejects_non_global_addresses(address: str, reason: str) -> None:
    with pytest.raises(DestinationRejected, match=reason):
        DestinationPolicy().validate_address(address)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://example.com", "scheme 'ftp' is not allowed"),
        ("https://user@example.com", "URL credentials are not allowed"),
        ("https:///path", "destination host is required"),
    ],
)
def test_destination_policy_refuses_each_invalid_url_boundary(url: str, message: str) -> None:
    with pytest.raises(DestinationRejected) as caught:
        DestinationPolicy().validate_url(urlsplit(url))
    assert str(caught.value) == message


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


@pytest.mark.parametrize(
    ("address", "reason"),
    (
        ("2002:7f00:1::", "loopback"),
        ("2002:a9fe:a9fe::", "link-local"),
        ("2001:0000:0808:0808:0000:ffff:80ff:fffe", "loopback"),
        ("2001:0000:0808:0808:0000:ffff:5601:5601", "link-local"),
        ("2001:0000:7f00:0001:0000:ffff:f7f7:f7f7", "loopback"),
        ("2001:0000:a9fe:a9fe:0000:ffff:f7f7:f7f7", "link-local"),
        ("2001:4860:4860:0000:0000:5efe:7f00:0001", "loopback"),
        ("2001:4860:4860:0000:0000:5efe:a9fe:a9fe", "link-local"),
    ),
)
def test_ipv6_transition_addresses_cannot_hide_restricted_ipv4_destinations(
    address: str, reason: str
) -> None:
    with pytest.raises(DestinationRejected, match=reason):
        DestinationPolicy(allow_private=True).validate_address(address)


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

    async def malicious_dns(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
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
    connected = False
    loop = asyncio.get_running_loop()

    async def mixed_dns(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
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
async def test_dns_answer_count_cannot_create_an_unbounded_connection_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()

    async def oversized_dns(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (f"8.8.{index // 254}.{index % 254 + 1}", 80),
            )
            for index in range(65)
        ]

    monkeypatch.setattr(loop, "getaddrinfo", oversized_dns)
    client = HTTPClient("dns-bound", base_url="http://example.com")

    with pytest.raises(DNSFailure, match="too many addresses"):
        await client._resolve()


@pytest.mark.asyncio
async def test_oversized_dns_answer_is_refused_before_address_policy_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()

    async def oversized_dns(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 80),
            )
        ] * 65

    monkeypatch.setattr(loop, "getaddrinfo", oversized_dns)
    client = HTTPClient("dns-bound-order", base_url="http://example.com")

    with pytest.raises(DNSFailure, match="too many addresses"):
        await client._resolve()


@pytest.mark.asyncio
async def test_duplicate_dns_answers_do_not_amplify_connection_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    answer = (
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        "",
        ("8.8.8.8", 80),
    )

    async def duplicate_dns(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [answer] * 64

    monkeypatch.setattr(loop, "getaddrinfo", duplicate_dns)
    client = HTTPClient("dns-deduplicate", base_url="http://example.com")

    assert await client._resolve() == (answer,)


@pytest.mark.asyncio
async def test_streaming_requests_spend_a_configured_rate_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    throttled = False

    async def throttle(_client: HTTPClient) -> None:
        nonlocal throttled
        throttled = True
        raise ClientError("rate token unavailable")

    async def acquire(_client: HTTPClient) -> object:
        raise AssertionError("stream acquired a connection before rate limiting")

    monkeypatch.setattr(HTTPClient, "_throttle", throttle)
    monkeypatch.setattr(HTTPClient, "_acquire", acquire)
    client = HTTPClient(
        "stream-rate",
        base_url="https://example.com",
        rate=RatePolicy(enabled=True, capacity=1, rate=1),
    )
    await client.start()

    with pytest.raises(ClientError, match="rate token unavailable"):
        async with client.stream("GET", "/events"):
            pass

    assert throttled


@pytest.mark.asyncio
async def test_stream_without_rate_policy_skips_throttle_coroutine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def throttle(_client: HTTPClient) -> None:
        raise AssertionError("disabled rate policy entered the throttle coroutine")

    async def acquire(_client: HTTPClient) -> object:
        raise ClientError("acquire reached")

    monkeypatch.setattr(HTTPClient, "_throttle", throttle)
    monkeypatch.setattr(HTTPClient, "_acquire", acquire)
    client = HTTPClient("stream-no-rate", base_url="https://example.com")
    await client.start()

    with pytest.raises(ClientError, match="acquire reached"):
        async with client.stream("GET", "/events"):
            pass


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

    async def getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
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
    import wreath.http_client as http_client

    assert "ProxyError" not in http_client.__all__
    assert not hasattr(http_client, "ProxyError")


def test_redirect_policy_offers_no_flag_it_does_not_honour() -> None:
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
        except asyncio.IncompleteReadError, ConnectionError:
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
