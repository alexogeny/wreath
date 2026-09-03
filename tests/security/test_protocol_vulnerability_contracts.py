from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import pytest
from http3.conftest import curl_http3, make_self_signed_cert, requires_curl_h3, requires_h3

from wreath._native import _edge
from wreath.edge.headers import append_forwarded
from wreath.edge.serve import serve as serve_edge
from wreath.edge.upstream import Upstream, UpstreamPool
from wreath.http_client import (
    ClientLimits,
    ClientTimeout,
    DestinationPolicy,
    HTTPClient,
    ProtocolError,
)
from wreath.request import Request
from wreath.server import ServerConfig, TLSConfig
from wreath.server import serve as serve_server
from wreath.websocket import WebSocket


class _Transport:
    def __init__(self) -> None:
        self.closed = False
        self.read_paused = False
        self.pause_reading_calls = 0
        self.resume_reading_calls = 0
        self.writes: list[bytes] = []

    def get_extra_info(self, name: str) -> object:
        return ("127.0.0.1", 1234) if name == "peername" else None

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    def close(self) -> None:
        self.closed = True

    def pause_reading(self) -> None:
        self.read_paused = True
        self.pause_reading_calls += 1

    def resume_reading(self) -> None:
        self.read_paused = False
        self.resume_reading_calls += 1


def _edge_exchange(response: bytes) -> tuple[_Transport, _Transport]:
    table = _edge.UpstreamTable([b"origin.test"], b"1.1 wreath", b"http")
    client_transport = _Transport()
    client = _edge.EdgeProtocol(table)
    client.connection_made(client_transport)
    upstream_transport = _Transport()
    upstream = _edge.UpstreamConnection(table, 0)
    upstream.connection_made(upstream_transport)
    client.data_received(b"GET / HTTP/1.1\r\nHost: example.test\r\n\r\n")
    assert upstream_transport.writes
    upstream.data_received(response)
    return client_transport, upstream_transport


def test_item_2_native_edge_bounds_the_upstream_wait_queue() -> None:
    table = _edge.UpstreamTable([b"origin.test"], b"1.1 wreath", b"http", max_waiting=1)
    upstream_transport = _Transport()
    upstream = _edge.UpstreamConnection(table, 0)
    upstream.connection_made(upstream_transport)

    clients = []
    for _ in range(3):
        transport = _Transport()
        client = _edge.EdgeProtocol(table)
        client.connection_made(transport)
        client.data_received(b"GET / HTTP/1.1\r\nHost: example.test\r\n\r\n")
        clients.append(transport)

    assert table.stats()["waiting"] == 1
    assert b"503 Service Unavailable" in b"".join(clients[2].writes)
    assert b"connection_limit_reached" in b"".join(clients[2].writes)


def test_item_2_native_edge_expires_a_queued_request() -> None:
    loop = asyncio.new_event_loop()
    try:
        table = _edge.UpstreamTable(
            [b"origin.test"],
            b"1.1 wreath",
            b"http",
            max_waiting=1,
            queue_timeout=0.01,
            loop=loop,
        )
        upstream_transport = _Transport()
        upstream = _edge.UpstreamConnection(table, 0)
        upstream.connection_made(upstream_transport)
        busy = _edge.EdgeProtocol(table)
        busy.connection_made(_Transport())
        busy.data_received(b"GET / HTTP/1.1\r\nHost: example.test\r\n\r\n")
        waiting_transport = _Transport()
        waiting = _edge.EdgeProtocol(table)
        waiting.connection_made(waiting_transport)
        waiting.data_received(b"GET / HTTP/1.1\r\nHost: example.test\r\n\r\n")

        loop.run_until_complete(asyncio.sleep(0.03))

        response = b"".join(waiting_transport.writes)
        assert b"503 Service Unavailable" in response
        assert b"connection_timeout" in response
        assert table.stats()["waiting"] == 0
    finally:
        loop.close()


def test_item_2_native_edge_refuses_invalid_queue_policy() -> None:
    pool = UpstreamPool([Upstream("http://127.0.0.1:1")])
    with pytest.raises(ValueError, match="max_waiting must be at least 1"):
        asyncio.run(serve_edge(pool, max_waiting=0))
    with pytest.raises(ValueError, match="^queue_timeout must be finite and positive$"):
        asyncio.run(serve_edge(pool, queue_timeout=0))


def test_item_2_native_edge_accepts_a_positive_queue_timeout() -> None:
    pool = UpstreamPool([Upstream("ftp://invalid.test")])
    with pytest.raises(ValueError, match=r"speaks http:// or https://"):
        asyncio.run(serve_edge(pool, queue_timeout=0.01))


