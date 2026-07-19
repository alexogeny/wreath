"""The metal tier, end to end.

`--loop metal` runs the real native server on the reactor loop with wheel-backed
timers. These drive the actual `wreath.server.Server` over real loopback sockets
and assert it serves HTTP/1.1, HTTP/2 (TLS+ALPN), and HTTP/3 (QUIC) with the
hashed wheel as the timer backend. Unlike the exploratory reactor.serve() specs
these replaced, everything here is the shipped path and passes.
"""
from __future__ import annotations

import asyncio
import datetime
import importlib
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))  # tests/http2 codec


def _metal_loop(timers: str | None = None):
    import os

    import wreath.reactor as r

    if timers is not None:
        prev = os.environ.get("WREATH_METAL_TIMERS")
        os.environ["WREATH_METAL_TIMERS"] = timers
        try:
            return r.metal_event_loop()
        finally:
            if prev is None:
                os.environ.pop("WREATH_METAL_TIMERS", None)
            else:
                os.environ["WREATH_METAL_TIMERS"] = prev
    return r.metal_event_loop()


def test_metal_defaults_to_poller_driven_wheel_without_bridge_heartbeat():
    loop = _metal_loop()
    fired: list[bool] = []
    try:
        assert loop.reactor_timers() == "wheel"
        loop.call_later(0.005, fired.append, True)
        assert loop._wheel_tick_handle is None
        loop.run_until_complete(asyncio.sleep(0.02))
        assert fired == [True]
        assert loop._wheel_tick_handle is None
    finally:
        loop.close()


def _dev_cert() -> tuple[str, str]:
    crypto = pytest.importorskip("cryptography")  # noqa: F841
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cf, cp = tempfile.mkstemp(suffix=".pem")
    kf, kp = tempfile.mkstemp(suffix=".pem")
    os.write(cf, cert.public_bytes(serialization.Encoding.PEM))
    os.close(cf)
    os.write(kf, key.private_bytes(serialization.Encoding.PEM,
             serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    os.close(kf)
    return cp, kp


async def _echo(scope, receive, send):
    while True:
        m = await receive()
        if m["type"] == "http.disconnect":
            return
        if not m.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"metal-" + scope["type"].encode()})


def _serve(loop, protocols, *, ssl_ctx=None, tls=None):
    from wreath.server import Server, ServerConfig

    cfg = ServerConfig(protocols=protocols, host="127.0.0.1", port=0, lifespan="off")
    srv = Server(_echo, cfg, loop)
    loop.run_until_complete(srv._start(ssl=ssl_ctx, tls=tls))
    return srv


def test_native_transport_fuses_http1_ingress_without_python_buffer_callbacks():
    """The metal transport and native HTTP/1 protocol meet through their C API."""
    Http1Protocol = importlib.import_module("wreath._native._server").Http1Protocol
    ServerConfig = importlib.import_module("wreath.server").ServerConfig
    callbacks: list[str] = []

    class ObservedProtocol(Http1Protocol):
        def get_buffer(self, sizehint):
            callbacks.append("get_buffer")
            return super().get_buffer(sizehint)

        def buffer_updated(self, nbytes):
            callbacks.append("buffer_updated")
            return super().buffer_updated(nbytes)

    loop = _metal_loop()
    client, server = socket.socketpair()
    client.setblocking(False)
    try:
        protocol = ObservedProtocol(
            _echo, ServerConfig(lifespan="off"), loop, set()
        )
        transport = loop._make_socket_transport(server, protocol)
        loop.run_until_complete(asyncio.sleep(0))
        assert transport._fused_http1 is True
        client.sendall(b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n")
        loop.run_until_complete(asyncio.sleep(0.01))
        response = client.recv(4096)

        assert b"200 OK" in response
        assert b"metal-http" in response
        assert callbacks == []
        assert transport._direct_read_dispatches >= 1
        assert transport._direct_protocol_writes >= 1
        assert transport._zero_copy_cork_writes >= 1
        transport.close()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        client.close()
        loop.close()


def test_native_transport_uses_vectored_io_for_large_writelines():
    class Protocol(asyncio.Protocol):
        pass

    loop = _metal_loop()
    client, server = socket.socketpair()
    client.setblocking(False)
    try:
        transport = loop._make_socket_transport(server, Protocol())
        loop.run_until_complete(asyncio.sleep(0))
        parts = [b"a" * 32768, b"b" * 32768]
        transport.writelines(parts)
        received = bytearray()
        while len(received) < 65536:
            received += loop.run_until_complete(loop.sock_recv(client, 65536))

        assert received == b"".join(parts)
        assert transport._direct_writelines == 1
        transport.close()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        client.close()
        loop.close()


def test_native_transport_is_collected_after_close():
    """The native SocketTransport must not leak: its self-referential bound
    methods have to be visited by GC so a closed connection is collected."""
    import gc

    loop = _metal_loop()

    def live():
        return sum(1 for o in gc.get_objects()
                   if type(o).__name__ == "SocketTransport")

    class P(asyncio.Protocol):
        def connection_made(self, t):
            pass

        def connection_lost(self, e):
            pass

    try:
        base = live()

        async def churn():
            peers = []
            for _ in range(30):
                a, b = socket.socketpair()
                tr = loop._make_socket_transport(a, P())
                peers.append(b)
                await asyncio.sleep(0)
                tr.close()
            await asyncio.sleep(0.05)
            for b in peers:
                b.close()

        loop.run_until_complete(churn())
        gc.collect()
        assert live() <= base
    finally:
        loop.close()


def test_metal_serves_http1_on_the_wheel():
    loop = _metal_loop(timers="wheel")
    try:
        assert loop.reactor_timers() == "wheel"
        srv = _serve(loop, ("http/1.1",))
        port = srv.sockets[0].getsockname()[1]
        out: list = []

        def client():
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n"
                      b"Connection: close\r\n\r\n")
            data = b""
            while True:
                c = s.recv(4096)
                if not c:
                    break
                data += c
            out.append(data)
            s.close()

        async def drive():
            t = threading.Thread(target=client)
            t.start()
            while t.is_alive():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            await srv.close()

        loop.run_until_complete(drive())
        assert out[0].startswith(b"HTTP/1.1 200")
        assert out[0].endswith(b"metal-http")
        assert loop.reactor_timers() == "wheel"  # deadlines rode the native wheel
    finally:
        loop.close()


