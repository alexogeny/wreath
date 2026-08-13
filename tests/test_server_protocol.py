"""Fake-transport protocol tests for the Wreath HTTP server.

Every behavioral test runs against the HTTP protocol implementation, skipped
when the extension is not built. We only ever compare bytes on the wire, ASGI
scopes/messages, and closure behavior -- never internal object layout.
"""

from __future__ import annotations

import asyncio
import gc
import gzip
import importlib
import inspect
import tracemalloc
from email.utils import parsedate_to_datetime
from typing import Any

import pytest
from _server_ingest import feed

import wreath.app as app_module
from wreath import Response, Wreath
from wreath.auth import BearerTokenBackend, Identity, authenticated
from wreath.cache_control import CacheControl
from wreath.policy import CachePolicy, CompressionPolicy, HttpPolicy
from wreath.response import PreparedResponse
from wreath.server import ServerConfig

try:
    _native_server = importlib.import_module("wreath._native._server")
    _NativeHttpProtocol = _native_server.HttpProtocol
except ImportError:
    _NativeHttpProtocol = None

IMPLS = [
    pytest.param(
        _NativeHttpProtocol,
        id="http1",
        marks=pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built"),
    )
]

impl = pytest.mark.parametrize("protocol_cls", IMPLS)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("response_high_water", 0),
        ("response_high_water_segments", 0),
        ("response_low_water", -1),
        ("response_low_water_segments", -1),
    ],
)
def test_response_watermarks_reject_invalid_bounds(field: str, value: int) -> None:
    with pytest.raises(ValueError, match=field):
        ServerConfig(**{field: value})


def test_response_low_watermarks_must_stay_below_high_watermarks() -> None:
    with pytest.raises(ValueError, match="response_low_water"):
        ServerConfig(response_high_water=1024, response_low_water=1025)
    with pytest.raises(ValueError, match="response_low_water_segments"):
        ServerConfig(response_high_water_segments=8, response_low_water_segments=9)