@pytest.mark.asyncio
async def test_item_2_native_edge_passes_bounded_queue_defaults_to_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("wreath.edge.serve")

    captured: dict[str, Any] = {}

    class Table:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            pass

    class Loop:
        async def create_connection(self, *args: Any, **kwargs: Any) -> None:
            raise OSError("stop after table construction")

    monkeypatch.setattr(module._edge, "UpstreamTable", Table)
    monkeypatch.setattr(module.asyncio, "get_running_loop", lambda: Loop())
    with pytest.raises(OSError, match="stop after table construction"):
        await module.serve(UpstreamPool([Upstream("http://127.0.0.1:1")]))
    assert captured["max_waiting"] == 1024
    assert captured["queue_timeout"] == 30.0


def test_item_3_h2_request_timeout_cancels_and_answers() -> None:
    from http2 import support
    from http2.conftest import H2Driver

    blocker = asyncio.Event()

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await blocker.wait()

    async def drive() -> list[tuple[bytes, bytes]]:
        driver = H2Driver(
            app,
            ServerConfig(protocols=("h2",), request_timeout=0.01),
        )
        await driver.preface()
        await driver.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
        await asyncio.sleep(0.03)
        decoder = support.HpackDecoder()
        return [
            pair
            for frame in driver.frames()
            if frame.type == support.HEADERS
            for pair in decoder.decode(frame.payload)
        ]

    headers = asyncio.run(drive())
    assert dict(headers)[b":status"] == b"408"


def test_http2_keep_alive_timeout_closes_an_incomplete_header_block() -> None:
    from http2 import support
    from http2.conftest import H2Driver

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        raise AssertionError("an incomplete header block must not activate ASGI")

    async def drive() -> bool:
        driver = H2Driver(
            app,
            ServerConfig(protocols=("h2",), keep_alive_timeout=0.01),
        )
        try:
            await driver.preface()
            await driver.feed_and_settle(
                support.build_headers_frame(
                    1,
                    support.request_headers(),
                    end_headers=False,
                )
            )
            await asyncio.sleep(0.03)
            return driver.transport.closed
        finally:
            driver.close()

    assert asyncio.run(drive())


def test_http2_ignored_priority_update_frames_share_the_flood_budget() -> None:
    from http2 import support
    from http2.conftest import H2Driver

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        raise AssertionError("control frames must not activate ASGI")

    async def drive() -> list[int]:
        driver = H2Driver(app)
        try:
            await driver.preface()
            ignored = support.encode_frame(0x10, 0, 0)
            await driver.feed_and_settle(ignored * 200)
            return [
                int.from_bytes(frame.payload[4:8], "big")
                for frame in driver.frames()
                if frame.type == support.GOAWAY
            ]
        finally:
            driver.close()

    assert support.ENHANCE_YOUR_CALM in asyncio.run(drive())


