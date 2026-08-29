from __future__ import annotations

import asyncio
import socket
import ssl
import threading

import pytest

from tests._metal import requires_metal

from .test_metal_tier import _dev_cert, _metal_loop, _serve

pytestmark = requires_metal


def _native_tls_context(certfile: str, keyfile: str):
    """The server context under test, or a red failure naming what is missing.

    Kept as a lookup rather than an import so the failure says what to build
    instead of an `ImportError` at collection.
    """
    import wreath.reactor as reactor

    assert hasattr(reactor, "metal_tls_context"), (
        "wreath.reactor.metal_tls_context() is not implemented yet: the native "
        "TLS transport in src/wreath/_native/ that terminates TLS in C, so a "
        "TLS listener keeps the metal tier instead of falling back to "
        "asyncio.sslproto. This spec stays RED until then."
    )
    return reactor.metal_tls_context(certfile=certfile, keyfile=keyfile)


def _dev_cert_for_ip() -> tuple[str, str]:
    """A certificate whose only SAN is an IP address, for the literal case."""
    pytest.importorskip("cryptography")
    import datetime
    import ipaddress
    import os
    import tempfile

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cf, cp = tempfile.mkstemp(suffix=".pem")
    kf, kp = tempfile.mkstemp(suffix=".pem")
    os.write(cf, cert.public_bytes(serialization.Encoding.PEM))
    os.close(cf)
    os.write(
        kf,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )
    os.close(kf)
    return cp, kp


def _client_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def _request(port: int, out: list) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with _client_context().wrap_socket(raw, server_hostname="localhost") as s:
            s.sendall(
                b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
    out.append(data)


def _drive(loop, srv, port, out) -> None:
    async def run():
        t = threading.Thread(target=_request, args=(port, out))
        t.start()
        while t.is_alive():  # noqa: ASYNC110 -- the client is a thread, not a task
            await asyncio.sleep(0.02)
        await srv.close()

    loop.run_until_complete(run())


def test_metal_serves_http1_over_native_tls() -> None:
    cp, kp = _dev_cert()
    loop = _metal_loop()
    try:
        srv = _serve(loop, ("http/1.1",), ssl_ctx=_native_tls_context(cp, kp))
        port = srv.sockets[0].getsockname()[1]
        out: list = []
        _drive(loop, srv, port, out)
        assert out[0].startswith(b"HTTP/1.1 200"), out[0][:120]
        assert out[0].endswith(b"metal-http"), out[0][-40:]
    finally:
        loop.close()


def test_a_tls_connection_keeps_the_native_transport() -> None:
    cp, kp = _dev_cert()
    loop = _metal_loop()
    seen: list[str] = []

    class Recording(asyncio.Protocol):
        """Records what it was handed, then answers so the client can finish."""

        def connection_made(self, transport):
            seen.append(type(transport).__module__ + "." + type(transport).__name__)
            self.transport = transport

        def data_received(self, data):
            if data.endswith(b"\r\n\r\n"):
                self.transport.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                )
                self.transport.close()

    try:
        server = loop.run_until_complete(
            loop.create_server(Recording, "127.0.0.1", 0, ssl=_native_tls_context(cp, kp))
        )
        port = server.sockets[0].getsockname()[1]
        out: list = []

        async def run():
            t = threading.Thread(target=_request, args=(port, out))
            t.start()
            while t.is_alive():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            server.close()
            await server.wait_closed()

        loop.run_until_complete(run())
        assert out and out[0].startswith(b"HTTP/1.1 200"), out[:1]
        assert seen, "connection_made never ran"
        assert all(m.startswith("wreath._native._reactor") for m in seen), (
            f"TLS connection is not on the native transport: {sorted(set(seen))}. "
            "asyncio.sslproto means a Python object per read and per write."
        )
    finally:
        loop.close()


