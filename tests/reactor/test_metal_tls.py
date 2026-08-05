"""TLS on the metal tier, terminated in C rather than in `asyncio.sslproto`.

RED until the reactor grows a native TLS transport.

**Why this is reactor work and not `wreath.edge` work.** `EventLoop._start_serving`
takes the native io_uring path only when `sslcontext is None`; a TLS listener
falls all the way back to stock asyncio, which means asyncio's selector accept
loop, asyncio's `_SelectorSocketTransport`, and `asyncio.sslproto.SSLProtocol` --
a *Python object in the data path for every read and every write*. A TLS
connection therefore does not merely lose the crypto to Python, it leaves the
metal tier entirely.

Measured on one machine, one physical core each, small JSON response, handshakes
fully amortised (64 connections, ~106k requests, so this is record framing and
symmetric crypto, not key exchange):

    wreath server, plaintext      74,300 req/s
    wreath server, TLS            21,300 req/s     3.49x tax
    nginx, plaintext              71,400 req/s
    nginx, TLS                    47,400 req/s     1.50x tax

Plaintext, wreath is 4% ahead of nginx. With TLS it is 2.23x behind, and the
whole of that difference is the fallback above. Since TLS termination is the
primary job of an edge proxy, this is the single measurement that decides
whether `wreath.edge` can face the internet at all.

The gain is not proxy-only, which is why it belongs here: `wreath.server` and
`wreath.http_client` take the same fallback for the same reason.
"""

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
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cf, cp = tempfile.mkstemp(suffix=".pem")
    kf, kp = tempfile.mkstemp(suffix=".pem")
    os.write(cf, cert.public_bytes(serialization.Encoding.PEM))
    os.close(cf)
    os.write(kf, key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
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
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n"
                      b"Connection: close\r\n\r\n")
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
    """A TLS request is served, and the bytes are right.

    The correctness half. If this passes and the next one fails, TLS works but
    is still going through Python -- which is the state this file exists to
    leave.
    """
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
    """The point of the exercise: no `asyncio.sslproto` in the data path.

    Asserted on the transport the protocol is handed, because that is the object
    every read and write goes through. `asyncio.sslproto._SSLProtocolTransport`
    is a Python class whose `write()` runs Python per call; the reactor's
    `SocketTransport` is C with a `writelines` that never returns to the
    interpreter. Naming the module rather than timing anything keeps this a
    contract instead of a benchmark that flakes on a loaded machine.
    """
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
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                    b"Connection: close\r\n\r\nok")
                self.transport.close()

    try:
        server = loop.run_until_complete(loop.create_server(
            Recording, "127.0.0.1", 0, ssl=_native_tls_context(cp, kp)))
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
    """The native TLS path must not disturb the path that already worked.

    Cheap, and it is the regression this change is most likely to cause: the
    TLS branch lives inside the same transport as plaintext.
    """
    loop = _metal_loop()
    try:
        srv = _serve(loop, ("http/1.1",))
        port = srv.sockets[0].getsockname()[1]
        out: list = []

        def client():
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n"
                          b"Connection: close\r\n\r\n")
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
    """Named, at startup, in front of a developer -- not on the first handshake.

    A listener that binds and then fails every connection is the shape this tree
    refuses everywhere else; a TLS context is exactly the place it is tempting,
    because OpenSSL is perfectly happy to defer the error.
    """
    import wreath.reactor as reactor

    assert hasattr(reactor, "metal_tls_context"), (
        "wreath.reactor.metal_tls_context() is not implemented yet."
    )
    with pytest.raises((OSError, ValueError, ssl.SSLError)):
        reactor.metal_tls_context(certfile="/nonexistent/cert.pem",
                                  keyfile="/nonexistent/key.pem")