class FakeTransport(asyncio.Transport):
    def __init__(self, extra: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.buffer = bytearray()
        self.writes: list[bytes] = []
        self.write_objects: list[Any] = []
        self.writeline_batches: list[tuple[Any, ...]] = []
        self.extra_info_calls: list[str] = []
        self.closed = False
        self.closed_event = asyncio.Event()
        self.aborted = False
        self.reading_paused = False
        self._extra = extra or {
            "sockname": ("127.0.0.1", 8000),
            "peername": ("127.0.0.1", 54321),
        }

    def write(self, data: Any) -> None:
        if not self.closed:
            self.write_objects.append(data)
            self.writes.append(bytes(data))
            self.buffer += data

    def writelines(self, chunks: Any) -> None:
        if not self.closed:
            batch = tuple(chunks)
            self.writeline_batches.append(batch)
            self.write_objects.extend(batch)
            self.writes.extend(bytes(chunk) for chunk in batch)
            for chunk in batch:
                self.buffer += chunk

    def close(self) -> None:
        self.closed = True
        self.closed_event.set()

    def abort(self) -> None:
        self.aborted = True
        self.closed = True
        self.closed_event.set()

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        self.reading_paused = True

    def resume_reading(self) -> None:
        self.reading_paused = False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        self.extra_info_calls.append(name)
        return self._extra.get(name, default)


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def drive(
    protocol_cls: type,
    app: Any,
    chunks: list[bytes],
    config: ServerConfig | None = None,
    *,
    extra: dict[str, Any] | None = None,
) -> FakeTransport:
    loop = asyncio.get_running_loop()
    registry: set[Any] = set()
    transport = FakeTransport(extra)
    protocol = protocol_cls(app, config or ServerConfig(), loop, registry)
    protocol.connection_made(transport)
    for chunk in chunks:
        feed(protocol, chunk)
        await _settle()
    await _settle()
    return transport


# --- simple apps ------------------------------------------------------------


async def echo_ok(scope: dict, receive: Any, send: Any) -> None:
    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def make_scope_capture() -> tuple[Any, list]:
    captured: list = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        captured.append(scope)
        # Drain body.
        while True:
            m = await receive()
            if m["type"] == "http.disconnect" or not m.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app, captured


def parse_responses(data: bytes | bytearray) -> list[bytes]:
    # Split on the double CRLF is unreliable with bodies; callers assert on the
    # raw bytes. This helper only counts status lines.
    return [bytes(line) for line in bytes(data).split(b"\r\n") if line.startswith(b"HTTP/")]


GET = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_request_shells_reuse_bounded_storage() -> None:
    """A second same-shaped request reuses both native ingress shells."""
    await drive(_NativeHttpProtocol, echo_ok, [GET])
    gc.collect()
    after_first = _native_server._request_storage_counts()

    await drive(_NativeHttpProtocol, echo_ok, [GET])
    gc.collect()
    after_second = _native_server._request_storage_counts()

    assert after_second == after_first, (
        "the second request allocated a new RequestContext or HeaderBlock "
        f"shell: {after_first!r} -> {after_second!r}"
    )


def test_native_application_entry_returns_the_compiled_dispatcher_directly() -> None:
    app = Wreath()

    assert not inspect.iscoroutinefunction(app._wreath_http)


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_bearer_policy_activates_verifier_from_header_spans() -> None:
    seen: list[str] = []

    async def verify(token: str) -> Identity | None:
        seen.append(token)
        return Identity("native-user") if token == "credential" else None

    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/private")
    @authenticated()
    async def private(request: Any) -> str:
        return request.identity.id

    transport = await drive(
        _NativeHttpProtocol,
        app,
        [b"GET /private HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer credential\r\n\r\n"],
    )

    assert app._dispatch_http.__name__ == "_handle_http_plain_auth"
    assert seen == ["credential"]
    assert b"200 OK" in transport.buffer
    assert transport.buffer.endswith(b"native-user")


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_first_class_cache_and_compression_transform_before_egress() -> None:
    app = Wreath(
        http_policy=HttpPolicy(
            cache_control=CachePolicy(CacheControl(public=True, max_age=60)),
            compression=CompressionPolicy(minimum_size=0),
        )
    )

    @app.get("/")
    async def payload(request: Any) -> str:
        return "native-policy" * 100

    request = GET[:-2] + b"Accept-Encoding: gzip\r\n\r\n"
    transport = await drive(_NativeHttpProtocol, app, [request])
    assert app._dispatch_http.__name__ == "_handle_http_plain"
    head, body = bytes(transport.buffer).split(b"\r\n\r\n", 1)
    assert b"cache-control: public, max-age=60\r\n" in head
    assert b"content-encoding: gzip\r\n" in head
    assert b"vary: accept-encoding\r\n" in head
    assert gzip.decompress(body) == b"native-policy" * 100


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_cache_policy_transforms_a_prepared_response_one_shot() -> None:
    app = Wreath(
        http_policy=HttpPolicy(
            cache_control=CachePolicy(CacheControl(public=True, max_age=60))
        )
    )
    response = PreparedResponse.text("ready")

    @app.get("/")
    async def payload(request: Any) -> PreparedResponse:
        return response

    transport = await drive(_NativeHttpProtocol, app, [GET])
    head, body = bytes(transport.buffer).split(b"\r\n\r\n", 1)

    assert b"cache-control: public, max-age=60\r\n" in head
    assert body == b"ready"


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_reuses_prepared_header_wire_on_keep_alive() -> None:
    app = Wreath()
    response = PreparedResponse.text("ready", headers=[(b"X-Prepared", b"yes")])

    @app.get("/")
    async def payload(request: Any) -> PreparedResponse:
        return response

    transport = await drive(_NativeHttpProtocol, app, [GET + GET])
    wire = bytes(transport.buffer).lower()

    assert wire.count(b"http/1.1 200 ok\r\n") == 2
    assert wire.count(b"x-prepared: yes\r\n") == 2
    assert wire.count(b"\r\n\r\nready") == 2


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_frozen_route_never_constructs_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Wreath()
    app.frozen("/", PreparedResponse.text("ready"))
    app._compile_routes()

    def unexpected_request(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("frozen native route constructed Request")

    def unexpected_general_finisher(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("frozen native route entered the general response finisher")

    # The routine test runner may enable process-wide trace propagation around
    # selected tests; this test isolates the ordinary unrecorded production
    # path whose absence of Request construction it pins.
    monkeypatch.setattr(app_module._telemetry, "PROPAGATING", False)
    monkeypatch.setattr(app_module, "Request", unexpected_request)
    monkeypatch.setattr(Wreath, "_finish_http_plain", unexpected_general_finisher)
    monkeypatch.setattr(
        app_module,
        "_arm_cancel_on_disconnect",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("undeclared frozen route armed disconnect override")
        ),
    )
    transport = await drive(_NativeHttpProtocol, app, [GET + GET])
    wire = bytes(transport.buffer).lower()

    assert app._dispatch_http == app._handle_http_frozen
    assert wire.count(b"http/1.1 200 ok\r\n") == 2
    assert wire.count(b"\r\n\r\nready") == 2


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_frozen_route_arms_disconnect_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Wreath()
    app.frozen(
        "/",
        PreparedResponse.text("ready"),
        cancel_on_disconnect=False,
    )
    app._compile_routes()
    armed: list[bool] = []

    monkeypatch.setattr(app_module._telemetry, "PROPAGATING", False)
    monkeypatch.setattr(
        app_module,
        "_arm_cancel_on_disconnect",
        lambda send, enabled: armed.append(enabled),
    )
    transport = await drive(_NativeHttpProtocol, app, [GET])

    assert bytes(transport.buffer).endswith(b"ready")
    assert armed == [False]


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_frozen_route_without_override_stays_unarmed_in_mixed_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Wreath()
    app.frozen(
        "/declared",
        PreparedResponse.text("declared"),
        cancel_on_disconnect=False,
    )
    app.frozen("/plain", PreparedResponse.text("plain"))
    app._compile_routes()
    armed: list[bool] = []

    monkeypatch.setattr(app_module._telemetry, "PROPAGATING", False)
    monkeypatch.setattr(
        app_module,
        "_arm_cancel_on_disconnect",
        lambda send, enabled: armed.append(enabled),
    )
    transport = await drive(
        _NativeHttpProtocol,
        app,
        [b"GET /plain HTTP/1.1\r\nHost: x\r\n\r\n"],
    )

    assert bytes(transport.buffer).endswith(b"plain")
    assert armed == []


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_bearer_auth_reads_one_header_without_materializing_the_block() -> None:
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: Identity("native") if token == "valid-token" else None)
    )

    @app.get("/auth")
    @authenticated()
    async def protected(request: Any) -> str:
        return request.identity.id

    fields = (
        b"host: localhost\r\nuser-agent: wreath-test\r\naccept: */*\r\n"
        b"accept-encoding: gzip\r\nx-request-id: test\r\n"
        b"x-forwarded-proto: https\r\ncache-control: no-cache\r\n"
    )
    allowed = await drive(
        _NativeHttpProtocol,
        app,
        [b"GET /auth HTTP/1.1\r\n" + fields + b"authorization: Bearer valid-token\r\n\r\n"],
    )
    duplicate = await drive(
        _NativeHttpProtocol,
        app,
        [
            b"GET /auth HTTP/1.1\r\n" + fields + b"authorization: Bearer valid-token\r\n"
            b"authorization: Bearer other\r\n\r\n"
        ],
    )

    assert allowed.buffer.startswith(b"HTTP/1.1 200")
    assert allowed.buffer.endswith(b"native")
    assert duplicate.buffer.startswith(b"HTTP/1.1 401")


# --- head fragmentation -----------------------------------------------------


@impl
@pytest.mark.asyncio
async def test_head_split_every_boundary(protocol_cls: type) -> None:
    for split in range(1, len(GET)):
        transport = await drive(protocol_cls, echo_ok, [GET[:split], GET[split:]])
        assert transport.buffer.startswith(b"HTTP/1.1 200"), split


