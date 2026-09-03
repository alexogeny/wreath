from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from wreath._agents.http_transport import (
    HTTPClientTransport,
    MCPHTTPClientTransport,
    _absolute_url,
    _origin,
)
from wreath.http_client import HTTPClient


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


@dataclass
class Response:
    status: int = 200
    headers: tuple[tuple[bytes, bytes], ...] = (
        (b"x-request-id", b"req-1"),
        (b"x-detail", b"caf\xe9"),
    )
    body: AsyncIterator[bytes] = field(default_factory=lambda: chunks(b"one", b"two"))

    def iter_bytes(self) -> AsyncIterator[bytes]:
        return self.body


@dataclass
class StreamContext:
    response: Response
    entered: int = 0
    exits: list[type[BaseException] | None] = field(default_factory=list)

    async def __aenter__(self) -> Response:
        self.entered += 1
        return self.response

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: Any,
    ) -> bool:
        self.exits.append(error_type)
        return False


@dataclass
class Client:
    origin: str = "https://api.example"
    contexts: list[StreamContext] = field(default_factory=list)
    requests: list[tuple[str, str, tuple[tuple[bytes, bytes], ...], bytes]] = field(
        default_factory=list
    )

    def stream(
        self,
        method: str,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
        body: bytes = b"",
    ) -> StreamContext:
        self.requests.append((method, target, headers, body))
        context = self.contexts.pop(0)
        return context


def transport(client: Client, base_url: str = "https://api.example/v1") -> HTTPClientTransport:
    return HTTPClientTransport(cast(HTTPClient, client), base_url=base_url)


@pytest.mark.parametrize(
    "value",
    [
        "ftp://api.example/path",
        "https:///path",
        "https://user@api.example/path",
        "https://user:password@api.example/path",
        "https://api.example:0/path",
        "https://api.example/path#fragment",
    ],
)
def test_absolute_url_rejects_each_invalid_authority_part(value: str) -> None:
    with pytest.raises(ValueError, match="absolute HTTP URL"):
        _absolute_url(value, label="test URL")


def test_origin_normalizes_ipv6_and_default_ports() -> None:
    assert _origin(_absolute_url("http://[::1]:80", label="test URL")) == "http://[::1]"
    assert (
        _origin(_absolute_url("https://[::1]:443", label="test URL"))
        == "https://[::1]"
    )
    assert (
        _origin(_absolute_url("https://[::1]:8443", label="test URL"))
        == "https://[::1]:8443"
    )


def test_origin_refuses_a_split_result_without_a_host() -> None:
    with pytest.raises(ValueError, match="include a host"):
        _origin(_absolute_url("https://api.example", label="test URL")._replace(netloc=""))


def test_model_transport_base_url_refuses_a_query() -> None:
    with pytest.raises(ValueError, match="must not include a query"):
        transport(Client(), "https://api.example/v1?version=1")


async def test_streams_without_body_copies_and_closes_after_exhaustion() -> None:
    first = b"first"
    second = b"second"
    context = StreamContext(Response(body=chunks(first, second)))
    client = Client(contexts=[context])
    adapter = transport(client)

    status, headers, body = await adapter(
        "POST",
        "https://api.example/v1/responses?stream=true",
        {"authorization": "Bearer secret", "x-detail": "caf\xe9"},
        b"request",
    )
    received = [chunk async for chunk in body]

    assert status == 200
    assert headers == {"x-request-id": "req-1", "x-detail": "caf\xe9"}
    assert received[0] is first
    assert received[1] is second
    assert client.requests == [
        (
            "POST",
            "/responses?stream=true",
            ((b"authorization", b"Bearer secret"), (b"x-detail", b"caf\xe9")),
            b"request",
        )
    ]
    assert context.entered == 1
    assert context.exits == [None]


async def test_refuses_off_origin_and_outside_base_path_before_client() -> None:
    client = Client()
    adapter = transport(client)

    with pytest.raises(ValueError, match="configured origin"):
        await adapter("POST", "https://attacker.example/v1/responses", {}, b"")
    with pytest.raises(ValueError, match="configured base URL"):
        await adapter("POST", "https://api.example/v10/responses", {}, b"")

    assert client.requests == []


async def test_early_body_close_exits_the_owned_stream_context() -> None:
    context = StreamContext(Response(body=chunks(b"first", b"second")))
    client = Client(contexts=[context])
    adapter = transport(client)

    _status, _headers, body = await adapter(
        "POST", "https://api.example/v1/responses", {}, b"request"
    )
    assert await anext(body) == b"first"
    await body.aclose()

    assert context.exits == [GeneratorExit]

    unopened = StreamContext(Response(body=chunks(b"unused")))
    client.contexts.append(unopened)
    _status, _headers, never_started = await adapter(
        "POST", "https://api.example/v1/responses", {}, b"request"
    )
    await never_started.aclose()

    assert unopened.exits == [GeneratorExit]