def test_a_plaintext_listener_is_unaffected() -> None:
    loop = _metal_loop()
    try:
        srv = _serve(loop, ("http/1.1",))
        port = srv.sockets[0].getsockname()[1]
        out: list = []

        def client():
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                s.sendall(
                    b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                data = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            out.append(data)

        async def run():
            t = threading.Thread(target=client)
            t.start()
            while t.is_alive():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            await srv.close()

        loop.run_until_complete(run())
        assert out[0].startswith(b"HTTP/1.1 200"), out[0][:120]
    finally:
        loop.close()


def test_the_context_refuses_a_missing_certificate_at_configuration_time() -> None:
    import wreath.reactor as reactor

    assert hasattr(reactor, "metal_tls_context"), (
        "wreath.reactor.metal_tls_context() is not implemented yet."
    )
    with pytest.raises((OSError, ValueError, ssl.SSLError)):
        reactor.metal_tls_context(certfile="/nonexistent/cert.pem", keyfile="/nonexistent/key.pem")


def test_a_plain_ssl_context_still_takes_the_asyncio_fallback() -> None:
    cp, kp = _dev_cert()
    loop = _metal_loop()
    seen: list[str] = []

    class Recording(asyncio.Protocol):
        def connection_made(self, transport):
            seen.append(type(transport).__module__)
            self.transport = transport

        def data_received(self, data):
            if data.endswith(b"\r\n\r\n"):
                self.transport.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                )
                self.transport.close()

    plain = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    plain.load_cert_chain(cp, kp)
    plain.set_alpn_protocols(["http/1.1"])
    try:
        server = loop.run_until_complete(loop.create_server(Recording, "127.0.0.1", 0, ssl=plain))
        port = server.sockets[0].getsockname()[1]
        out: list = []

        async def run():
            t = threading.Thread(target=_request, args=(port, out))
            t.start()
            while t.is_alive():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            server.close()
            await server.wait_closed()

        loop.run_until_complete(run())
        assert out and out[0].startswith(b"HTTP/1.1 200"), out[:1]
        assert seen and all(m.startswith("asyncio") for m in seen), (
            f"expected the asyncio fallback for a plain context, got {seen}"
        )
    finally:
        loop.close()


def _client_tls_context(**kwargs):
    """The outbound context under test, or a red failure naming what is missing."""
    import wreath.reactor as reactor

    assert hasattr(reactor, "metal_tls_client_context"), (
        "wreath.reactor.metal_tls_client_context() is not implemented yet: the "
        "outbound half of the native TLS transport. Only SSL_accept is wired, "
        "so every https:// call still takes the asyncio.sslproto path."
    )
    return reactor.metal_tls_client_context(**kwargs)


def test_an_outbound_tls_connection_keeps_the_native_transport() -> None:
    cp, kp = _dev_cert()
    loop = _metal_loop()
    seen: list[str] = []
    body: list[bytes] = []

    class Server(asyncio.Protocol):
        def connection_made(self, transport):
            self.transport = transport

        def data_received(self, data):
            if data.endswith(b"\r\n\r\n"):
                self.transport.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"
                )
                self.transport.close()

    class Client(asyncio.Protocol):
        def __init__(self, done):
            self.done = done
            self.buf = b""

        def connection_made(self, transport):
            seen.append(type(transport).__module__)
            transport.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")

        def data_received(self, data):
            self.buf += data

        def connection_lost(self, exc):
            body.append(self.buf)
            if not self.done.done():
                self.done.set_result(None)

    try:

        async def run():
            server = await loop.create_server(
                Server, "127.0.0.1", 0, ssl=_native_tls_context(cp, kp)
            )
            port = server.sockets[0].getsockname()[1]
            done = loop.create_future()
            # verify=False: the certificate is self-signed for this test. The
            # point under test is which transport carries the bytes, not the
            # trust decision -- that has its own test below.
            await loop.create_connection(
                lambda: Client(done),
                "127.0.0.1",
                port,
                ssl=_client_tls_context(verify=False),
                server_hostname="localhost",
            )
            await asyncio.wait_for(done, 10)
            server.close()
            await server.wait_closed()

        loop.run_until_complete(run())
        assert body and body[0].endswith(b"hello"), body[:1]
        assert seen and all(m.startswith("wreath._native._reactor") for m in seen), (
            f"outbound TLS is not on the native transport: {sorted(set(seen))}"
        )
    finally:
        loop.close()