def test_metal_serves_http2_over_tls():
    pytest.importorskip("cryptography")
    from http2 import support as h2  # type: ignore

    from wreath.server import TLSConfig

    cp, kp = _dev_cert()
    loop = _metal_loop()
    try:
        ssl_ctx = TLSConfig(certfile=cp, keyfile=kp).build_ssl_context(("h2",))
        srv = _serve(loop, ("h2",), ssl_ctx=ssl_ctx)
        port = srv.sockets[0].getsockname()[1]
        out: dict = {}

        def client():
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.set_alpn_protocols(["h2"])
            s = ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5),
                                server_hostname="localhost")
            out["alpn"] = s.selected_alpn_protocol()
            s.sendall(h2.PREFACE + h2.encode_settings({}))
            s.sendall(h2.build_headers_frame(1, h2.request_headers(), end_stream=True))
            data = b""
            s.settimeout(3.0)
            try:
                while b"metal-http" not in data:
                    c = s.recv(4096)
                    if not c:
                        break
                    data += c
            except OSError:
                pass
            out["raw"] = data
            s.close()

        async def drive():
            t = threading.Thread(target=client)
            t.start()
            while t.is_alive():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            await srv.close()

        loop.run_until_complete(drive())
        assert out["alpn"] == "h2"
        parser = h2.FrameParser()
        parser.feed(out["raw"])
        assert any(f.type == h2.DATA and b"metal-http" in f.payload for f in parser.frames())
    finally:
        loop.close()
        os.unlink(cp)
        os.unlink(kp)


def _curl_has_http3() -> bool:
    curl = shutil.which("curl")
    if not curl:
        return False
    try:
        out = subprocess.run([curl, "--version"], capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    return "http3" in out.stdout.lower()


def test_metal_serves_http3_over_quic():
    pytest.importorskip("cryptography")
    import importlib.util

    if importlib.util.find_spec("wreath._native._http3") is None:
        pytest.skip("native HTTP/3 extension not built")
    if not _curl_has_http3():
        pytest.skip("curl without HTTP/3 support")

    from wreath.server import TLSConfig

    cp, kp = _dev_cert()
    loop = _metal_loop()
    try:
        srv = _serve(loop, ("h3",), tls=TLSConfig(certfile=cp, keyfile=kp))
        udp_port = srv.datagram_addresses[0][1]
        out: dict = {}

        def client():
            out["proc"] = subprocess.run(
                ["curl", "--http3-only", "-sk", f"https://127.0.0.1:{udp_port}/"],
                capture_output=True, timeout=15)

        async def drive():
            t = threading.Thread(target=client)
            t.start()
            while t.is_alive():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            await srv.close()

        loop.run_until_complete(drive())
        proc = out["proc"]
        assert proc.returncode == 0
        assert b"metal-http" in proc.stdout
    finally:
        loop.close()
        os.unlink(cp)
        os.unlink(kp)