async def test_body_error_and_cancellation_close_the_stream_context() -> None:
    async def fails() -> AsyncIterator[bytes]:
        yield b"visible"
        raise OSError("disconnected")

    failed_context = StreamContext(Response(body=fails()))
    gate = asyncio.Event()

    async def waits() -> AsyncIterator[bytes]:
        yield b"visible"
        await gate.wait()
        yield b"never"

    cancelled_context = StreamContext(Response(body=waits()))
    client = Client(contexts=[failed_context, cancelled_context])
    adapter = transport(client)

    _status, _headers, failed_body = await adapter(
        "POST", "https://api.example/v1/responses", {}, b""
    )
    with pytest.raises(OSError, match="disconnected"):
        [chunk async for chunk in failed_body]
    assert failed_context.exits == [OSError]

    _status, _headers, cancelled_body = await adapter(
        "POST", "https://api.example/v1/responses", {}, b""
    )

    async def consume() -> None:
        async for _chunk in cancelled_body:
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled_context.exits == [asyncio.CancelledError]


async def test_refuses_client_origin_mismatch_and_unrepresentable_headers() -> None:
    with pytest.raises(ValueError, match="HTTPClient origin"):
        transport(Client(origin="https://other.example"))

    context = StreamContext(Response())
    client = Client(contexts=[context])
    adapter = transport(client)

    with pytest.raises(ValueError, match="ISO-8859-1"):
        await adapter(
            "POST",
            "https://api.example/v1/responses",
            {"x-snowman": "\N{SNOWMAN}"},
            b"",
        )
    assert client.requests == []


async def test_response_header_conversion_error_closes_entered_context() -> None:
    context = StreamContext(Response(headers=((b"x-\xff", b"value"),)))
    client = Client(contexts=[context])
    adapter = transport(client)

    with pytest.raises(ValueError, match="response header names"):
        await adapter("POST", "https://api.example/v1/responses", {}, b"")

    assert context.exits == [ValueError]


async def test_mcp_adapter_uses_one_bounded_nonredirecting_stream() -> None:
    first = b'{"jsonrpc":"2.0",'
    second = b'"id":1,"result":{}}'
    context = StreamContext(Response(body=chunks(first, second)))
    client = Client(contexts=[context])
    adapter = MCPHTTPClientTransport(
        cast(HTTPClient, client),
        endpoint="https://api.example/mcp",
    )

    response = await adapter.request(
        "POST",
        "https://api.example/mcp",
        headers={"content-type": "application/json"},
        body=b"request",
        max_response_bytes=64,
    )

    assert adapter.origin == "https://api.example"
    assert response.url == "https://api.example/mcp"
    assert response.status == 200
    assert response.body == first + second
    assert client.requests == [
        (
            "POST",
            "/mcp",
            ((b"content-type", b"application/json"),),
            b"request",
        )
    ]
    assert context.exits == [None]
    await adapter.close()


async def test_mcp_adapter_refuses_wrong_endpoint_and_over_limit_body() -> None:
    context = StreamContext(Response(body=chunks(b"1234", b"5")))
    client = Client(contexts=[context])
    adapter = MCPHTTPClientTransport(
        cast(HTTPClient, client),
        endpoint="https://api.example/mcp",
    )

    with pytest.raises(ValueError, match="configured endpoint"):
        await adapter.request(
            "POST",
            "https://other.example/mcp",
            headers={},
            body=None,
            max_response_bytes=4,
        )
    with pytest.raises(ValueError, match="exceeds 4 bytes"):
        await adapter.request(
            "POST",
            "https://api.example/mcp",
            headers={},
            body=None,
            max_response_bytes=4,
        )

    assert len(client.requests) == 1
    assert context.exits == [ValueError]


def test_mcp_adapter_refuses_insecure_or_mismatched_origins() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        MCPHTTPClientTransport(
            cast(HTTPClient, Client(origin="http://api.example")),
            endpoint="http://api.example/mcp",
        )
    with pytest.raises(ValueError, match="must match HTTPClient origin"):
        MCPHTTPClientTransport(
            cast(HTTPClient, Client(origin="https://other.example")),
            endpoint="https://api.example/mcp",
        )


@pytest.mark.parametrize(
    ("endpoint", "target"),
    [
        ("https://api.example", "/"),
        ("https://api.example/mcp?token=one", "/mcp?token=one"),
    ],
)
async def test_mcp_adapter_preserves_root_and_query_targets(
    endpoint: str, target: str
) -> None:
    context = StreamContext(Response(body=chunks(b"{}")))
    client = Client(contexts=[context])
    adapter = MCPHTTPClientTransport(cast(HTTPClient, client), endpoint=endpoint)

    await adapter.request(
        "POST",
        endpoint,
        headers={},
        body=None,
        max_response_bytes=2,
    )

    assert client.requests == [("POST", target, (), b"")]
