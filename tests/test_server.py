"""Loopback socket integration tests for ``wreath.server``.

These drive the server end to end over a real (asyncio) TCP transport, asserting
on bytes on the wire. They exercise whichever protocol implementation the
facade selects (native when built, pure otherwise); ``WREATH_PURE`` selection is
checked in an isolated subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import subprocess
import sys
import time
from typing import Any

import pytest

import wreath
from wreath.auth import BearerTokenBackend, Identity, authenticated
from wreath.http_client import DestinationPolicy, HTTPClient
from wreath.policy import HttpPolicy, ProxyPolicy
from wreath.server import ServerConfig, TLSConfig, _select_protocol, serve


async def _raw_request(port: int, data: bytes, *, read_until_close: bool = False,
                       reads: int = 1) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    if read_until_close:
        chunks = await reader.read()
    else:
        chunks = b""
        for _ in range(reads):
            chunk = await asyncio.wait_for(reader.read(65536), timeout=2.0)
            if not chunk:
                break
            chunks += chunk
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionResetError, BrokenPipeError):
        pass
    return chunks


async def _serve(app: Any, **config: Any) -> Any:
    cfg = ServerConfig(host="127.0.0.1", port=0, lifespan="off", **config)
    server = await serve(app, cfg)
    return server


def _port(server: Any) -> int:
    return server.sockets[0].getsockname()[1]


# --- apps -------------------------------------------------------------------

def make_wreath_app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/")
    async def index(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("hello")

    @app.post("/echo")
    async def echo(request: wreath.Request) -> wreath.Response:
        body = await request.body()
        return wreath.Response(body)

    @app.get("/stream")
    async def stream(request: wreath.Request) -> wreath.response.StreamingResponse:
        async def gen() -> Any:
            for part in (b"a", b"b", b"c"):
                yield part

        return wreath.response.StreamingResponse(gen())

    return app


async def minimal_asgi(scope: dict, receive: Any, send: Any) -> None:
    assert scope["type"] == "http"
    while True:
        m = await receive()
        if m["type"] == "http.disconnect" or not m.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"minimal"})


# --- tests ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_serve_wreath_app() -> None:
    server = await _serve(make_wreath_app())
    try:
        resp = await _raw_request(_port(server), b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b"HTTP/1.1 200" in resp
        assert resp.endswith(b"hello")
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_serve_minimal_asgi_app() -> None:
    server = await _serve(minimal_asgi)
    try:
        resp = await _raw_request(_port(server), b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        assert resp.endswith(b"minimal")
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_keep_alive_reuse() -> None:
    server = await _serve(make_wreath_app())
    try:
        port = _port(server)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        for _ in range(3):
            writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"hello"), timeout=2.0)
            assert b"HTTP/1.1 200" in head
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_fixed_body() -> None:
    server = await _serve(make_wreath_app())
    try:
        req = (b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello")
        resp = await _raw_request(_port(server), req)
        assert resp.endswith(b"hello")
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_chunked_body() -> None:
    server = await _serve(make_wreath_app())
    try:
        req = (
            b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        )
        resp = await _raw_request(_port(server), req)
        assert resp.endswith(b"hello world")
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_ambiguous_proxy_request_cannot_activate_an_outbound_pivot() -> None:
    """Reject ambiguous framing before routing a second, protected request.

    The Wreath listener sees the connection as a trusted proxy and the canary is
    a real internal HTTP server. A normal authenticated request first proves the
    handler and outbound leg are live. The attack then sends TE+CL ambiguity
    followed by a complete request to that handler on the same TCP stream.
    """
    canary_requests = 0

    async def canary(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal canary_requests
        canary_requests += 1
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    internal = await asyncio.start_server(canary, "127.0.0.1", 0)
    internal_port = internal.sockets[0].getsockname()[1]
    outbound = HTTPClient(
        "pivot-canary",
        base_url=f"http://127.0.0.1:{internal_port}",
        destination=DestinationPolicy(allow_loopback=True),
    )
    await outbound.start()

    pivots = 0
    app = wreath.Wreath(
        http_policy=HttpPolicy(
            proxy=ProxyPolicy(trusted=("127.0.0.0/8",))
        )
    )
    app.configure_auth(
        BearerTokenBackend(
            lambda token: Identity(id="attacker") if token == "valid" else None
        )
    )

    @app.get("/pivot")
    @authenticated()
    async def pivot(request: wreath.Request) -> wreath.Response:
        nonlocal pivots
        pivots += 1
        response = await outbound.get("/internal")
        return wreath.Response(response.body)

    server = await _serve(app)
    headers = (
        b"Host: app.example\r\n"
        b"Authorization: Bearer valid\r\n"
        b"X-Forwarded-For: 203.0.113.9\r\n"
    )
    try:
        control = await _raw_request(
            _port(server), b"GET /pivot HTTP/1.1\r\n" + headers + b"\r\n"
        )
        assert b"HTTP/1.1 200" in control
        assert pivots == canary_requests == 1

        attack = (
            b"POST /ignored HTTP/1.1\r\n"
            + headers
            + b"Transfer-Encoding: chunked\r\n"
            b"Content-Length: 4\r\n\r\n"
            b"0\r\n\r\n"
            b"GET /pivot HTTP/1.1\r\n"
            + headers
            + b"Connection: close\r\n\r\n"
        )
        refused = await asyncio.wait_for(
            _raw_request(_port(server), attack, read_until_close=True), timeout=2.0
        )

        assert refused.startswith(b"HTTP/1.1 400")
        assert b"HTTP/1.1 200" not in refused
        assert pivots == 1
        assert canary_requests == 1
    finally:
        await server.close()
        await outbound.close()
        internal.close()
        await internal.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    (
        b"http://evil.example/pivot",
        b"https://good.example@evil.example/pivot",
        b"http://127.0.0.1:80/pivot",
    ),
)
async def test_absolute_form_target_is_rejected_before_route_activation(
    target: bytes,
) -> None:
    """An origin server must not reinterpret proxy-form targets as local paths."""
    activated = 0
    app = wreath.Wreath()

    @app.get("/pivot")
    async def pivot(request: wreath.Request) -> str:
        nonlocal activated
        activated += 1
        return "sensitive"

    server = await _serve(app)
    try:
        response = await _raw_request(
            _port(server), b"GET " + target + b" HTTP/1.1\r\nHost: good.example\r\n\r\n"
        )
    finally:
        await server.close()

    assert response.startswith(b"HTTP/1.1 4")
    assert b"sensitive" not in response
    assert activated == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header_block",
    (
        b"X-Test: one\r\n two\r\n",
        b"Transfer-Encoding : chunked\r\n",
        b"Host : app.example\r\n",
        b"X-Test: safe\x00hidden\r\n",
    ),
)
async def test_proxy_differential_header_shapes_never_reach_the_app(
    header_block: bytes,
) -> None:
    """Reject obsolete folding, pre-colon whitespace, and embedded controls."""
    activated = 0

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal activated
        activated += 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    server = await _serve(app)
    try:
        response = await _raw_request(
            _port(server),
            b"GET / HTTP/1.1\r\nHost: app.example\r\n" + header_block + b"\r\n",
        )
    finally:
        await server.close()

    assert response.startswith(b"HTTP/1.1 400")
    assert activated == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value"),
    (
        (b"x-safe\r\nx-injected", b"yes"),
        (b"x-safe", b"ok\r\nx-injected: yes"),
        (b"x-safe", b"ok\n\nHTTP/1.1 200 Injected"),
    ),
)
async def test_response_splitting_bytes_never_reach_the_wire(
    name: bytes, value: bytes
) -> None:
    """Validate at Wreath's server boundary even for a raw ASGI application."""

    async def malicious(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(name, value)],
            }
        )
        await send({"type": "http.response.body", "body": b"unsafe"})

    server = await _serve(malicious)
    try:
        response = await _raw_request(
            _port(server), b"GET / HTTP/1.1\r\nHost: app.example\r\n\r\n",
            read_until_close=True,
        )
    finally:
        await server.close()

    lowered = response.lower()
    assert b"\r\nx-injected:" not in lowered
    assert b"http/1.1 200 injected" not in lowered
    assert b"unsafe" not in lowered