def test_http2_rejects_a_second_request_header_block_on_an_active_stream() -> None:
    from http2 import support
    from http2.conftest import H2Driver

    activations: list[bytes] = []
    blocker = asyncio.Event()

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        activations.append(scope["raw_path"])
        await blocker.wait()

    async def drive() -> tuple[list[bytes], bool]:
        driver = H2Driver(app)
        try:
            await driver.preface()
            await driver.feed_and_settle(
                support.build_headers_frame(
                    1,
                    support.request_headers(path=b"/first"),
                    end_stream=False,
                )
            )
            await driver.feed_and_settle(
                support.build_headers_frame(
                    1,
                    support.request_headers(path=b"/second"),
                )
            )
            frames = driver.frames()
            rejected = any(
                frame.type == support.RST_STREAM and frame.stream_id == 1 for frame in frames
            ) or any(frame.type == support.GOAWAY for frame in frames)
            return activations, rejected
        finally:
            driver.close()

    seen, rejected = asyncio.run(drive())
    assert seen == [b"/first"]
    assert rejected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    (b"http://victim.test/private", b"victim.test:443", b"*", b"/private#public"),
)
async def test_http2_refuses_non_origin_request_targets_before_app_activation(
    target: bytes,
) -> None:
    from http2 import support
    from http2.conftest import H2Driver

    activated: list[bytes] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        activated.append(scope["raw_path"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    driver = H2Driver(app)
    await driver.preface()
    await driver.feed_and_settle(
        support.build_headers_frame(1, support.request_headers(path=target))
    )

    assert not activated
    assert any(
        frame.type == support.RST_STREAM
        and int.from_bytes(frame.payload, "big") == support.PROTOCOL_ERROR
        for frame in driver.frames()
    )


@pytest.mark.asyncio
async def test_http2_preserves_options_asterisk_form() -> None:
    from http2 import support
    from http2.conftest import H2Driver

    activated: list[tuple[str, bytes]] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        activated.append((scope["method"], scope["raw_path"]))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    driver = H2Driver(app)
    await driver.preface()
    await driver.feed_and_settle(
        support.build_headers_frame(
            1,
            support.request_headers(method=b"OPTIONS", path=b"*"),
        )
    )

    assert activated == [("OPTIONS", b"*")]


@pytest.mark.asyncio
async def test_http2_refuses_nonzero_content_length_on_header_end_stream() -> None:
    from http2 import support
    from http2.conftest import H2Driver

    activated = False

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal activated
        activated = True

    driver = H2Driver(app)
    await driver.preface()
    await driver.feed_and_settle(
        support.build_headers_frame(
            1,
            support.request_headers(extra=[(b"content-length", b"1")]),
            end_stream=True,
        )
    )

    assert not activated
    assert any(
        frame.type == support.RST_STREAM
        and int.from_bytes(frame.payload, "big") == support.PROTOCOL_ERROR
        for frame in driver.frames()
    )


def test_http2_scope_keeps_connection_addresses_for_client_isolation() -> None:
    from http2 import support
    from http2.conftest import H2Driver

    captured: list[tuple[object, object]] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        captured.append((scope.get("server"), scope.get("client")))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def drive() -> None:
        driver = H2Driver(
            app,
            extra={
                "sockname": ("127.0.0.1", 8443),
                "peername": ("198.51.100.23", 49152),
            },
        )
        try:
            await driver.preface()
            await driver.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
        finally:
            driver.close()

    asyncio.run(drive())
    assert captured == [(("127.0.0.1", 8443), ("198.51.100.23", 49152))]


def test_http3_scope_reuses_connection_owned_addresses_for_client_isolation() -> None:
    root = Path(__file__).parents[2] / "src/wreath/_native"
    header = (root / "http3.h").read_text()
    connection = (root / "http3_connection.c").read_text()
    asgi = (root / "http3_asgi.c").read_text()

    assert "PyObject *client_address;" in header
    assert "PyObject *server_address;" in header
    assert "c->client_address = sockaddr_to_py(" in connection
    assert "c->server_address = c->client_address != NULL" in connection
    assert "Py_CLEAR(c->client_address);" in connection
    assert "Py_CLEAR(c->server_address);" in connection
    assert "c->server_address, c->client_address" in asgi


@requires_h3
@requires_curl_h3
@pytest.mark.network
@pytest.mark.asyncio
async def test_item_3_h3_request_timeout_cancels_and_answers() -> None:
    blocker = asyncio.Event()

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await blocker.wait()

    cert, key = make_self_signed_cert()
    server = await serve_server(
        app,
        ServerConfig(
            host="127.0.0.1",
            port=0,
            lifespan="off",
            protocols=("h3",),
            request_timeout=0.05,
        ),
        tls=TLSConfig(cert, key),
    )
    try:
        port = server.datagram_addresses[0][1]
        returncode, output = await curl_http3(port, "/", "-D", "-")
        assert returncode == 0
        assert output.startswith(b"HTTP/3 408")
    finally:
        await server.close()


@pytest.mark.parametrize(
    ("name", "value"),
    [(b"bad name", b"ok"), (b"x-ok", b"bad\r\nsmuggled: yes"), (b"x\x00bad", b"ok")],
)
def test_item_6_native_header_transform_refuses_invalid_fields(name: bytes, value: bytes) -> None:
    with pytest.raises(ValueError, match="invalid header"):
        _edge.request_headers([(name, value)], client=None, scheme=b"http", via=b"1.1 wreath")


def test_item_6_native_edge_parser_refuses_invalid_field_name() -> None:
    table = _edge.UpstreamTable([b"origin.test"], b"1.1 wreath", b"http")
    transport = _Transport()
    client = _edge.EdgeProtocol(table)
    client.connection_made(transport)
    client.data_received(b"GET / HTTP/1.1\r\nHost: example.test\r\nBad(Name: x\r\n\r\n")
    assert b"400 Bad Request" in b"".join(transport.writes)


def test_item_11_forwarded_quoted_values_are_escaped() -> None:
    headers: list[tuple[bytes, bytes]] = []
    append_forwarded(headers, client='client"\\node', host=b'example"\\host', scheme="https")
    forwarded = dict(headers)[b"forwarded"]
    assert forwarded == (b'for="client\\"\\\\node"; proto=https; host="example\\"\\\\host"')
    native = _edge.request_headers(
        [(b"host", b'example"\\host')],
        client='client"\\node',
        scheme=b"https",
        via=b"1.1 wreath",
    )
    assert dict(native)[b"forwarded"] == forwarded


@pytest.mark.parametrize("host", [b"good\r\nbad: yes", b"bad\x00host"])
def test_item_11_forwarded_host_refuses_invalid_field_values(host: bytes) -> None:
    with pytest.raises(ValueError, match="host must be a valid HTTP field value"):
        append_forwarded([], client=None, host=host, scheme="https")


def test_item_12_websocket_accept_refuses_unoffered_subprotocol() -> None:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    websocket = WebSocket(
        {"type": "websocket", "path": "/", "subprotocols": ["chat"]},
        receive,
        send,
    )
    with pytest.raises(ValueError, match="was not offered"):
        asyncio.run(websocket.accept("admin"))
    assert sent == []


@pytest.mark.parametrize("content_type", [None, b"application/json", b"text/plain"])
def test_item_17_form_refuses_non_form_media_types_before_reading(
    content_type: bytes | None,
) -> None:
    async def receive() -> dict[str, Any]:
        raise AssertionError("non-form content must be refused before body I/O")

    headers = [] if content_type is None else [(b"content-type", content_type)]
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "scheme": "https",
            "query_string": b"",
            "headers": headers,
        },
        receive,
    )
    with pytest.raises(ValueError, match=r"form\(\) requires"):
        asyncio.run(request.form())


def test_item_17_form_media_type_matching_is_exact_and_case_insensitive() -> None:
    messages = iter([{"type": "http.request", "body": b"a=1", "more_body": False}])

    async def receive() -> dict[str, Any]:
        return next(messages)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "scheme": "https",
            "query_string": b"",
            "headers": [(b"content-type", b"Application/X-Www-Form-Urlencoded; charset=UTF-8")],
        },
        receive,
    )
    assert asyncio.run(request.form()).fields == {"a": "1"}


def test_item_17_multipart_media_type_uses_the_multipart_parser() -> None:
    body = (
        b"--secure-boundary\r\n"
        b'Content-Disposition: form-data; name="field"\r\n\r\n'
        b"value\r\n--secure-boundary--\r\n"
    )
    messages = iter([{"type": "http.request", "body": body, "more_body": False}])

    async def receive() -> dict[str, Any]:
        return next(messages)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "scheme": "https",
            "query_string": b"",
            "headers": [(b"content-type", b"Multipart/Form-Data; boundary=secure-boundary")],
        },
        receive,
    )
    assert asyncio.run(request.form()).fields == {"field": "value"}


def test_http3_cryptographic_state_is_endpoint_owned_and_connection_admission_is_bounded() -> None:
    root = Path(__file__).parents[2]
    source = (root / "src/wreath/_native/http3_connection.c").read_text()
    header = (root / "src/wreath/_native/http3.h").read_text()

    assert "static const uint8_t wreath_h3_secret" not in source
    assert "rand()" not in source
    random = source.split("h3_random", 1)[1].split("py_addr_to_sockaddr", 1)[0]
    assert "RAND_priv_bytes" in random
    assert "memset(dest" not in random
    assert "retry_secret" in header
    assert "connection_count" in header
    assert "WREATH_H3_MAX_CONNECTIONS" in source
    assert "ep->connection_count >= WREATH_H3_MAX_CONNECTIONS" in source


def test_http3_datagrams_do_not_scan_every_connection_to_rearm_the_timer() -> None:
    source = (Path(__file__).parents[2] / "src/wreath/_native/http3_connection.c").read_text()
    datagram = source.split("endpoint_datagram_received", 1)[1].split(
        "endpoint_connection_made", 1
    )[0]

    assert "PyDict_Values(ep->conns)" not in datagram
    assert "rearm_connection_timer(ep, c)" in datagram


@pytest.mark.parametrize(
    "response",
    (
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
    ),
)
def test_native_edge_refuses_ambiguous_upstream_response_framing(
    response: bytes,
) -> None:
    client, upstream = _edge_exchange(response)

    assert b"502 Bad Gateway" in b"".join(client.writes)
    assert upstream.closed


