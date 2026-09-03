from __future__ import annotations

from typing import Any

import pytest

from wreath.edge import ReverseProxy, Upstream, UpstreamPool
from wreath.edge.proxy import _proxy_error
from wreath.http_client import (
    ClientClosed,
    ConnectError,
    DestinationRejected,
    DNSFailure,
    PoolTimeout,
    ProtocolError,
    RequestTimeout,
    ResponseTimeout,
    TLSFailure,
)


class _Request:
    method = "GET"
    path = "/resource"
    query_string = b""
    client = ("127.0.0.1", 1234)
    scheme = "http"

    def __init__(self, incremental: str | None = None) -> None:
        self.headers = [] if incremental is None else [(b"incremental", incremental.encode())]
        self._incremental = incremental

    def header(self, name: str, default: Any = None) -> Any:
        if name == "incremental":
            return self._incremental
        return default


class _FailedStream:
    async def __aenter__(self) -> Any:
        raise ConnectError("origin refused the connection")


class _FailedClient:
    def stream(self, *args: Any, **kwargs: Any) -> _FailedStream:
        return _FailedStream()


class _RecordingFailedClient(_FailedClient):
    def __init__(self) -> None:
        self.targets: list[str] = []

    def stream(self, method: str, target: str, **kwargs: Any) -> _FailedStream:
        self.targets.append(target)
        return super().stream(method, target, **kwargs)


class _PostRequest(_Request):
    method = "POST"

    def __init__(self, idempotency_key: str | None) -> None:
        super().__init__()
        self.idempotency_key = idempotency_key

    async def body(self) -> bytes:
        return b""

    def header(self, name: str, default: Any = None) -> Any:
        if name == "idempotency-key":
            return self.idempotency_key
        return super().header(name, default)


class _UpstreamResponse:
    status = 200

    def __init__(self, incremental: bytes) -> None:
        self.headers = [(b"incremental", incremental)]

    def header(self, name: bytes) -> bytes | None:
        return None

    async def iter_bytes(self):
        yield b"one"


class _OpenStream:
    def __init__(self, incremental: bytes) -> None:
        self.response = _UpstreamResponse(incremental)

    async def __aenter__(self) -> _UpstreamResponse:
        return self.response

    async def __aexit__(self, *args: Any) -> None:
        return None


class _OpenClient:
    def __init__(self, incremental: bytes) -> None:
        self.incremental = incremental

    def stream(self, *args: Any, **kwargs: Any) -> _OpenStream:
        return _OpenStream(self.incremental)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (DNSFailure("dns"), "dns_error"),
        (TLSFailure("tls"), "tls_protocol_error"),
        (DestinationRejected("destination"), "destination_ip_prohibited"),
        (PoolTimeout("pool"), "connection_limit_reached"),
        (RequestTimeout("request"), "connection_timeout"),
        (ResponseTimeout("response"), "connection_read_timeout"),
        (ProtocolError("protocol"), "http_protocol_error"),
        (ConnectError("connect"), "destination_unavailable"),
        (ClientClosed("closed"), "proxy_internal_error"),
        (OSError("socket"), "connection_terminated"),
    ],
)
def test_proxy_error_reports_each_transport_failure(error: Any, expected: str) -> None:
    assert _proxy_error(error) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["?1", "?1;wait", " ?1;reason=stream "])
async def test_asgi_proxy_refuses_incremental_messages_before_upstream_selection(
    value: str,
) -> None:
    upstream = Upstream("http://origin")
    clients: dict[str, Any] = {upstream.url: _FailedClient()}
    proxy = ReverseProxy(UpstreamPool([upstream]), clients, via_name="edge")

    response = await proxy(_Request(value))

    assert response.status == 501
    assert dict(response.headers)[b"proxy-status"] == b"edge;error=incremental_refused"
    assert upstream.total == 0


@pytest.mark.asyncio
async def test_asgi_proxy_ignores_duplicate_incremental_fields() -> None:
    upstream = Upstream("http://origin")
    clients: dict[str, Any] = {upstream.url: _FailedClient()}
    proxy = ReverseProxy(UpstreamPool([upstream]), clients, via_name="edge", attempts=1)
    request = _Request("?1")
    request.headers.append((b"incremental", b"?0"))

    response = await proxy(request)

    assert response.status == 502
    assert upstream.failures == 1


@pytest.mark.asyncio
async def test_asgi_proxy_explains_an_upstream_connection_failure() -> None:
    upstream = Upstream("http://origin")
    clients: dict[str, Any] = {upstream.url: _FailedClient()}
    proxy = ReverseProxy(
        UpstreamPool([upstream]),
        clients,
        via_name="edge",
        attempts=1,
    )

    response = await proxy(_Request())

    assert response.status == 502
    assert dict(response.headers)[b"proxy-status"] == b"edge;error=destination_unavailable"


@pytest.mark.asyncio
async def test_asgi_proxy_forwards_query_strings_without_a_dangling_separator() -> None:
    upstream = Upstream("http://origin")
    client = _RecordingFailedClient()
    proxy = ReverseProxy(
        UpstreamPool([upstream]), {upstream.url: client}, via_name="edge", attempts=1
    )
    request = _Request()
    request.query_string = b"page=2"

    await proxy(request)
    request.query_string = b""
    await proxy(request)

    assert client.targets == ["/resource?page=2", "/resource"]


@pytest.mark.asyncio
async def test_asgi_proxy_retries_a_post_only_with_an_idempotency_key() -> None:
    first = Upstream("http://first")
    second = Upstream("http://second")
    clients = {first.url: _FailedClient(), second.url: _FailedClient()}

    retrying = ReverseProxy(UpstreamPool([first, second]), clients, attempts=2)
    await retrying(_PostRequest("request-1"))
    assert first.failures + second.failures == 2

    first = Upstream("http://first")
    second = Upstream("http://second")
    clients = {first.url: _FailedClient(), second.url: _FailedClient()}
    single = ReverseProxy(UpstreamPool([first, second]), clients, attempts=2)
    await single(_PostRequest(None))
    assert first.failures + second.failures == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"?1", b"?0"])
async def test_asgi_proxy_preserves_an_upstream_incremental_preference(value: bytes) -> None:
    upstream = Upstream("http://origin")
    clients: dict[str, Any] = {upstream.url: _OpenClient(value)}
    proxy = ReverseProxy(UpstreamPool([upstream]), clients, attempts=1)

    response = await proxy(_Request())

    assert [header for header in response.headers if header[0] == b"incremental"] == [
        (b"incremental", value)
    ]