@pytest.mark.asyncio
async def test_streaming_response() -> None:
    server = await _serve(make_wreath_app())
    try:
        resp = await _raw_request(_port(server), b"GET /stream HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b"transfer-encoding: chunked" in resp.lower()
        # Chunked framing carries the three parts.
        body = resp.split(b"\r\n\r\n", 1)[1]
        assert b"a" in body and b"b" in body and b"c" in body
        assert body.endswith(b"0\r\n\r\n")
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_pipelined_order() -> None:
    server = await _serve(make_wreath_app())
    try:
        port = _port(server)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n" * 2)
        await writer.drain()
        buf = b""
        while buf.count(b"HTTP/1.1 200") < 2:
            buf += await asyncio.wait_for(reader.read(65536), timeout=2.0)
        assert buf.count(b"HTTP/1.1 200") == 2
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_malformed_does_not_call_app() -> None:
    called = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        called.append(1)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    server = await _serve(app)
    try:
        req = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\nx"
        resp = await _raw_request(_port(server), req)
        assert b"HTTP/1.1 400" in resp
        assert not called
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_an_idle_connection_reports_no_work_to_drain() -> None:
    """The drain predicate must read a number, not fail to find one.

    `_has_work_to_drain` treats a protocol with no readable `active_requests`
    as busy, because it cannot tell. That fallback is meant for a foreign
    protocol object, and every protocol Wreath ships has to stay out of it: a
    shipped protocol that lands there makes every graceful close spend the
    whole `shutdown_timeout` draining a connection that owes nothing.

    Asserted on the attribute *and* the predicate, because reading zero from
    one idle connection would also pass if the counter were hard-wired.
    """
    server = await _serve(make_wreath_app())
    try:
        port = _port(server)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"hello"), timeout=3.0)
        protocol = next(iter(server._protocols))
        assert protocol.active_requests == 0, "an answered request is not still in flight"
        assert server._has_work_to_drain() is False
        writer.close()
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await writer.wait_closed()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_a_tls_connection_owing_nothing_does_not_hold_the_close() -> None:
    """A peer that never answers `close_notify` must not detain shutdown.

    `loop.create_server` defaults `ssl_shutdown_timeout` to asyncio's 30
    seconds, and the TLS unwrap it bounds runs on *every* connection close --
    so a client that vanishes without a close_notify keeps its transport, and
    therefore the protocol, alive for half a minute. During a graceful close
    that is the whole cost: the drain window, the teardown wait and
    `Server.wait_closed()` all queue behind one unwrap nobody will complete.

    Measured before this test existed: 30.01s of teardown for a gRPC call
    whose `call` phase was 0.01s. The bound asserted here is deliberately
    loose -- the defect is a 30-second stall, and a threshold near the real
    cost would make this flaky on a loaded machine for no extra evidence.

    The client here is a **raw blocking socket that stops being serviced**, not
    an asyncio one. Two shapes look like they should reproduce this and do not:
    `transport.abort()` sends a TCP RST, which kills the transport instantly,
    and an idle `open_connection(ssl=...)` peer answers the server's
    close_notify by itself because asyncio's own SSL protocol is still running.
    The stall needs a peer that is connected and simply never replies.
    """
    cert, key = _make_self_signed()
    server = await serve(
        make_wreath_app(),
        ServerConfig(host="127.0.0.1", port=0, lifespan="off"),
        tls=TLSConfig(certfile=cert, keyfile=key),
    )
    port = _port(server)
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE

    def one_request() -> ssl.SSLSocket:
        sock = client_ctx.wrap_socket(
            socket.create_connection(("127.0.0.1", port)), server_hostname="localhost"
        )
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b"200 OK" in sock.recv(4096)
        return sock  # left open and unread: nothing will answer a close_notify

    sock = await asyncio.to_thread(one_request)
    started = time.monotonic()
    try:
        await server.close()
        await server.wait_closed()
        elapsed = time.monotonic() - started
    finally:
        sock.close()
    assert elapsed < 5.0, f"shutdown waited out a TLS unwrap ({elapsed:.1f}s)"