@impl
@pytest.mark.asyncio
async def test_two_requests_in_one_data_received(protocol_cls: type) -> None:
    transport = await drive(protocol_cls, echo_ok, [GET + GET])
    assert parse_responses(transport.buffer) == [b"HTTP/1.1 200 OK", b"HTTP/1.1 200 OK"]


@impl
@pytest.mark.asyncio
async def test_pipelined_response_ordering(protocol_cls: type) -> None:
    r1 = b"GET /a HTTP/1.1\r\nHost: x\r\n\r\n"
    r2 = b"GET /b HTTP/1.1\r\nHost: x\r\n\r\n"

    async def app(scope: dict, receive: Any, send: Any) -> None:
        path = scope["path"].encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": path})

    transport = await drive(protocol_cls, app, [r1 + r2])
    # /a body must precede /b body.
    assert transport.buffer.index(b"/a") < transport.buffer.index(b"/b")


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_wreath_native_request_context_keeps_scope_lazy() -> None:
    app = Wreath()
    observed: list[Any] = []

    @app.get("/lazy")
    async def lazy(request: Any) -> Response:
        assert request._scope is None
        assert request.method == "GET"
        assert request.path == "/lazy"
        assert request.header("host") == "x"
        assert request._scope is None
        observed.append(request._context)
        return Response(b"ok")

    transport = await drive(
        _NativeHttpProtocol,
        app,
        [b"GET /lazy HTTP/1.1\r\nHost: x\r\n\r\n"],
    )
    assert observed
    assert type(observed[0]).__name__ == "_RequestContext"
    assert b"200 OK" in transport.buffer


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_wreath_native_request_context_materializes_compatible_scope() -> None:
    app = Wreath()
    observed: list[dict[str, Any]] = []

    @app.get("/scope")
    async def scope_handler(request: Any) -> Response:
        observed.append(request.scope)
        return Response(b"ok")

    await drive(
        _NativeHttpProtocol,
        app,
        [b"GET /scope?q=1 HTTP/1.1\r\nHost: x\r\n\r\n"],
    )
    assert observed[0]["type"] == "http"
    assert observed[0]["path"] == "/scope"
    assert observed[0]["query_string"] == b"q=1"
    assert "wreath.response" in observed[0]["extensions"]


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_wreath_native_request_headers_stay_native_until_public_read() -> None:
    app = Wreath()

    @app.get("/headers")
    async def header_handler(request: Any) -> Response:
        assert request._scope is None
        index = request._index_headers()
        assert len(index) == 3
        assert index[b"x-value"] == b"first"
        assert index.get(b"missing") is None
        assert request.cookies == {"a": "1", "b": "2"}
        request._set_header(b"host", b"updated.example")
        assert request.header("host") == "updated.example"
        assert request._scope is None
        assert request.headers[0] == (b"host", b"updated.example")
        return Response(b"ok")

    transport = await drive(
        _NativeHttpProtocol,
        app,
        [
            b"GET /headers HTTP/1.1\r\n"
            b"Host: original.example\r\n"
            b"X-Value: first\r\n"
            b"X-Value: second\r\n"
            b"Cookie: a=1; b=2\r\n\r\n"
        ],
    )
    assert b"200 OK" in transport.buffer


# --- fixed body -------------------------------------------------------------

FIXED = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello"


@impl
@pytest.mark.asyncio
async def test_fixed_body_split_every_boundary(protocol_cls: type) -> None:
    for split in range(1, len(FIXED)):
        transport = await drive(protocol_cls, echo_ok, [FIXED[:split], FIXED[split:]])
        assert transport.buffer.endswith(b"hello"), split


# --- chunked body -----------------------------------------------------------

CHUNKED = (
    b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
    b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
)


@impl
@pytest.mark.asyncio
async def test_chunked_body_split_every_boundary(protocol_cls: type) -> None:
    for split in range(1, len(CHUNKED)):
        transport = await drive(protocol_cls, echo_ok, [CHUNKED[:split], CHUNKED[split:]])
        assert transport.buffer.endswith(b"hello world"), split


# --- resumable delimiter scanning -------------------------------------------
#
# Head, chunk-size, and trailer delimiters are found with a per-state cursor
# that resumes where the previous scan stopped, so a slow peer cannot make each
# arrival rescan the whole buffered prefix. A cursor that advanced too far would
# skip a delimiter split across a receive boundary; these drive every split.

CHUNKED_TRAILERS = (
    b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
    b"5\r\nhello\r\n0\r\nX-Trailer: v\r\nX-Other: w\r\n\r\n"
)


@impl
@pytest.mark.asyncio
async def test_head_delivered_one_byte_at_a_time(protocol_cls: type) -> None:
    transport = await drive(protocol_cls, echo_ok, [GET[i : i + 1] for i in range(len(GET))])
    assert transport.buffer.startswith(b"HTTP/1.1 200")


@impl
@pytest.mark.asyncio
async def test_chunked_request_delivered_one_byte_at_a_time(protocol_cls: type) -> None:
    chunks = [CHUNKED[i : i + 1] for i in range(len(CHUNKED))]
    transport = await drive(protocol_cls, echo_ok, chunks)
    assert transport.buffer.endswith(b"hello world")


@impl
@pytest.mark.asyncio
async def test_head_terminator_split_at_every_interior_byte(protocol_cls: type) -> None:
    """Split inside the final CRLFCRLF itself, at all three interior points."""
    end = GET.index(b"\r\n\r\n")
    for offset in range(1, 4):
        split = end + offset
        transport = await drive(protocol_cls, echo_ok, [GET[:split], GET[split:]])
        assert transport.buffer.startswith(b"HTTP/1.1 200"), offset


@impl
@pytest.mark.asyncio
async def test_head_terminator_split_three_ways(protocol_cls: type) -> None:
    """Two receive boundaries inside the terminator: scan, stall, resume, stall."""
    end = GET.index(b"\r\n\r\n")
    for first in range(1, 4):
        for second in range(first + 1, 4):
            transport = await drive(
                protocol_cls,
                echo_ok,
                [GET[: end + first], GET[end + first : end + second], GET[end + second :]],
            )
            assert transport.buffer.startswith(b"HTTP/1.1 200"), (first, second)


