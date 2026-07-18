"""Mixed-protocol startup, ALPN negotiation, and shutdown for ``wreath.server``.

Covers every supported protocol combination, atomic startup, the fixed shutdown
order, and repeated close. HTTP/2 requires the native extension; HTTP/3 requires
the optional QUIC backend, and those cases skip when unavailable.
"""
from __future__ import annotations

import asyncio
import datetime
import socket
import ssl
import tempfile

import pytest

from wreath.server import ServerConfig, TLSConfig, _http3_available, serve

_native_h2 = False
try:
    import wreath._native._server as _srv

    _native_h2 = hasattr(_srv, "Http2Protocol")
except ImportError:  # pragma: no cover
    _srv = None  # type: ignore[assignment]

requires_h2 = pytest.mark.skipif(not _native_h2, reason="native Http2Protocol not built")
requires_h3 = pytest.mark.skipif(
    not _http3_available(), reason="HTTP/3 backend not built"
)

pytestmark = pytest.mark.asyncio


def _cert() -> tuple[str, str]:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:  # pragma: no cover
        pytest.skip("cryptography not available")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp()
    cp, kp = f"{d}/c.pem", f"{d}/k.pem"
    with open(cp, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kp, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption()))
    return cp, kp


async def _app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


def _tcp_port(server) -> int:
    return server.sockets[0].getsockname()[1]


# --- single-protocol startup ------------------------------------------------

async def test_http11_only_startup_and_shutdown():
    server = await serve(_app, ServerConfig(port=0, lifespan="off"))
    try:
        assert server.sockets
        assert server.datagram_addresses == ()
    finally:
        await server.close()


@requires_h2
async def test_http2_only_startup_and_shutdown():
    cert, key = _cert()
    server = await serve(
        _app, ServerConfig(port=0, lifespan="off", protocols=("h2",)),
        tls=TLSConfig(cert, key))
    try:
        assert server.sockets
        assert server.datagram_addresses == ()
    finally:
        await server.close()


@pytest.mark.network
@requires_h3
async def test_http3_only_startup_and_shutdown():
    cert, key = _cert()
    server = await serve(
        _app, ServerConfig(port=0, lifespan="off", protocols=("h3",)),
        tls=TLSConfig(cert, key))
    try:
        assert server.datagram_addresses
        assert server.sockets == ()
    finally:
        await server.close()


@pytest.mark.network
@requires_h2
@requires_h3
async def test_all_three_protocols_same_port():
    cert, key = _cert()
    server = await serve(
        _app,
        ServerConfig(port=0, lifespan="off", protocols=("http/1.1", "h2", "h3")),
        tls=TLSConfig(cert, key))
    try:
        assert server.sockets
        assert server.datagram_addresses
        # TCP and UDP share the same numeric port.
        assert _tcp_port(server) == server.datagram_addresses[0][1]
    finally:
        await server.close()


# --- ALPN negotiation for combined http/1.1 + h2 ---------------------------

async def _alpn_request(port: int, offer: list[str]) -> tuple[str | None, bytes]:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(offer)
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    ssl_obj = writer.get_extra_info("ssl_object")
    selected = ssl_obj.selected_alpn_protocol()
    body = b""
    if selected in ("http/1.1", None):
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        try:
            body = await asyncio.wait_for(reader.readuntil(b"ok"), timeout=3.0)
        except (asyncio.IncompleteReadError, TimeoutError):
            pass
    writer.close()
    try:
        await writer.wait_closed()
    except ssl.SSLError:
        pass
    return selected, body


@requires_h2
async def test_combined_prefers_h2_when_configured_first():
    cert, key = _cert()
    server = await serve(
        _app, ServerConfig(port=0, lifespan="off", protocols=("h2", "http/1.1")),
        tls=TLSConfig(cert, key))
    try:
        selected, _ = await _alpn_request(_tcp_port(server), ["h2", "http/1.1"])
        assert selected == "h2"
    finally:
        await server.close()


@requires_h2
async def test_combined_prefers_h1_when_configured_first():
    cert, key = _cert()
    server = await serve(
        _app, ServerConfig(port=0, lifespan="off", protocols=("http/1.1", "h2")),
        tls=TLSConfig(cert, key))
    try:
        selected, body = await _alpn_request(_tcp_port(server), ["http/1.1", "h2"])
        assert selected == "http/1.1"
        assert b"200" in body
    finally:
        await server.close()


@requires_h2
async def test_combined_serves_http11_client_without_alpn():
    cert, key = _cert()
    server = await serve(
        _app, ServerConfig(port=0, lifespan="off", protocols=("http/1.1", "h2")),
        tls=TLSConfig(cert, key))
    try:
        selected, body = await _alpn_request(_tcp_port(server), [])
        assert selected is None
        assert b"200" in body  # falls back to HTTP/1.1
    finally:
        await server.close()


# --- configuration / startup errors ----------------------------------------

async def test_h2_without_tls_is_error():
    with pytest.raises(ValueError, match="TLS"):
        await serve(_app, ServerConfig(port=0, lifespan="off", protocols=("h2",)))


async def test_h3_without_tls_is_error():
    with pytest.raises(ValueError, match="TLSConfig"):
        await serve(_app, ServerConfig(port=0, lifespan="off", protocols=("h3",)))


async def test_unbuilt_h3_fails_without_downgrade():
    if _http3_available():
        pytest.skip("HTTP/3 backend is built")
    cert, key = _cert()
    with pytest.raises(RuntimeError, match="HTTP/3"):
        await serve(
            _app, ServerConfig(port=0, lifespan="off", protocols=("h3",)),
            tls=TLSConfig(cert, key))


async def test_atomic_startup_udp_bind_failure_closes_tcp():
    # Occupy a UDP port, then request h3 on that exact port so UDP bind fails;
    # startup must be atomic (no TCP listener left running).
    if not _http3_available():
        pytest.skip("HTTP/3 backend not built")
    cert, key = _cert()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", 0))
    port = blocker.getsockname()[1]
    try:
        with pytest.raises(OSError):
            await serve(
                _app,
                ServerConfig(host="127.0.0.1", port=port, lifespan="off",
                             protocols=("http/1.1", "h3")),
                tls=TLSConfig(cert, key))
        # The TCP listener that was created first must have been torn down, so the
        # port is bindable for TCP now.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()
    finally:
        blocker.close()


async def test_lifespan_startup_failure_aborts():
    async def failing_app(scope, receive, send):
        if scope["type"] == "lifespan":
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.failed",
                            "message": "boom"})
                return

    with pytest.raises(RuntimeError):
        await serve(failing_app, ServerConfig(port=0, lifespan="on"))


# --- shutdown behavior ------------------------------------------------------

async def test_repeated_close_and_wait_closed():
    server = await serve(_app, ServerConfig(port=0, lifespan="off"))
    await server.close()
    await server.close()  # idempotent
    await server.wait_closed()
    await server.wait_closed()


async def test_shutdown_with_idle_connection():
    server = await serve(_app, ServerConfig(port=0, lifespan="off"))
    port = _tcp_port(server)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        # Keep an idle keep-alive connection open, then shut down.
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"ok"), timeout=3.0)
        await server.close()  # must not hang on the idle connection
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass
