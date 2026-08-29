from __future__ import annotations

import asyncio
import datetime
import ssl
import tempfile

import pytest

from wreath.server import ServerConfig, TLSConfig, serve

from . import support
from .conftest import requires_h2

pytestmark = [requires_h2, pytest.mark.asyncio, pytest.mark.network]


def _self_signed() -> tuple[str, str]:
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
    cert_path, key_path = f"{tmp}/cert.pem", f"{tmp}/key.pem"
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


def _port(server) -> int:
    return server.sockets[0].getsockname()[1]


async def _serve_h2():
    cert, key = _self_signed()
    tls = TLSConfig(certfile=cert, keyfile=key)
    server = await serve(
        _echo_app,
        ServerConfig(host="127.0.0.1", port=0, lifespan="off", protocols=("h2",)),
        tls=tls,
    )
    return server


async def _echo_app(scope, receive, send):
    assert scope["http_version"] == "2"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"h2-ok"})


async def test_alpn_negotiates_h2(make_driver=None):
    server = await _serve_h2()
    try:
        port = _port(server)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_alpn_protocols(["h2"])
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
        ssl_obj = writer.get_extra_info("ssl_object")
        assert ssl_obj.selected_alpn_protocol() == "h2"

        writer.write(support.PREFACE)
        writer.write(support.encode_settings({}))
        writer.write(support.build_headers_frame(1, support.request_headers()))
        await writer.drain()

        parser = support.FrameParser()
        deadline = asyncio.get_event_loop().time() + 3.0
        body = b""
        status = None
        dec = support.HpackDecoder()
        while asyncio.get_event_loop().time() < deadline:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=3.0)
            if not chunk:
                break
            parser.feed(chunk)
            done = False
            for frame in parser.frames():
                if frame.type == support.HEADERS and frame.stream_id == 1:
                    status = dict(dec.decode(frame.payload)).get(b":status")
                elif frame.type == support.DATA and frame.stream_id == 1:
                    body += frame.payload
                    if frame.flags & support.FLAG_END_STREAM:
                        done = True
            if done:
                break
        assert status == b"200"
        assert body == b"h2-ok"
        writer.close()
        try:
            await writer.wait_closed()
        except ssl.SSLError:
            pass
    finally:
        await server.close()


async def test_alpn_mismatch_is_rejected():
    # A client offering only http/1.1 to an h2-only server must not get an h2
    # connection; the handshake either fails ALPN or the server closes.
    server = await _serve_h2()
    try:
        port = _port(server)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_alpn_protocols(["http/1.1"])
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
        except ssl.SSLError:
            return  # ALPN failure at handshake is acceptable
        ssl_obj = writer.get_extra_info("ssl_object")
        assert ssl_obj.selected_alpn_protocol() in (None, "http/1.1")
        writer.close()
        try:
            await writer.wait_closed()
        except ssl.SSLError:
            pass
    finally:
        await server.close()