@impl
@pytest.mark.asyncio
async def test_trailers_split_every_boundary(protocol_cls: type) -> None:
    for split in range(1, len(CHUNKED_TRAILERS)):
        transport = await drive(
            protocol_cls,
            echo_ok,
            [CHUNKED_TRAILERS[:split], CHUNKED_TRAILERS[split:]],
        )
        assert transport.buffer.endswith(b"hello"), split


@impl
@pytest.mark.asyncio
async def test_trailers_delivered_one_byte_at_a_time(protocol_cls: type) -> None:
    chunks = [CHUNKED_TRAILERS[i : i + 1] for i in range(len(CHUNKED_TRAILERS))]
    transport = await drive(protocol_cls, echo_ok, chunks)
    assert transport.buffer.endswith(b"hello")


@impl
@pytest.mark.asyncio
async def test_empty_trailer_section_split_every_boundary(protocol_cls: type) -> None:
    for split in range(1, len(CHUNKED)):
        transport = await drive(protocol_cls, echo_ok, [CHUNKED[:split], CHUNKED[split:]])
        assert transport.buffer.endswith(b"hello world"), split


@impl
@pytest.mark.asyncio
async def test_pipelined_requests_rescan_from_the_new_request(protocol_cls: type) -> None:
    """Consuming a request must reset the scan cursors for the next one."""
    transport = await drive(protocol_cls, echo_ok, [GET + GET + GET])
    assert transport.buffer.count(b"HTTP/1.1 200") == 3


@impl
@pytest.mark.asyncio
async def test_pipelined_requests_one_byte_at_a_time(protocol_cls: type) -> None:
    stream = GET + GET
    transport = await drive(protocol_cls, echo_ok, [stream[i : i + 1] for i in range(len(stream))])
    assert transport.buffer.count(b"HTTP/1.1 200") == 2


MALFORMED = [
    pytest.param(b"NOT-HTTP\r\n\r\n", id="garbage-request-line"),
    pytest.param(b"GET\r\nHost: x\r\n\r\n", id="request-line-missing-target"),
    pytest.param(b"GET / HTTP/1.1\r\nBad Header\r\n\r\n", id="header-without-colon"),
    pytest.param(
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",
        id="non-hex-chunk-size",
    ),
]


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.parametrize("payload", MALFORMED)
@pytest.mark.asyncio
async def test_malformed_input_status_is_the_same_at_every_split(payload: bytes) -> None:
    """Resumable scanning must not change what malformed input produces.

    The oracle is the payload delivered whole: a parser that resumes correctly
    cannot care where the TCP boundary landed, so all `len(payload) - 1` split
    points must produce the response the single `data_received` produced.
    """
    whole = await drive(_NativeHttpProtocol, echo_ok, [payload])
    expected = parse_responses(whole.buffer)
    for split in range(1, len(payload)):
        chunks = [payload[:split], payload[split:]]
        split_at = await drive(_NativeHttpProtocol, echo_ok, chunks)
        assert parse_responses(split_at.buffer) == expected, split


@impl
@pytest.mark.asyncio
async def test_chunk_extensions_ignored(protocol_cls: type) -> None:
    body = (
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"5;ext=1\r\nhello\r\n0\r\n\r\n"
    )
    transport = await drive(protocol_cls, echo_ok, [body])
    assert transport.buffer.endswith(b"hello")


# --- framing errors ---------------------------------------------------------


@impl
@pytest.mark.asyncio
async def test_conflicting_content_length(protocol_cls: type) -> None:
    called = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        called.append(1)

    req = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\nhello"
    transport = await drive(protocol_cls, app, [req])
    assert transport.buffer.startswith(b"HTTP/1.1 400")
    assert b"connection: close" in transport.buffer.lower()
    assert not called


@impl
@pytest.mark.asyncio
async def test_chunked_body_limit_is_cumulative_while_app_drains(
    protocol_cls: type,
) -> None:
    config = ServerConfig(max_body_bytes=10)
    first = b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n6\r\n123456\r\n"
    second = b"6\r\nabcdef\r\n0\r\n\r\n"
    transport = await drive(protocol_cls, echo_ok, [first, second], config)
    assert transport.buffer.startswith(b"HTTP/1.1 413")


@impl
@pytest.mark.asyncio
async def test_chunked_body_frame_count_is_bounded(protocol_cls: type) -> None:
    config = ServerConfig(max_body_chunks=8)
    request = (
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        + b"1\r\nx\r\n" * 9
        + b"0\r\n\r\n"
    )
    transport = await drive(protocol_cls, echo_ok, [request], config)
    assert transport.buffer.startswith(b"HTTP/1.1 413")