@pytest.mark.asyncio
async def test_graceful_shutdown_drains_active_response() -> None:
    release = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"2")]})
        await release.wait()
        await send({"type": "http.response.body", "body": b"ok"})

    server = await _serve(app, shutdown_timeout=5.0)
    port = _port(server)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()
    await asyncio.sleep(0.05)

    close_task = asyncio.ensure_future(server.close())
    await asyncio.sleep(0.05)
    release.set()  # let the in-flight response finish during shutdown

    resp = await asyncio.wait_for(reader.read(), timeout=3.0)
    assert resp.endswith(b"ok")
    await close_task
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionResetError, BrokenPipeError):
        pass


@pytest.mark.asyncio
async def test_tls_transport() -> None:
    cert_dir = _make_self_signed()
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_dir[0], cert_dir[1])

    server = await serve(
        make_wreath_app(),
        ServerConfig(host="127.0.0.1", port=0, lifespan="off"),
        ssl=server_ctx,
    )
    try:
        port = _port(server)
        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_ctx)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        resp = await asyncio.wait_for(reader.readuntil(b"hello"), timeout=3.0)
        assert b"HTTP/1.1 200" in resp
        writer.close()
        try:
            await writer.wait_closed()
        except ssl.SSLError:
            pass
    finally:
        await server.close()