def test_a_plain_ssl_context_still_takes_the_asyncio_fallback() -> None:
    """The other half of the assertion above, and the proof it can tell them apart.

    A stock `ssl.SSLContext` carries no native handle -- there is no supported
    way to borrow an `SSL_CTX *` from one -- so it must keep working exactly as
    before rather than being silently refused. This test is also what stops the
    native-transport assertion from being vacuous: if it passed for *every*
    context, it would be measuring nothing.
    """
    cp, kp = _dev_cert()
    loop = _metal_loop()
    seen: list[str] = []

    class Recording(asyncio.Protocol):
        def connection_made(self, transport):
            seen.append(type(transport).__module__)
            self.transport = transport

        def data_received(self, data):
            if data.endswith(b"\r\n\r\n"):
                self.transport.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                                     b"Connection: close\r\n\r\nok")
                self.transport.close()

    plain = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    plain.load_cert_chain(cp, kp)
    plain.set_alpn_protocols(["http/1.1"])
    try:
        server = loop.run_until_complete(
            loop.create_server(Recording, "127.0.0.1", 0, ssl=plain))
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
            f"expected the asyncio fallback for a plain context, got {seen}")
    finally:
        loop.close()


# --- client side ---------------------------------------------------------


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
    """`SSL_connect` in C, so an https call does not leave the metal tier either.

    The inbound half alone leaves a proxy half-native: it terminates TLS to the
    client in C and then pays Python per read to talk to the origin. This is
    also not proxy-only -- it is every outbound call `wreath.http_client` makes.
    """
    cp, kp = _dev_cert()
    loop = _metal_loop()
    seen: list[str] = []
    body: list[bytes] = []

    class Server(asyncio.Protocol):
        def connection_made(self, transport):
            self.transport = transport

        def data_received(self, data):
            if data.endswith(b"\r\n\r\n"):
                self.transport.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n"
                                     b"Connection: close\r\n\r\nhello")
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
                Server, "127.0.0.1", 0, ssl=_native_tls_context(cp, kp))
            port = server.sockets[0].getsockname()[1]
            done = loop.create_future()
            # verify=False: the certificate is self-signed for this test. The
            # point under test is which transport carries the bytes, not the
            # trust decision -- that has its own test below.
            await loop.create_connection(
                lambda: Client(done), "127.0.0.1", port,
                ssl=_client_tls_context(verify=False),
                server_hostname="localhost")
            await asyncio.wait_for(done, 10)
            server.close()
            await server.wait_closed()

        loop.run_until_complete(run())
        assert body and body[0].endswith(b"hello"), body[:1]
        assert seen and all(m.startswith("wreath._native._reactor") for m in seen), (
            f"outbound TLS is not on the native transport: {sorted(set(seen))}")
    finally:
        loop.close()


def test_an_untrusted_certificate_is_refused() -> None:
    """Verification is on by default, and it actually rejects.

    The failure mode worth pinning: a native client that silently skips the
    trust check is faster than one that does not, and looks identical until it
    matters. Asserted against the same self-signed certificate the test above
    deliberately accepts, so the two differ only in the flag.
    """
    cp, kp = _dev_cert()
    loop = _metal_loop()

    class Server(asyncio.Protocol):
        def connection_made(self, transport):
            self.transport = transport

    try:
        async def run():
            server = await loop.create_server(
                Server, "127.0.0.1", 0, ssl=_native_tls_context(cp, kp))
            port = server.sockets[0].getsockname()[1]
            try:
                with pytest.raises((ssl.SSLError, ssl.SSLCertVerificationError,
                                    ConnectionError, OSError)):
                    await loop.create_connection(
                        asyncio.Protocol, "127.0.0.1", port,
                        ssl=_client_tls_context(),
                        server_hostname="localhost")
            finally:
                server.close()
                await server.wait_closed()

        loop.run_until_complete(run())
    finally:
        loop.close()