def test_body_chunk_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_body_chunks must be positive"):
        ServerConfig(max_body_chunks=0)


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host_fields",
    [b"", b"Host: first\r\nHost: second\r\n"],
)
async def test_native_http11_requires_exactly_one_host(host_fields: bytes) -> None:
    request = b"GET / HTTP/1.1\r\n" + host_fields + b"\r\n"
    transport = await drive(_NativeHttpProtocol, echo_ok, [request])
    assert transport.buffer.startswith(b"HTTP/1.1 400")


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_header_count_rejects_before_dispatch() -> None:
    called = False

    async def app(scope: dict, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    request = b"GET / HTTP/1.1\r\nHost: x\r\nX-Extra: y\r\n\r\n"
    transport = await drive(_NativeHttpProtocol, app, [request], ServerConfig(max_header_count=1))
    assert transport.buffer.startswith(b"HTTP/1.1 431")
    assert called is False


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_expect_continue_is_answered() -> None:
    request = (
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nExpect: 100-continue\r\n\r\nhello"
    )
    transport = await drive(_NativeHttpProtocol, echo_ok, [request])
    assert transport.buffer.startswith(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200")


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_rejects_malformed_or_forbidden_trailers() -> None:
    request = (
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"1\r\nx\r\n0\r\nContent-Length: 9\r\n\r\n"
    )
    transport = await drive(_NativeHttpProtocol, echo_ok, [request])
    assert transport.buffer.startswith(b"HTTP/1.1 400")


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_pauses_a_pipeline_behind_a_running_request() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(scope: dict, receive: Any, send: Any) -> None:
        started.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok"})

    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = _NativeHttpProtocol(slow, ServerConfig(max_header_bytes=1024), loop, set())
    protocol.connection_made(transport)
    feed(protocol, GET)
    await started.wait()
    feed(protocol, b"X" * 1024)
    assert transport.reading_paused is True
    release.set()
    await _settle()


@impl
@pytest.mark.asyncio
async def test_transfer_encoding_and_content_length(protocol_cls: type) -> None:
    called = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        called.append(1)

    req = (
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
        b"Content-Length: 5\r\n\r\n0\r\n\r\n"
    )
    transport = await drive(protocol_cls, app, [req])
    assert transport.buffer.startswith(b"HTTP/1.1 400")
    assert not called


@impl
@pytest.mark.asyncio
async def test_oversized_request_line(protocol_cls: type) -> None:
    config = ServerConfig(max_request_line=64)
    req = b"GET /" + b"a" * 200 + b" HTTP/1.1\r\nHost: x\r\n\r\n"
    transport = await drive(protocol_cls, echo_ok, [req], config)
    assert transport.buffer.startswith(b"HTTP/1.1 414")


@impl
@pytest.mark.asyncio
async def test_oversized_body(protocol_cls: type) -> None:
    config = ServerConfig(max_body_bytes=4)
    called = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        called.append(1)

    req = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n"
    transport = await drive(protocol_cls, app, [req], config)
    assert transport.buffer.startswith(b"HTTP/1.1 413")
    assert not called


# --- read backpressure ------------------------------------------------------


@impl
@pytest.mark.asyncio
async def test_read_pause_resume_watermarks(protocol_cls: type) -> None:
    config = ServerConfig(read_high_water=16)
    gate = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await gate.wait()
        while True:
            m = await receive()
            if m["type"] == "http.disconnect" or not m.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    loop = asyncio.get_running_loop()
    registry: set[Any] = set()
    transport = FakeTransport()
    protocol = protocol_cls(app, config, loop, registry)
    protocol.connection_made(transport)
    feed(protocol, b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n")
    feed(protocol, b"x" * 100)
    await _settle()
    assert transport.reading_paused
    gate.set()
    await _settle()
    assert not transport.reading_paused


# --- write backpressure -----------------------------------------------------


@impl
@pytest.mark.asyncio
async def test_write_pause_resume_awaitable(protocol_cls: type) -> None:
    done = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        # First streaming chunk; blocks on drain while writing is paused.
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        done.set()

    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = protocol_cls(app, ServerConfig(), loop, set())
    protocol.connection_made(transport)
    protocol.pause_writing()
    feed(protocol, GET)
    await _settle()
    assert not done.is_set()  # blocked on drain
    protocol.resume_writing()
    await _settle()
    assert done.is_set()


# --- timeouts ---------------------------------------------------------------


@impl
@pytest.mark.asyncio
async def test_keep_alive_timeout(protocol_cls: type) -> None:
    config = ServerConfig(keep_alive_timeout=0.02)
    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = protocol_cls(echo_ok, config, loop, set())
    protocol.connection_made(transport)
    await asyncio.wait_for(transport.closed_event.wait(), timeout=1.0)
    assert transport.closed
    assert transport.buffer == b""  # app never invoked


@impl
@pytest.mark.asyncio
async def test_request_timeout(protocol_cls: type) -> None:
    config = ServerConfig(request_timeout=0.02, keep_alive_timeout=5.0)
    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = protocol_cls(echo_ok, config, loop, set())
    protocol.connection_made(transport)
    # Send a partial head; the request timer starts once head bytes arrive.
    feed(protocol, b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n")
    await asyncio.wait_for(transport.closed_event.wait(), timeout=1.0)
    assert transport.buffer.startswith(b"HTTP/1.1 408")


# --- disconnects ------------------------------------------------------------


@impl
@pytest.mark.asyncio
async def test_disconnect_before_body_complete(protocol_cls: type) -> None:
    saw_disconnect = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        while True:
            m = await receive()
            if m["type"] == "http.disconnect":
                saw_disconnect.append(1)
                return
            if not m.get("more_body", False):
                break

    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = protocol_cls(app, ServerConfig(), loop, set())
    protocol.connection_made(transport)
    feed(protocol, b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n")
    await _settle()
    protocol.connection_lost(None)
    await _settle()
    assert saw_disconnect


# --- application errors ------------------------------------------------------


@impl
@pytest.mark.asyncio
async def test_app_exception_before_start(protocol_cls: type) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        raise RuntimeError("boom")

    transport = await drive(protocol_cls, app, [GET])
    assert transport.buffer.startswith(b"HTTP/1.1 500")
    assert b"connection: close" in transport.buffer.lower()


@impl
@pytest.mark.asyncio
async def test_app_exception_after_start(protocol_cls: type) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError("boom")

    transport = await drive(protocol_cls, app, [GET])
    # Only one response head; no second response injected.
    assert parse_responses(transport.buffer) == [b"HTTP/1.1 200 OK"]
    assert transport.closed


# --- body suppression -------------------------------------------------------


@impl
@pytest.mark.asyncio
async def test_head_suppresses_body(protocol_cls: type) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"5")]}
        )
        await send({"type": "http.response.body", "body": b"hello"})

    req = b"HEAD / HTTP/1.1\r\nHost: x\r\n\r\n"
    transport = await drive(protocol_cls, app, [req])
    head, _, body = transport.buffer.partition(b"\r\n\r\n")
    assert b"content-length: 5" in head.lower()
    assert body == b""


@impl
@pytest.mark.parametrize("status", [204, 304])
@pytest.mark.asyncio
async def test_no_body_statuses(protocol_cls: type, status: int) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"nope"})

    transport = await drive(protocol_cls, app, [GET])
    _, _, body = transport.buffer.partition(b"\r\n\r\n")
    assert body == b""