def test_an_untrusted_certificate_is_refused() -> None:
    cp, kp = _dev_cert()
    loop = _metal_loop()

    class Server(asyncio.Protocol):
        def connection_made(self, transport):
            self.transport = transport

    try:

        async def run():
            server = await loop.create_server(
                Server, "127.0.0.1", 0, ssl=_native_tls_context(cp, kp)
            )
            port = server.sockets[0].getsockname()[1]
            try:
                with pytest.raises(
                    (ssl.SSLError, ssl.SSLCertVerificationError, ConnectionError, OSError)
                ):
                    await loop.create_connection(
                        asyncio.Protocol,
                        "127.0.0.1",
                        port,
                        ssl=_client_tls_context(),
                        server_hostname="localhost",
                    )
            finally:
                server.close()
                await server.wait_closed()

        loop.run_until_complete(run())
    finally:
        loop.close()


def test_an_https_call_from_http_client_keeps_the_native_transport() -> None:
    from wreath.http_client import ClientTLS, DestinationPolicy, HTTPClient

    cp, kp = _dev_cert()
    loop = _metal_loop()
    seen: list[str] = []

    class Server(asyncio.Protocol):
        def connection_made(self, transport):
            self.transport = transport

        def data_received(self, data):
            if data.endswith(b"\r\n\r\n"):
                self.transport.write(b"HTTP/1.1 200 OK\r\ncontent-length: 5\r\n\r\nhello")

    try:

        async def run():
            server = await loop.create_server(
                Server, "127.0.0.1", 0, ssl=_native_tls_context(cp, kp)
            )
            port = server.sockets[0].getsockname()[1]

            original = loop.create_connection

            async def recording(*args, **kwargs):
                transport, protocol = await original(*args, **kwargs)
                seen.append(type(transport).__module__)
                return transport, protocol

            loop.create_connection = recording
            client = HTTPClient(
                "probe",
                base_url=f"https://localhost:{port}",
                tls=ClientTLS(cafile=cp),
                # The origin is on loopback; the SSRF guard denies that by
                # default and this test is not about the guard.
                destination=DestinationPolicy(allow_loopback=True),
            )
            await client.start()
            try:
                response = await client.get("/")
                assert response.status == 200, response.status
                assert response.body == b"hello", response.body
            finally:
                await client.close()
                loop.create_connection = original
                server.close()
                await server.wait_closed()

        loop.run_until_complete(run())
        assert seen, "no outbound connection was made"
        assert all(m.startswith("wreath._native._reactor") for m in seen), (
            f"HTTPClient's https path is not native: {sorted(set(seen))}"
        )
    finally:
        loop.close()


def test_an_ip_literal_is_checked_against_the_address_san() -> None:
    cp, kp = _dev_cert_for_ip()
    loop = _metal_loop()
    body: list[bytes] = []

    class Server(asyncio.Protocol):
        def connection_made(self, transport):
            self.transport = transport

        def data_received(self, data):
            if data.endswith(b"\r\n\r\n"):
                self.transport.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                )
                self.transport.close()

    class Client(asyncio.Protocol):
        def __init__(self, done):
            self.done, self.buf = done, b""

        def connection_made(self, transport):
            transport.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")

        def data_received(self, data):
            self.buf += data

        def connection_lost(self, exc):
            body.append(self.buf)
            if not self.done.done():
                self.done.set_result(None)

    try:

        async def run():
            server = await loop.create_server(
                Server, "127.0.0.1", 0, ssl=_native_tls_context(cp, kp)
            )
            port = server.sockets[0].getsockname()[1]
            done = loop.create_future()
            await loop.create_connection(
                lambda: Client(done),
                "127.0.0.1",
                port,
                ssl=_client_tls_context(cafile=cp),
                server_hostname="127.0.0.1",
            )
            await asyncio.wait_for(done, 10)
            server.close()
            await server.wait_closed()

        loop.run_until_complete(run())
        assert body and body[0].endswith(b"ok"), body[:1]
    finally:
        loop.close()


def test_a_certificate_for_a_different_address_is_refused() -> None:
    cp, kp = _dev_cert_for_ip()  # SAN is IP:127.0.0.1
    loop = _metal_loop()

    try:

        async def run():
            server = await loop.create_server(
                asyncio.Protocol, "127.0.0.1", 0, ssl=_native_tls_context(cp, kp)
            )
            port = server.sockets[0].getsockname()[1]
            try:
                with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
                    await loop.create_connection(
                        asyncio.Protocol,
                        "127.0.0.1",
                        port,
                        ssl=_client_tls_context(cafile=cp),
                        server_hostname="127.0.0.2",
                    )
            finally:
                server.close()
                await server.wait_closed()

        loop.run_until_complete(run())
    finally:
        loop.close()