def test_an_https_call_from_http_client_keeps_the_native_transport() -> None:
    """The outbound half, delivered rather than merely available.

    The transport supporting `SSL_connect` buys nothing until the client asks
    for it: `HTTPClient` built a plain `ssl.SSLContext`, which carries no native
    handle, so every https call stayed on `asyncio.sslproto` even with the C
    path sitting right there. This is the test that the wiring exists.

    A trust store has to be nameable for that to be possible at all. A built
    `SSLContext` will not give up its material, so a client handed one keeps the
    asyncio path by necessity -- `ClientTLS` names the *paths* instead, the same
    answer the server side and HTTP/3 already use.
    """
    from wreath.http_client import ClientTLS, DestinationPolicy, HTTPClient

    cp, kp = _dev_cert()
    loop = _metal_loop()
    seen: list[str] = []

    class Server(asyncio.Protocol):
        def connection_made(self, transport):
            self.transport = transport

        def data_received(self, data):
            if data.endswith(b"\r\n\r\n"):
                self.transport.write(b"HTTP/1.1 200 OK\r\ncontent-length: 5\r\n"
                                     b"\r\nhello")

    try:
        async def run():
            server = await loop.create_server(
                Server, "127.0.0.1", 0, ssl=_native_tls_context(cp, kp))
            port = server.sockets[0].getsockname()[1]

            original = loop.create_connection

            async def recording(*args, **kwargs):
                transport, protocol = await original(*args, **kwargs)
                seen.append(type(transport).__module__)
                return transport, protocol

            loop.create_connection = recording
            client = HTTPClient(
                "probe", base_url=f"https://localhost:{port}",
                tls=ClientTLS(cafile=cp),
                # The origin is on loopback; the SSRF guard denies that by
                # default and this test is not about the guard.
                destination=DestinationPolicy(allow_loopback=True))
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
            f"HTTPClient's https path is not native: {sorted(set(seen))}")
    finally:
        loop.close()


def test_an_ip_literal_is_checked_against_the_address_san() -> None:
    """`https://10.0.0.4:8443` has to work, and has to still verify.

    An upstream pool is usually written as addresses. A client that treats one
    as a DNS name looks for it among the certificate's DNS entries, where it
    cannot be, and refuses every correctly-issued certificate it could have been
    handed -- while a client that quietly stops checking instead is worse. The
    address goes to the iPAddress SAN, and SNI is not sent at all, which RFC
    6066 forbids for a literal.
    """
    cp, kp = _dev_cert_for_ip()
    loop = _metal_loop()
    body: list[bytes] = []

    class Server(asyncio.Protocol):
        def connection_made(self, transport):
            self.transport = transport

        def data_received(self, data):
            if data.endswith(b"\r\n\r\n"):
                self.transport.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                                     b"Connection: close\r\n\r\nok")
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
                Server, "127.0.0.1", 0, ssl=_native_tls_context(cp, kp))
            port = server.sockets[0].getsockname()[1]
            done = loop.create_future()
            await loop.create_connection(
                lambda: Client(done), "127.0.0.1", port,
                ssl=_client_tls_context(cafile=cp),
                server_hostname="127.0.0.1")
            await asyncio.wait_for(done, 10)
            server.close()
            await server.wait_closed()

        loop.run_until_complete(run())
        assert body and body[0].endswith(b"ok"), body[:1]
    finally:
        loop.close()


def test_a_certificate_for_a_different_address_is_refused() -> None:
    """The falsification for the test above: the IP check has to reject too.

    Routing an address to the iPAddress SAN would be worthless if it then
    matched anything, and "verification quietly stopped happening" is precisely
    the failure that looks like success. Same certificate, same connection, one
    digit different in the name being claimed.
    """
    cp, kp = _dev_cert_for_ip()          # SAN is IP:127.0.0.1
    loop = _metal_loop()

    try:
        async def run():
            server = await loop.create_server(
                asyncio.Protocol, "127.0.0.1", 0,
                ssl=_native_tls_context(cp, kp))
            port = server.sockets[0].getsockname()[1]
            try:
                with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
                    await loop.create_connection(
                        asyncio.Protocol, "127.0.0.1", port,
                        ssl=_client_tls_context(cafile=cp),
                        server_hostname="127.0.0.2")
            finally:
                server.close()
                await server.wait_closed()

        loop.run_until_complete(run())
    finally:
        loop.close()