def test_lifespan_runs() -> None:
    events: list[str] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                m = await receive()
                if m["type"] == "lifespan.startup":
                    events.append("startup")
                    await send({"type": "lifespan.startup.complete"})
                elif m["type"] == "lifespan.shutdown":
                    events.append("shutdown")
                    await send({"type": "lifespan.shutdown.complete"})
                    return

    async def scenario() -> None:
        server = await serve(app, ServerConfig(host="127.0.0.1", port=0, lifespan="on"))
        await server.close()

    asyncio.run(scenario())
    assert events == ["startup", "shutdown"]


def test_wreath_pure_selection_in_subprocess() -> None:
    code = (
        "import os; os.environ['WREATH_PURE']='1';"
        "from wreath.server import _select_protocol;"
        "cls=_select_protocol();"
        "print(cls.__module__)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "wreath._pure.server"


# --- helpers ----------------------------------------------------------------

def _make_self_signed() -> tuple[str, str]:
    import datetime
    import tempfile

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:  # pragma: no cover - optional
        pytest.skip("cryptography not available for TLS test")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    tmp = tempfile.mkdtemp()
    cert_path = f"{tmp}/cert.pem"
    key_path = f"{tmp}/key.pem"
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    return cert_path, key_path


def _free_port() -> int:  # pragma: no cover - helper
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- Step 1: protocol config and module-boundary freeze ---------------------

def test_http_protocol_alias_remains_http1() -> None:
    # The selected implementation (native when built, else pure) must expose
    # HttpProtocol as an alias of Http1Protocol.
    selected = _select_protocol()
    module = sys.modules[selected.__module__]
    assert hasattr(module, "Http1Protocol")
    assert hasattr(module, "HttpProtocol")
    assert module.HttpProtocol is module.Http1Protocol
    assert selected is module.Http1Protocol

    # The pure reference must also expose both names identically.
    from wreath._pure import server as pure_server

    assert pure_server.HttpProtocol is pure_server.Http1Protocol


def test_default_config_enables_only_http11() -> None:
    assert ServerConfig().protocols == ("http/1.1",)


@pytest.mark.asyncio
async def test_http11_ssl_api_remains_supported() -> None:
    # ssl= remains a supported way to serve HTTP/1.1 over TLS; adding the
    # protocol config and tls= must not reinterpret or remove it.
    cert, key = _make_self_signed()
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    server = await serve(
        make_wreath_app(),
        ServerConfig(host="127.0.0.1", port=0, lifespan="off"),
        ssl=server_ctx,
    )
    try:
        port = _port(server)
        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_ctx)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        resp = await asyncio.wait_for(reader.readuntil(b"hello"), timeout=3.0)
        assert b"HTTP/1.1 200" in resp
        writer.close()
        try:
            await writer.wait_closed()
        except ssl.SSLError:
            pass
    finally:
        await server.close()


def test_protocol_config_rejects_empty_unknown_and_duplicate_values() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        ServerConfig(protocols=())
    with pytest.raises(ValueError, match="unknown protocol"):
        ServerConfig(protocols=("http/2",))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown protocol"):
        ServerConfig(protocols=("h2c",))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="duplicate protocol"):
        ServerConfig(protocols=("h2", "h2"))
    with pytest.raises(ValueError, match="duplicate protocol"):
        ServerConfig(protocols=("http/1.1", "h2", "http/1.1"))
    # A valid ordered set is accepted and preserves order.
    assert ServerConfig(protocols=("h2", "http/1.1")).protocols == ("h2", "http/1.1")


@pytest.mark.asyncio
async def test_requesting_unbuilt_http3_fails_without_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("wreath.server._http3_available", lambda: False)

    cert, key = _make_self_signed()
    tls = TLSConfig(certfile=cert, keyfile=key)

    # Requesting h3 without the extension must raise and must NOT downgrade to a
    # running TCP-only server.
    with pytest.raises(RuntimeError, match="HTTP/3"):
        await serve(
            make_wreath_app(),
            ServerConfig(host="127.0.0.1", port=0, lifespan="off", protocols=("h3",)),
            tls=tls,
        )

    # h3 without any TLS config is a configuration error, not a downgrade.
    with pytest.raises(ValueError, match="TLSConfig"):
        await serve(
            make_wreath_app(),
            ServerConfig(host="127.0.0.1", port=0, lifespan="off", protocols=("h3",)),
        )

    # Passing both ssl= and tls= is rejected.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    with pytest.raises(ValueError, match="not both"):
        await serve(
            make_wreath_app(),
            ServerConfig(host="127.0.0.1", port=0, lifespan="off"),
            ssl=ctx,
            tls=tls,
        )