def test_native_edge_unions_every_upstream_connection_option() -> None:
    client, _ = _edge_exchange(
        b"HTTP/1.1 200 OK\r\n"
        b"Connection: x-internal-auth\r\n"
        b"Connection: close\r\n"
        b"X-Internal-Auth: trusted\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    response = b"".join(client.writes).lower()

    assert b"x-internal-auth" not in response


def test_native_edge_does_not_release_an_upstream_after_an_informational_response() -> None:
    table = _edge.UpstreamTable([b"origin.test"], b"1.1 wreath", b"http")
    upstream_transport = _Transport()
    upstream = _edge.UpstreamConnection(table, 0)
    upstream.connection_made(upstream_transport)
    first_transport = _Transport()
    first = _edge.EdgeProtocol(table)
    first.connection_made(first_transport)
    second = _edge.EdgeProtocol(table)
    second.connection_made(_Transport())
    request = b"GET / HTTP/1.1\r\nHost: example.test\r\n\r\n"
    first.data_received(request)
    second.data_received(request)

    upstream.data_received(b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\n")

    assert len(upstream_transport.writes) == 1
    assert table.stats()["waiting"] == 1
    upstream.data_received(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    assert b"103 Early Hints" in b"".join(first_transport.writes)
    assert b"200 OK" in b"".join(first_transport.writes)
    assert len(upstream_transport.writes) == 2


def test_native_edge_bounds_informational_response_floods() -> None:
    table = _edge.UpstreamTable([b"origin.test"], b"1.1 wreath", b"http")
    upstream_transport = _Transport()
    upstream = _edge.UpstreamConnection(table, 0)
    upstream.connection_made(upstream_transport)
    client_transport = _Transport()
    client = _edge.EdgeProtocol(table)
    client.connection_made(client_transport)
    client.data_received(b"GET / HTTP/1.1\r\nHost: example.test\r\n\r\n")

    early_hint = b"HTTP/1.1 103 Early Hints\r\nLink: </x>; rel=preload\r\n\r\n"
    upstream.data_received(early_hint * 200)

    assert b"103 Early Hints" in b"".join(client_transport.writes)
    assert upstream_transport.closed


def test_native_edge_applies_downstream_backpressure_to_the_origin() -> None:
    table = _edge.UpstreamTable([b"origin.test"], b"1.1 wreath", b"http")
    upstream_transport = _Transport()
    upstream = _edge.UpstreamConnection(table, 0)
    upstream.connection_made(upstream_transport)
    client = _edge.EdgeProtocol(table)
    client.connection_made(_Transport())
    client.data_received(b"GET / HTTP/1.1\r\nHost: example.test\r\n\r\n")

    client.pause_writing()
    assert upstream_transport.read_paused
    client.resume_writing()
    assert not upstream_transport.read_paused


def test_native_edge_bounds_pipelined_bytes_while_a_response_is_in_flight() -> None:
    table = _edge.UpstreamTable([b"origin.test"], b"1.1 wreath", b"http")
    upstream = _edge.UpstreamConnection(table, 0)
    upstream.connection_made(_Transport())
    client_transport = _Transport()
    client = _edge.EdgeProtocol(table)
    client.connection_made(client_transport)
    client.data_received(b"GET / HTTP/1.1\r\nHost: example.test\r\n\r\n")

    client.data_received(b"G" * 32769)

    assert client_transport.closed


@pytest.mark.parametrize(
    "raw_request",
    (
        pytest.param(
            b"GE\tT / HTTP/1.1\r\nHost: example.test\r\n\r\n",
            id="method-control",
        ),
        pytest.param(
            b"GET http://attacker.test/ HTTP/1.1\r\nHost: example.test\r\n\r\n",
            id="absolute-form",
        ),
        pytest.param(
            b"GET /safe\x01unsafe HTTP/1.1\r\nHost: example.test\r\n\r\n",
            id="target-control",
        ),
    ),
)
def test_native_edge_refuses_unsafe_request_methods_and_targets(raw_request: bytes) -> None:
    table = _edge.UpstreamTable([b"origin.test"], b"1.1 wreath", b"http")
    upstream_transport = _Transport()
    upstream = _edge.UpstreamConnection(table, 0)
    upstream.connection_made(upstream_transport)
    client_transport = _Transport()
    client = _edge.EdgeProtocol(table)
    client.connection_made(client_transport)

    client.data_received(raw_request)

    assert b"400 Bad Request" in b"".join(client_transport.writes)
    assert not upstream_transport.writes


def test_native_edge_unions_every_request_connection_option() -> None:
    table = _edge.UpstreamTable([b"origin.test"], b"1.1 wreath", b"http")
    upstream_transport = _Transport()
    upstream = _edge.UpstreamConnection(table, 0)
    upstream.connection_made(upstream_transport)
    client = _edge.EdgeProtocol(table)
    client.connection_made(_Transport())

    client.data_received(
        b"GET / HTTP/1.1\r\n"
        b"Host: example.test\r\n"
        b"Connection: x-internal-auth\r\n"
        b"Connection: close\r\n"
        b"X-Internal-Auth: trusted\r\n\r\n"
    )

    forwarded = b"".join(upstream_transport.writes).lower()
    assert b"x-internal-auth" not in forwarded


@pytest.mark.parametrize(
    "body",
    (
        pytest.param(b"f" * 65537, id="chunk-size-line"),
        pytest.param(b"1\r\na\r\n" * 4097, id="chunk-count"),
        pytest.param(
            b"0\r\nX-Trailer: " + b"x" * 65537 + b"\r\n\r\n",
            id="trailer-bytes",
        ),
    ),
)
def test_native_edge_bounds_chunked_upstream_metadata(body: bytes) -> None:
    client, upstream = _edge_exchange(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + body
    )

    assert client.closed
    assert upstream.closed


@pytest.mark.parametrize(
    "body",
    (
        pytest.param(b"f" * 65537, id="chunk-size-line"),
        pytest.param(b"1\r\na\r\n" * 4097, id="chunk-count"),
        pytest.param(
            b"0\r\nX-Trailer: " + b"x" * 65537,
            id="trailer-bytes",
        ),
    ),
)
def test_native_edge_bounds_chunk_metadata_before_request_dispatch(body: bytes) -> None:
    table = _edge.UpstreamTable([b"origin.test"], b"1.1 wreath", b"http", max_body=1024 * 1024)
    client_transport = _Transport()
    client = _edge.EdgeProtocol(table)
    client.connection_made(client_transport)
    upstream_transport = _Transport()
    upstream = _edge.UpstreamConnection(table, 0)
    upstream.connection_made(upstream_transport)

    client.data_received(
        b"POST / HTTP/1.1\r\nHost: example.test\r\nTransfer-Encoding: chunked\r\n\r\n" + body
    )

    assert client_transport.closed
    assert not upstream_transport.writes


@pytest.mark.parametrize(
    "chunk_line",
    (
        pytest.param(b"1suffix\r\n", id="missing-extension-delimiter"),
        pytest.param(b"1;bad extension\r\n", id="invalid-extension-name"),
    ),
)
def test_native_edge_refuses_invalid_request_chunk_suffixes(chunk_line: bytes) -> None:
    table = _edge.UpstreamTable([b"origin.test"], b"1.1 wreath", b"http")
    client_transport = _Transport()
    client = _edge.EdgeProtocol(table)
    client.connection_made(client_transport)
    upstream_transport = _Transport()
    upstream = _edge.UpstreamConnection(table, 0)
    upstream.connection_made(upstream_transport)

    client.data_received(
        b"POST / HTTP/1.1\r\nHost: example.test\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n" + chunk_line + b"a\r\n0\r\n\r\n"
    )

    assert b"400 Bad Request" in b"".join(client_transport.writes)
    assert not upstream_transport.writes


@pytest.mark.parametrize(
    "chunk_line",
    (
        pytest.param(b"1suffix\r\n", id="missing-extension-delimiter"),
        pytest.param(b"1;bad extension\r\n", id="invalid-extension-name"),
    ),
)
def test_native_edge_refuses_invalid_response_chunk_suffixes(chunk_line: bytes) -> None:
    client, upstream = _edge_exchange(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunk_line + b"a\r\n0\r\n\r\n"
    )

    assert client.closed
    assert upstream.closed


async def _serve_protocol_response(response: bytes) -> tuple[asyncio.AbstractServer, int]:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(response)
        await writer.drain()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _protocol_client(port: int) -> HTTPClient:
    return HTTPClient(
        "protocol-contract",
        base_url=f"http://127.0.0.1:{port}",
        destination=DestinationPolicy(allow_loopback=True),
        limits=ClientLimits(max_connections=1, max_keepalive_connections=1),
        timeout=ClientTimeout(total=0.05),
    )


@pytest.mark.asyncio
async def test_streamed_response_refuses_buffered_bytes_after_its_framed_body() -> None:
    server, port = await _serve_protocol_response(
        b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\naHTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
    )
    client = _protocol_client(port)
    await client.start()
    try:
        with pytest.raises(ProtocolError, match="unsolicited bytes"):
            async with client.stream("GET", "/") as response:
                assert b"".join([chunk async for chunk in response.iter_bytes()]) == b"a"
        assert client.snapshot().active == 0
        assert client.snapshot().idle == 0
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stream_entry_protocol_failure_releases_its_active_pool_slot() -> None:
    server, port = await _serve_protocol_response(b"not an HTTP response\r\n\r\n")
    client = _protocol_client(port)
    await client.start()
    try:
        with pytest.raises(ProtocolError):
            async with client.stream("GET", "/"):
                pass
        assert client.snapshot().active == 0
        assert client.snapshot().idle == 0
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


def test_http3_uses_shared_path_normalization_and_excludes_the_query_from_raw_path() -> None:
    source = (Path(__file__).parents[2] / "src/wreath/_native/http3_asgi.c").read_text()
    request = source.split("start_request", 1)[1].split("end_headers_cb", 1)[0]

    assert "decode_request_path(" in request
    assert "pp, path_len" in request
    assert "PyBytes_FromStringAndSize(pp, path_len)" in request
    assert 'PyUnicode_DecodeUTF8(pp, q >= 0 ? q : pl, "surrogateescape")' not in request


def test_http3_counts_empty_data_frames_against_the_request_chunk_budget() -> None:
    source = (Path(__file__).parents[2] / "src/wreath/_native/http3_asgi.c").read_text()
    callback = source.split("recv_data_cb", 1)[1].split("end_stream_cb", 1)[0]

    assert "if (datalen > 0)" not in callback
    assert "s->body_frames++" in callback


def test_http3_trailer_headers_cannot_reactivate_an_existing_request() -> None:
    source = (Path(__file__).parents[2] / "src/wreath/_native/http3_asgi.c").read_text()
    callback = source.split("end_headers_cb", 1)[1].split("/* Reject one request stream", 1)[0]

    guard = "if (s->scope != NULL)"
    assert guard in callback
    assert callback.index(guard) < callback.index("start_request(s)")
    assert "NGHTTP3_H3_MESSAGE_ERROR" in callback


def test_http3_validates_request_target_form_before_scope_construction() -> None:
    source = (Path(__file__).parents[2] / "src/wreath/_native/http3_asgi.c").read_text()
    request = source.split("start_request", 1)[1].split("end_headers_cb", 1)[0]

    assert "path_data[0] != '/'" in request
    assert "path_data[0] == '*'" in request
    assert 'memcmp(PyBytes_AS_STRING(method), "OPTIONS", 7)' in request
    assert 'memcmp(PyBytes_AS_STRING(method), "CONNECT", 7)' in request
    assert "memchr(path_data, '#', (size_t)path_size)" in request
    assert request.index("path_data[0] != '/'") < request.index("wreath_request_context_new")


def test_http3_enforces_connection_header_and_content_length_framing_rules() -> None:
    root = Path(__file__).parents[2] / "src/wreath/_native"
    source = (root / "http3_asgi.c").read_text()
    header = (root / "http3.h").read_text()
    request = source.split("start_request", 1)[1].split("end_headers_cb", 1)[0]
    data = source.split("recv_data_cb", 1)[1].split("end_stream_cb", 1)[0]
    ended = source.split("end_stream_cb", 1)[1].split("h3_stream_close_cb", 1)[0]

    for forbidden in (
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "upgrade",
    ):
        assert f'"{forbidden}"' in request
    assert 'memcmp(np, "te", 2)' in request
    assert 'memcmp(vp, "trailers", 8)' in request
    assert "parse_h3_content_length" in request
    assert "content_length" in header
    assert "s->content_length" in request
    assert "total > s->content_length" in data
    assert "s->body_received != s->content_length" in ended
    assert "s->request_ended && content_length != -1 && content_length != 0" in request


def test_http3_enforces_pseudo_header_order_and_uniqueness_before_activation() -> None:
    source = (Path(__file__).parents[2] / "src/wreath/_native/http3_asgi.c").read_text()
    request = source.split("start_request", 1)[1].split("end_headers_cb", 1)[0]

    assert "seen_regular" in request
    for field in ("method", "path", "scheme", "authority"):
        assert f"if ({field} != NULL) goto message_err" in request
    assert "if (seen_regular) goto message_err" in request
    assert "else {\n                goto message_err;\n            }" in request


def test_http3_rejects_invalid_regular_header_names_before_scope_construction() -> None:
    source = (Path(__file__).parents[2] / "src/wreath/_native/http3_asgi.c").read_text()
    request = source.split("start_request", 1)[1].split("end_headers_cb", 1)[0]

    assert "nghttp3_check_header_name" in request
    assert request.index("nghttp3_check_header_name") < request.index("wreath_request_context_new")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trailer",
    (
        (b":status", b"204"),
        (b"connection", b"close"),
        (b"content-length", b"0"),
        (b"transfer-encoding", b"chunked"),
        (b"x-ok", b"bad\r\ninjected: true"),
    ),
)
async def test_http2_response_trailers_use_response_header_validation(
    trailer: tuple[bytes, bytes],
) -> None:
    from http2 import support
    from http2.conftest import H2Driver

    async def app(_scope: dict[str, Any], _receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
                "trailers": True,
            }
        )
        await send({"type": "http.response.body", "body": b""})
        await send(
            {
                "type": "http.response.trailers",
                "headers": [trailer],
            }
        )

    driver = H2Driver(app, ServerConfig(protocols=("h2",)))
    await driver.preface()
    await driver.feed_and_settle(
        support.build_headers_frame(1, support.request_headers(), end_stream=True)
    )

    assert len([frame for frame in driver.frames() if frame.type == support.HEADERS]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "accept",
    (
        pytest.param(
            {
                "type": "websocket.accept",
                "subprotocol": "chat\r\nx-internal-auth: trusted",
            },
            id="subprotocol",
        ),
        *(
            pytest.param(
                {
                    "type": "websocket.accept",
                    "headers": [(name, b"attacker")],
                },
                id=name.decode(),
            )
            for name in (
                b"upgrade",
                b"connection",
                b"sec-websocket-accept",
                b"sec-websocket-protocol",
                b"sec-websocket-extensions",
            )
        ),
    ),
)
async def test_native_websocket_accept_refuses_response_splitting(
    accept: dict[str, Any],
) -> None:
    from test_server_websocket import _NativeHttpProtocol, start, upgrade

    assert _NativeHttpProtocol is not None

    async def app(_scope: dict[str, Any], receive: Any, send: Any) -> None:
        await receive()
        await send(accept)

    _, transport = await start(_NativeHttpProtocol, app, [upgrade()])
    wire = bytes(transport.buffer).lower()

    assert b"x-internal-auth" not in wire
    assert b"attacker" not in wire
    assert wire.count(b"sec-websocket-accept:") <= 1


@pytest.mark.asyncio
async def test_native_websocket_bounds_control_frame_floods_after_answering_ping() -> None:
    from _server_ingest import feed
    from test_server_websocket import (
        MASK,
        _NativeHttpProtocol,
        _settle,
        frames,
        split_head,
        start,
        upgrade,
    )

    from wreath._websocket import build_frame

    assert _NativeHttpProtocol is not None

    async def app(_scope: dict[str, Any], receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "websocket.accept"})
        while (await receive())["type"] != "websocket.disconnect":
            pass

    ping = build_frame(9, b"ping", True, MASK)
    protocol, transport = await start(_NativeHttpProtocol, app, [upgrade(), ping])
    _, initial_wire = split_head(bytes(transport.buffer))
    assert frames(initial_wire) == [(True, 10, b"ping")]

    response_offset = len(transport.buffer)
    feed(protocol, ping * 200)
    await _settle()

    assert transport.closed
    flood_frames = frames(bytes(transport.buffer)[response_offset:])
    assert any(opcode == 8 for _, opcode, _ in flood_frames)