@impl
@pytest.mark.asyncio
async def test_server_supplies_default_response_headers(protocol_cls: type) -> None:
    transport = await drive(protocol_cls, echo_ok, [GET])
    head = bytes(transport.buffer.partition(b"\r\n\r\n")[0])
    headers = dict(line.split(b": ", 1) for line in head.split(b"\r\n")[1:] if b": " in line)

    assert headers[b"server"] == b"wreath"
    parsed = parsedate_to_datetime(headers[b"date"].decode("ascii"))
    assert parsed.tzinfo is not None


@impl
@pytest.mark.asyncio
async def test_application_headers_override_server_defaults(protocol_cls: type) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"Server", b"application"), (b"Date", b"custom")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    transport = await drive(protocol_cls, app, [GET])
    head = transport.buffer.partition(b"\r\n\r\n")[0].lower()
    assert head.count(b"server:") == 1
    assert b"server: application" in head
    assert head.count(b"date:") == 1
    assert b"date: custom" in head


@impl
@pytest.mark.asyncio
async def test_server_default_headers_can_be_disabled(protocol_cls: type) -> None:
    config = ServerConfig(server_header=None, date_header=False)
    transport = await drive(protocol_cls, echo_ok, [GET], config)
    head = transport.buffer.partition(b"\r\n\r\n")[0].lower()
    assert b"server:" not in head
    assert b"date:" not in head


@impl
@pytest.mark.asyncio
async def test_existing_protocol_observes_refreshed_default_header_wire(
    protocol_cls: type,
) -> None:
    config = ServerConfig(server_header="before", date_header=False)
    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = protocol_cls(echo_ok, config, loop, set())
    protocol.connection_made(transport)

    defaults = config._default_response_headers
    defaults.server = b"after"
    defaults.refresh(False)
    feed(protocol, GET)
    await _settle()

    head = transport.buffer.partition(b"\r\n\r\n")[0].lower()
    assert b"server: after\r\n" in head
    assert b"server: before\r\n" not in head


# --- HTTP/1.0 ---------------------------------------------------------------


@impl
@pytest.mark.asyncio
async def test_http10_defaults_close(protocol_cls: type) -> None:
    req = b"GET / HTTP/1.0\r\nHost: x\r\n\r\n"
    transport = await drive(protocol_cls, echo_ok, [req])
    assert b"connection: close" in transport.buffer.lower()
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_http10_keep_alive(protocol_cls: type) -> None:
    req = b"GET / HTTP/1.0\r\nHost: x\r\nConnection: keep-alive\r\nContent-Length: 0\r\n\r\n"
    transport = await drive(protocol_cls, echo_ok, [req])
    assert b"connection: keep-alive" in transport.buffer.lower()
    assert not transport.closed


@impl
@pytest.mark.asyncio
async def test_http10_keep_alive_requires_an_exact_connection_token(
    protocol_cls: type,
) -> None:
    req = b"GET / HTTP/1.0\r\nHost: x\r\nConnection: xkeep-alive\r\n\r\n"
    transport = await drive(protocol_cls, echo_ok, [req])
    assert b"connection: close" in transport.buffer.lower()
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_http11_close_requires_an_exact_connection_token(
    protocol_cls: type,
) -> None:
    req = b"GET / HTTP/1.1\r\nHost: x\r\nConnection: disclose\r\n\r\n"
    transport = await drive(protocol_cls, echo_ok, [req])
    assert b"connection: keep-alive" in transport.buffer.lower()
    assert not transport.closed


# --- native hot path --------------------------------------------------------


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_caches_connection_metadata_across_requests() -> None:
    transport = await drive(_NativeHttpProtocol, echo_ok, [GET, GET])

    assert transport.extra_info_calls.count("sockname") == 1
    assert transport.extra_info_calls.count("peername") == 1
    assert transport.extra_info_calls.count("sslcontext") == 1


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_coalesces_fixed_response_head_and_body() -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", b"2")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    transport = await drive(_NativeHttpProtocol, app, [GET])

    assert len(transport.writes) == 1
    assert transport.writes[0].endswith(b"\r\n\r\nok")


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_large_response_retains_original_body_for_writelines() -> None:
    body = b"x" * (16 * 1024 + 1)

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    transport = await drive(_NativeHttpProtocol, app, [GET])

    assert len(transport.writeline_batches) == 1
    head, emitted_body = transport.writeline_batches[0]
    assert bytes(head).startswith(b"HTTP/1.1 200")
    assert emitted_body is body


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_snapshots_headers_before_response_body() -> None:
    headers = [(b"x-original", b"before")]

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        headers[0] = (b"x-mutated", b"after")
        headers.append((b"x-added", b"late"))
        await send({"type": "http.response.body", "body": b"ok"})

    transport = await drive(_NativeHttpProtocol, app, [GET])
    head, _, _ = bytes(transport.buffer).partition(b"\r\n\r\n")

    assert b"x-original: before" in head
    assert b"x-mutated" not in head
    assert b"x-added" not in head


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_does_not_retain_large_response_serialization_buffer() -> None:
    response_count = 0

    async def app(scope: dict, receive: Any, send: Any) -> None:
        nonlocal response_count
        response_count += 1
        body = b"x" if response_count == 1 else b"x" * (16 * 1024)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    loop = asyncio.get_running_loop()
    protocol = _NativeHttpProtocol(app, ServerConfig(), loop, set())
    transport = FakeTransport()
    protocol.connection_made(transport)
    tracemalloc.start()
    try:
        feed(protocol, GET)
        await _settle()
        transport.buffer.clear()
        transport.writes.clear()
        transport.write_objects.clear()
        gc.collect()
        baseline, _ = tracemalloc.get_traced_memory()

        feed(protocol, GET)
        await _settle()
        transport.buffer.clear()
        transport.writes.clear()
        transport.write_objects.clear()
        gc.collect()
        retained, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        protocol.connection_lost(None)
        await _settle()

    assert retained - baseline < 8 * 1024


# --- collectability ---------------------------------------------------------