# --- buffered native ingress over real transports -----------------------------

try:
    from wreath._native import _server as _native_server
except ImportError:  # pragma: no cover - native build always present in CI
    _native_server = None

requires_native = pytest.mark.skipif(
    _native_server is None, reason="native server not built"
)


@requires_native
@pytest.mark.asyncio
async def test_real_tcp_traffic_uses_buffered_ingress() -> None:
    """Production socket traffic must enter through get_buffer()/buffer_updated().

    asyncio selects the buffered receive path via isinstance(protocol,
    BufferedProtocol); with it, data_received() — the copying path — must never
    be called for plain TCP traffic.
    """
    data_received_calls: list[int] = []

    class CountingProtocol(_native_server.Http1Protocol):
        def data_received(self, data: bytes) -> None:
            data_received_calls.append(len(data))
            super().data_received(data)

    loop = asyncio.get_running_loop()
    registry: set[Any] = set()
    server = await loop.create_server(
        lambda: CountingProtocol(minimal_asgi, ServerConfig(), loop, registry),
        "127.0.0.1",
        0,
    )
    try:
        port = server.sockets[0].getsockname()[1]
        resp = await _raw_request(port, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        assert resp.endswith(b"minimal")
        body = b"z" * 4096
        resp = await _raw_request(
            port,
            b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 4096\r\n\r\n" + body,
        )
        assert b"HTTP/1.1 200" in resp
    finally:
        server.close()
        await server.wait_closed()
    assert data_received_calls == []


@pytest.mark.asyncio
async def test_large_post_body_spans_many_buffered_reads() -> None:
    server = await _serve(make_wreath_app())
    try:
        port = _port(server)
        body = bytes(range(256)) * 1024  # 256 KiB > one 64 KiB receive offer
        request = (
            b"POST /echo HTTP/1.1\r\nHost: x\r\nConnection: close\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        resp = await _raw_request(port, request, read_until_close=True)
        assert b"HTTP/1.1 200" in resp
        assert resp.endswith(body)  # nothing truncated when offers fill
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_large_chunked_body_spans_many_buffered_reads() -> None:
    """The chunked path compacts the read buffer too.

    Both body paths slice a memoryview of the read buffer and then consume it;
    consuming compacts once the read cursor passes 64 KiB, and a bytearray
    cannot be resized while a memoryview export of it is alive. Only the pure
    server buffers this way, so this only bites under WREATH_PURE=1.
    """
    server = await _serve(make_wreath_app())
    try:
        port = _port(server)
        piece = bytes(range(256)) * 64  # 16 KiB per chunk
        chunks = 16  # 256 KiB total, well past the 64 KiB compaction threshold
        framed = b"".join(
            b"%x\r\n" % len(piece) + piece + b"\r\n" for _ in range(chunks)
        ) + b"0\r\n\r\n"
        request = (
            b"POST /echo HTTP/1.1\r\nHost: x\r\nConnection: close\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n" + framed
        )
        resp = await _raw_request(port, request, read_until_close=True)
        assert b"HTTP/1.1 200" in resp
        assert resp.endswith(piece * chunks)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_disconnect_during_partial_request_keeps_server_healthy() -> None:
    server = await _serve(make_wreath_app())
    try:
        port = _port(server)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\nabc")
        await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass
        await asyncio.sleep(0.05)
        resp = await _raw_request(port, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b"HTTP/1.1 200" in resp
    finally:
        await server.close()


@requires_native
def test_buffered_ingress_under_uvloop() -> None:
    uvloop = pytest.importorskip("uvloop")

    async def scenario() -> None:
        server = await _serve(make_wreath_app())
        try:
            port = _port(server)
            resp = await _raw_request(port, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            assert resp.endswith(b"hello")
            body = b"u" * 100_000
            request = (
                b"POST /echo HTTP/1.1\r\nHost: x\r\nConnection: close\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            resp = await _raw_request(port, request, read_until_close=True)
            assert resp.endswith(body)
        finally:
            await server.close()

    uvloop.run(scenario())

def test_server_response_header_configuration_validates_values() -> None:
    with pytest.raises(ValueError, match="printable ASCII"):
        ServerConfig(server_header="wreath\ninvalid")
    with pytest.raises(ValueError, match="printable ASCII"):
        ServerConfig(server_header="")
    with pytest.raises(ValueError, match="date_header must be bool"):
        ServerConfig(date_header=1)  # type: ignore[arg-type]


def test_no_unreachable_socket_helper_survives() -> None:
    """`_open_socket` bound and listened, and nothing ever called it.

    Kept as a test rather than deleted-and-forgotten because a helper that
    binds a port is exactly the kind of thing that gets re-added "for the
    multiworker path" and then diverges from the one `Server._start` uses.
    """
    import wreath.server as server_module

    assert not hasattr(server_module, "_open_socket")


def test_server_holds_no_write_only_signal_flag() -> None:
    """`_signal_handlers_installed` was set and never read by anything."""
    import wreath.server as server_module

    async def scenario() -> None:
        server = await serve(_ok_app, ServerConfig(port=0, lifespan="off"))
        try:
            assert not hasattr(server, "_signal_handlers_installed")
        finally:
            await server.close()

    source = server_module.Server.__init__.__code__.co_names
    assert "_signal_handlers_installed" not in source
    asyncio.run(scenario())


async def _ok_app(scope, receive, send) -> None:
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


# --- pre-arming -------------------------------------------------------------
# The first request a process serves costs multiples of the steady state, and on
# a single-threaded loop everything arriving alongside it queues behind that.
# Pre-arming pays it at boot instead. Measured on the metal loop: ~2.1 ms first
# request without it, ~0.5 ms with, for ~2 ms of startup.

@pytest.mark.asyncio
async def test_prearm_is_off_unless_asked_for() -> None:
    server = await _serve(minimal_asgi)
    try:
        assert server.prearmed_connections == 0
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_prearm_warms_the_stack_without_running_the_application() -> None:
    """The safety property, and the reason pre-arm requests ask for a missing route.

    Warming is worth nothing if it costs a handler side effect at every boot --
    a counter incremented, a row written, a webhook sent. Pre-arm requests go to
    a path no route can match, so they exercise ingress, parsing, routing and
    egress and stop at the framework's own 404.
    """
    app = make_wreath_app()
    seen: list[str] = []

    @app.get("/counted")
    async def counted(request: Any) -> str:
        seen.append("hit")
        return "counted"

    server = await _serve(app, prearm=3)
    try:
        assert server.prearmed_connections == 3
        assert seen == [], "a pre-arm request reached an application handler"
        # ... and the warmed server still serves normally.
        resp = await _raw_request(
            _port(server), b"GET /counted HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b"HTTP/1.1 200" in resp
        assert seen == ["hit"]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_the_prearm_path_is_not_routable() -> None:
    from wreath.server import Server

    server = await _serve(make_wreath_app(), prearm=1)
    try:
        resp = await _raw_request(
            _port(server),
            f"GET {Server.PREARM_PATH} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        assert b"HTTP/1.1 404" in resp
    finally:
        await server.close()


def test_prearm_rejects_a_negative_count() -> None:
    with pytest.raises(ValueError, match="prearm"):
        ServerConfig(host="127.0.0.1", port=0, prearm=-1)


def test_protocol_class_is_resolved_once_not_per_connection() -> None:
    """`_protocol_factory` runs per accepted connection, so it must not re-derive.

    `_select_tcp_protocol` reads `os.environ` (two KeyErrors raised and caught
    inside `os._Environ.get`) and calls `importlib.import_module`; it measured
    at 1.82us to recompute a constant, paid on every connection. Resolution is
    still lazy so a `Server` built by hand with an unservable protocol set fails
    where it always did -- what is pinned here is that it happens once.
    """
    import asyncio

    from wreath.server import Server

    async def app(scope, receive, send):  # pragma: no cover - never invoked
        raise AssertionError("no request is served here")

    loop = asyncio.new_event_loop()
    try:
        server = Server(app, ServerConfig(lifespan="off"), loop)
        assert server._protocol_cls is None, "resolution must stay lazy"

        calls = 0
        real = wreath.server._select_tcp_protocol

        def counting(config):
            nonlocal calls
            calls += 1
            return real(config)

        wreath.server._select_tcp_protocol = counting
        try:
            first = server._protocol_factory()
            second = server._protocol_factory()
        finally:
            wreath.server._select_tcp_protocol = real

        assert calls == 1, f"protocol class re-derived {calls} times"
        assert type(first) is type(second)
        assert server._protocol_cls is type(first)
    finally:
        loop.close()