@pytest.mark.asyncio
async def test_native_websocket_pauses_input_while_upgrade_acceptance_is_pending() -> None:
    from _server_ingest import feed
    from test_server_websocket import _NativeHttpProtocol, _settle, start, upgrade

    assert _NativeHttpProtocol is not None
    decide = asyncio.Event()

    async def app(_scope: dict[str, Any], receive: Any, send: Any) -> None:
        await receive()
        await decide.wait()
        await send({"type": "websocket.accept"})
        await receive()

    protocol, transport = await start(_NativeHttpProtocol, app, [upgrade()])
    try:
        feed(protocol, b"x" * (32 * 1024))
        await _settle()
        assert transport.reading_paused
    finally:
        decide.set()
        await _settle()
        protocol.connection_lost(None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_request",
    (
        pytest.param(
            b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Key: YWJjZGVmZ2hpamtsbW5vcA==\r\n"
            b"Sec-WebSocket-Key: YWJjZGVmZ2hpamtsbW5vcA==\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n",
            id="duplicate-key",
        ),
        pytest.param(
            b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Key: YWJjZGVmZ2hpamtsbW5vcA==\r\n"
            b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Version: 13\r\n\r\n",
            id="duplicate-version",
        ),
        pytest.param(
            b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Key: !!!!!!!!!!!!!!!!!!!!!!==\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n",
            id="malformed-key",
        ),
        pytest.param(
            b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Key: YWJjZA==\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n",
            id="wrong-key-length",
        ),
    ),
)
async def test_native_websocket_refuses_ambiguous_or_invalid_handshake_keys(
    raw_request: bytes,
) -> None:
    from test_server_websocket import _NativeHttpProtocol, start

    assert _NativeHttpProtocol is not None
    activated = False

    async def app(_scope: dict[str, Any], _receive: Any, _send: Any) -> None:
        nonlocal activated
        activated = True

    _, transport = await start(_NativeHttpProtocol, app, [raw_request])

    assert not activated
    assert bytes(transport.buffer).startswith(b"HTTP/1.1 400")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "target"),
    (
        (b"GET", b"http://victim.test/private"),
        (b"GET", b"victim.test:443"),
        (b"GET", b"*"),
        (b"GET", b"/private#public"),
        (b"CONNECT", b"victim.test:443"),
    ),
)
async def test_http1_refuses_unsupported_request_target_forms_before_app_activation(
    method: bytes,
    target: bytes,
) -> None:
    from test_server_protocol import _NativeHttpProtocol, drive

    assert _NativeHttpProtocol is not None
    activated: list[bytes] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        activated.append(scope["raw_path"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    transport = await drive(
        _NativeHttpProtocol,
        app,
        [method + b" " + target + b" HTTP/1.1\r\nHost: public.test\r\n\r\n"],
    )

    assert not activated
    assert bytes(transport.buffer).startswith(b"HTTP/1.1 400")


@pytest.mark.asyncio
async def test_http1_preserves_options_asterisk_form() -> None:
    from test_server_protocol import _NativeHttpProtocol, drive

    assert _NativeHttpProtocol is not None
    activated: list[tuple[str, bytes]] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        activated.append((scope["method"], scope["raw_path"]))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    await drive(
        _NativeHttpProtocol,
        app,
        [b"OPTIONS * HTTP/1.1\r\nHost: public.test\r\n\r\n"],
    )

    assert activated == [("OPTIONS", b"*")]