@impl
@pytest.mark.asyncio
async def test_protocol_collectable_after_loss(protocol_cls: type) -> None:
    import gc
    import weakref

    loop = asyncio.get_running_loop()
    registry: set[Any] = set()
    transport = FakeTransport()
    protocol = protocol_cls(echo_ok, ServerConfig(), loop, registry)
    protocol.connection_made(transport)
    feed(protocol, GET)
    await _settle()
    try:
        ref = weakref.ref(protocol)
    except TypeError:
        ref = None  # native type may not be weak-referenceable
    protocol.connection_lost(None)
    await _settle()
    assert not registry  # connection removed itself, retaining no reference
    del protocol
    gc.collect()
    if ref is not None:
        assert ref() is None


# --- wreath.response one-shot extension -----------------------------------------


async def one_shot_ok(scope: dict, receive: Any, send: Any) -> None:
    await send(
        {
            "type": "wreath.response",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
            "body": b"hello",
        }
    )


@impl
@pytest.mark.asyncio
async def test_extension_advertised_in_scope(protocol_cls: type) -> None:
    app, captured = make_scope_capture()
    await drive(protocol_cls, app, [GET])
    assert captured
    assert "wreath.response" in captured[0]["extensions"]


@impl
@pytest.mark.asyncio
async def test_one_shot_matches_start_body_pair(protocol_cls: type) -> None:
    async def paired(scope: dict, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"hello"})

    one_shot_wire = (await drive(protocol_cls, one_shot_ok, [GET])).buffer
    paired_wire = (await drive(protocol_cls, paired, [GET])).buffer
    assert bytes(one_shot_wire) == bytes(paired_wire)
    assert b"content-length: 5" in bytes(one_shot_wire)
    assert bytes(one_shot_wire).endswith(b"\r\n\r\nhello")


@pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")
@pytest.mark.asyncio
async def test_native_typed_response_abi_emits_complete_response() -> None:
    async def typed_response(scope: dict, receive: Any, send: Any) -> None:
        protocol = send.__self__
        await protocol._wreath_response(
            200, [(b"content-type", b"text/plain"), (b"x-native", b"typed")], b"hello"
        )

    wire = bytes((await drive(_NativeHttpProtocol, typed_response, [GET])).buffer)

    assert wire.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"x-native: typed\r\n" in wire
    assert b"content-length: 5\r\n" in wire
    assert wire.endswith(b"\r\n\r\nhello")


@impl
@pytest.mark.asyncio
async def test_one_shot_keep_alive_second_request(protocol_cls: type) -> None:
    transport = await drive(protocol_cls, one_shot_ok, [GET, GET])
    assert bytes(transport.buffer).count(b"HTTP/1.1 200 OK") == 2
    assert not transport.closed


@impl
@pytest.mark.asyncio
async def test_one_shot_after_start_rejected(protocol_cls: type) -> None:
    errors: list = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            await send({"type": "wreath.response", "status": 200, "headers": [], "body": b"x"})
        except RuntimeError as exc:
            errors.append(str(exc))
            raise

    transport = await drive(protocol_cls, app, [GET])
    assert errors == ["response already started"]
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_one_shot_head_suppresses_body(protocol_cls: type) -> None:
    async def paired(scope: dict, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"hello"})

    head = b"HEAD / HTTP/1.1\r\nHost: x\r\n\r\n"
    one_shot_wire = bytes((await drive(protocol_cls, one_shot_ok, [head])).buffer)
    paired_wire = bytes((await drive(protocol_cls, paired, [head])).buffer)
    assert one_shot_wire == paired_wire
    assert one_shot_wire.endswith(b"\r\n\r\n")  # head only, no body bytes


# --- native buffered ingress (asyncio.BufferedProtocol) -----------------------

native_only = pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built")


def _make_native(app: Any, config: ServerConfig | None = None) -> tuple[Any, FakeTransport]:
    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = _NativeHttpProtocol(app, config or ServerConfig(), loop, set())
    protocol.connection_made(transport)
    return protocol, transport


@native_only
def test_native_is_buffered_protocol() -> None:
    assert issubclass(_NativeHttpProtocol, asyncio.BufferedProtocol)


@native_only
@pytest.mark.asyncio
async def test_get_buffer_sizehints_return_writable_nonempty() -> None:
    protocol, _ = _make_native(echo_ok)
    for hint in (-1, 0, 1, 17, 4096, 1 << 20):
        view = memoryview(protocol.get_buffer(hint))
        assert len(view) > 0
        assert not view.readonly
        if hint > 0:
            assert len(view) >= min(hint, 32768)
        view[0:1] = b"G"  # actually writable
        protocol.buffer_updated(0)  # zero-byte update is harmless
        view.release()
    protocol.connection_lost(None)
    await _settle()


@native_only
@pytest.mark.asyncio
async def test_buffered_fragmented_request_invokes_app() -> None:
    protocol, transport = _make_native(echo_ok)
    payload = b"POST /frag HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello"
    for i in range(len(payload)):
        feed(protocol, payload[i : i + 1])
    await _settle()
    assert transport.buffer.startswith(b"HTTP/1.1 200")
    assert transport.buffer.endswith(b"hello")
    protocol.connection_lost(None)
    await _settle()


@native_only
@pytest.mark.asyncio
async def test_buffered_keep_alive_requests_reuse_allocation() -> None:
    protocol, transport = _make_native(echo_ok)
    first = memoryview(protocol.get_buffer(-1))
    capacity = len(first)
    first[: len(GET)] = GET
    protocol.buffer_updated(len(GET))
    first.release()
    await _settle()
    for _ in range(5):
        view = memoryview(protocol.get_buffer(-1))
        # Fully drained keep-alive traffic re-offers the same-size tail
        # instead of growing the allocation.
        assert len(view) == capacity
        view[: len(GET)] = GET
        protocol.buffer_updated(len(GET))
        view.release()
        await _settle()
    assert bytes(transport.buffer).count(b"HTTP/1.1 200") == 6
    protocol.connection_lost(None)
    await _settle()


