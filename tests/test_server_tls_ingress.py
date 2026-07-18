"""HTTP/1.1 over TLS, keep-alive, under concurrent load.

There was no coverage of this at all: every other TLS test makes one request per
connection, so the native `BufferedProtocol` ingress had never been driven with
many requests over several concurrent TLS connections.

What these do **not** cover, so nobody reads more into them than is there: the
server logs one `Fatal error on SSL protocol` per TLS connection at teardown
(`get_buffer() called while a previous read offer is live`), and these tests
pass anyway, because it costs no requests. asyncio's TLS path calls
`buffer_updated()` only when the SSL layer decrypted at least one byte
(`sslproto._do_read__buffered`: `if offset > 0`), so a zero-byte decrypt leaves
the offer unanswered; `http_protocol_releasebuffer` already clears an abandoned
offer when the last view drops, which is why requests still succeed. Reproducing
the log noise needs the re-entrant close path, not load, and these do not
reproduce it.
"""
from __future__ import annotations

import asyncio
import datetime
import ssl
import tempfile

import pytest

from wreath.server import ServerConfig, TLSConfig, serve

pytestmark = pytest.mark.asyncio

CONNECTIONS = 8
PER_CONNECTION = 40


def _cert() -> tuple[str, str]:
    cryptography = pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    del cryptography
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .sign(key, hashes.SHA256()))
    directory = tempfile.mkdtemp()
    cert_path, key_path = f"{directory}/cert.pem", f"{directory}/key.pem"
    with open(cert_path, "wb") as handle:
        handle.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as handle:
        handle.write(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.TraditionalOpenSSL,
                                       serialization.NoEncryption()))
    return cert_path, key_path


async def _app(scope, receive, send) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


def _client_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(["http/1.1"])
    return context


async def _keepalive_requests(port: int, count: int) -> int:
    """`count` sequential requests over one TLS connection. Returns the successes."""
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", port, ssl=_client_context()
    )
    served = 0
    try:
        for _ in range(count):
            writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            if b"200" not in head:
                break
            body = await asyncio.wait_for(reader.readexactly(2), timeout=5.0)
            if body != b"ok":
                break
            served += 1
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ssl.SSLError, OSError):
            pass
    return served


@pytest.mark.network
async def test_http11_over_tls_survives_concurrent_keepalive_load() -> None:
    cert, key = _cert()
    server = await serve(
        _app, ServerConfig(host="127.0.0.1", port=0, lifespan="off"),
        tls=TLSConfig(cert, key),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        served = await asyncio.gather(*[
            _keepalive_requests(port, PER_CONNECTION) for _ in range(CONNECTIONS)
        ])
        expected = [PER_CONNECTION] * CONNECTIONS
        assert list(served) == expected, (
            f"TLS keep-alive served {served}, expected {expected}"
        )
    finally:
        await server.close()


@pytest.mark.network
async def test_a_single_tls_connection_serves_many_requests() -> None:
    cert, key = _cert()
    server = await serve(
        _app, ServerConfig(host="127.0.0.1", port=0, lifespan="off"),
        tls=TLSConfig(cert, key),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        assert await _keepalive_requests(port, 100) == 100
    finally:
        await server.close()