@native_only
@pytest.mark.asyncio
async def test_pipelined_requests_survive_deferred_compaction() -> None:
    # Hold an aliasing export across the parse so do_consume() must defer its
    # reset/compaction, then release it and keep the connection going.
    protocol, transport = _make_native(echo_ok)
    view = memoryview(protocol.get_buffer(-1))
    alias = memoryview(protocol)  # second export of the same offer
    data = GET + GET
    view[: len(data)] = data
    protocol.buffer_updated(len(data))
    await _settle()
    assert parse_responses(transport.buffer) == [b"HTTP/1.1 200 OK", b"HTTP/1.1 200 OK"]
    view.release()
    alias.release()  # exports drop to zero; deferred compaction may now run
    feed(protocol, GET)
    await _settle()
    assert parse_responses(transport.buffer) == [b"HTTP/1.1 200 OK"] * 3
    protocol.connection_lost(None)
    await _settle()


@native_only
@pytest.mark.asyncio
async def test_released_view_permits_later_growth_and_compaction() -> None:
    protocol, transport = _make_native(echo_ok)
    view = memoryview(protocol.get_buffer(-1))
    view.release()  # abandoned offer (transport recv would have raised)
    # A released view must allow later offers, buffer growth (the pipelined
    # backlog below exceeds one 64 KiB offer), and deferred compaction.
    count = 3000
    feed(protocol, GET * count)
    await _settle()
    assert parse_responses(transport.buffer) == [b"HTTP/1.1 200 OK"] * count
    protocol.connection_lost(None)
    await _settle()


@native_only
@pytest.mark.asyncio
async def test_buffer_updated_invalid_counts_rejected() -> None:
    protocol, _ = _make_native(echo_ok)
    view = memoryview(protocol.get_buffer(-1))
    with pytest.raises(ValueError):
        protocol.buffer_updated(-1)
    view.release()
    view = memoryview(protocol.get_buffer(-1))
    with pytest.raises(ValueError):
        protocol.buffer_updated(len(view) + 1)
    view.release()
    with pytest.raises(RuntimeError):
        protocol.buffer_updated(1)  # no active offer
    protocol.connection_lost(None)
    await _settle()


@native_only
@pytest.mark.asyncio
async def test_overlapping_read_offers_rejected() -> None:
    protocol, _ = _make_native(echo_ok)
    view = memoryview(protocol.get_buffer(-1))
    with pytest.raises(RuntimeError):
        protocol.get_buffer(-1)
    with pytest.raises(RuntimeError):
        protocol.data_received(b"x")  # mixing ingestion paths is refused
    view[: len(GET)] = GET
    protocol.buffer_updated(len(GET))
    view.release()
    await _settle()
    protocol.data_received(GET)  # compatible again once the offer is done
    await _settle()
    protocol.connection_lost(None)
    await _settle()


@native_only
@pytest.mark.asyncio
async def test_export_without_offer_rejected() -> None:
    protocol, _ = _make_native(echo_ok)
    with pytest.raises(BufferError):
        memoryview(protocol)
    protocol.connection_lost(None)
    await _settle()


@native_only
@pytest.mark.asyncio
async def test_connection_lost_with_live_view_is_safe() -> None:
    protocol, _ = _make_native(echo_ok)
    view = memoryview(protocol.get_buffer(-1))
    protocol.connection_lost(None)
    await _settle()
    view[:4] = b"GET "  # memory must remain valid while exported
    assert bytes(view[:4]) == b"GET "
    with pytest.raises(RuntimeError):
        protocol.buffer_updated(4)  # the offer died with the connection
    view.release()


@native_only
@pytest.mark.asyncio
async def test_native_data_received_compat_path_still_parses() -> None:
    # data_received() stays as the copying compatibility path (delegating
    # transports, direct harnesses); exercise it explicitly.
    protocol, transport = _make_native(echo_ok)
    payload = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello"
    for i in range(len(payload)):
        feed(protocol, payload[i : i + 1], force_data_received=True)
    await _settle()
    feed(protocol, GET + GET, force_data_received=True)
    await _settle()
    # parse_responses misses a status line glued to the POST body ("helloHTTP/")
    assert bytes(transport.buffer).count(b"HTTP/1.1 200 OK") == 3
    assert b"hello" in transport.buffer
    protocol.connection_lost(None)
    await _settle()


# --- integer rendering in the response head ---------------------------------
#
# The status code, the content-length, and every chunk size line are written by
# hand in the native protocol rather than through `PyOS_snprintf`, because
# glibc's printf machinery measured at ~2% of a saturated metal worker's cycles
# for what is at most a 20-digit number. A hand-rolled writer is only worth
# having if it is right at the edges, so these drive the values a `%zd`/`%zx`
# format string would have covered for free: zero, one digit, every digit-count
# boundary, and both sides of each hex nibble boundary.



@impl
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "size",
    [0, 1, 9, 10, 99, 100, 999, 1000, 65535, 65536, 1234567, 10_000_000],
)
async def test_response_content_length_renders_at_digit_boundaries(
    protocol_cls: type, size: int
) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"z" * size})

    transport = await drive(protocol_cls, app, [GET])
    head = bytes(transport.buffer).split(b"\r\n\r\n", 1)[0]
    assert b"content-length: %d\r\n" % size in head.lower(), head


@impl
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [100, 101, 200, 204, 301, 404, 418, 500, 599, 999])
async def test_response_status_line_renders_every_three_digit_code(
    protocol_cls: type, status: int
) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    transport = await drive(protocol_cls, app, [GET])
    assert bytes(transport.buffer).startswith(b"HTTP/1.1 %d " % status)


@impl
@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1, 15, 16, 17, 255, 256, 257, 4095, 4096])
async def test_streaming_chunk_size_lines_render_across_nibble_boundaries(
    protocol_cls: type, size: int
) -> None:
    """A chunk size is hex, so its boundaries are the nibble ones, not the decimal."""

    async def app(scope: dict, receive: Any, send: Any) -> None:
        # No content-length: HTTP/1.1 framing falls to chunked, which is the
        # only path that writes a size line.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"q" * size, "more_body": True})
        await send({"type": "http.response.body", "body": b""})

    transport = await drive(protocol_cls, app, [GET])
    head, _, body = bytes(transport.buffer).partition(b"\r\n\r\n")
    assert b"transfer-encoding: chunked" in head.lower(), head
    assert body.startswith(b"%x\r\n" % size), body[:32]
    assert body.endswith(b"0\r\n\r\n"), body[-16:]
