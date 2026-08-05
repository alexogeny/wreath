"""Helpers shared by the native-reactor spec.

`asyncio_reference` is the oracle: it runs a scenario on a throwaway stock
asyncio loop and returns the observable result, so a native test can assert
"behave exactly like asyncio here" without hard-coding what asyncio does. The
asyncio run is scaffolding inside a red test; it is never a passing row itself.
"""
from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import Callable, Coroutine
from contextlib import closing
from typing import Any


def run(loop: Any, coro: Coroutine) -> Any:
    """run_until_complete on the loop under test."""
    return loop.run_until_complete(coro)


def asyncio_reference(coro_factory: Callable[[asyncio.AbstractEventLoop], Coroutine]) -> Any:
    """Run `coro_factory(loop)` on a fresh stock asyncio loop; return its result."""
    ref = asyncio.new_event_loop()
    try:
        return ref.run_until_complete(coro_factory(ref))
    finally:
        ref.close()


class Recorder:
    """Append-only event log with a stable, comparable representation."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def __call__(self, *item: Any) -> None:
        self.events.append(item if len(item) != 1 else item[0])

    def __eq__(self, other: object) -> bool:
        return self.events == other

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Recorder({self.events!r})"


def socketpair() -> tuple[socket.socket, socket.socket]:
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    return a, b


def tcp_listener() -> tuple[socket.socket, tuple[str, int]]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(128)
    s.setblocking(False)
    return s, s.getsockname()


def free_tcp_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_udp_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_tls_contexts(alpn: list[str] | None = None) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """A server+client SSLContext pair backed by a fresh self-signed cert.

    Requires `cryptography`; callers should `pytest.importorskip` it first.
    """
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

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
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )

    import os
    import tempfile

    fd_c, cert_path = tempfile.mkstemp(suffix=".pem")
    fd_k, key_path = tempfile.mkstemp(suffix=".pem")
    os.write(fd_c, cert_pem)
    os.write(fd_k, key_pem)
    os.close(fd_c)
    os.close(fd_k)

    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.load_verify_locations(cert_path)
    client.check_hostname = True
    if alpn:
        server.set_alpn_protocols(alpn)
        client.set_alpn_protocols(alpn)
    return server, client


# --- tiny ASGI apps used across protocol-integration specs -----------------

def echo_app(status: int = 200):
    async def app(scope, receive, send):
        assert scope["type"] == "http"
        body = b""
        more = True
        while more:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/octet-stream")]})
        await send({"type": "http.response.body", "body": body})

    return app


def sync_ok_app():
    """A handler that never awaits anything that suspends: the inline-drive case."""

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def suspending_app(*, fail: bool = False):
    """A handler that really suspends before it replies.

    Every other app here awaits only already-completed receive/send objects, so
    `spawn_app_task` finishes them on its first `PyIter_Send` and no asyncio
    Task is ever built. This one yields to the loop first, which is the *only*
    way into that function's Task branch -- and it is the branch every handler
    that talks to a database or an upstream takes on every request.

    `fail=True` raises after suspending, to reach the completion path instead.
    """

    async def app(scope, receive, send):
        await asyncio.sleep(0)
        if fail:
            raise RuntimeError("handler failed after suspending")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def reactor_serve(loop, app, protocols=("http/1.1",), config=None):
    """Start the framework's server on the native reactor.

    Pins the acceptance entrypoint `wreath.reactor.serve(...)` (see
    tests/reactor/README.md). Returns an awaitable resolving to a handle with
    `.host`, `.port`, `.udp_port`, and an async `.aclose()`. RED until built.
    """
    try:
        import wreath.reactor as r
    except ImportError as exc:  # not built yet
        raise AssertionError(
            "native reactor not built — needs wreath.reactor.serve()"
        ) from exc
    assert hasattr(r, "serve"), "wreath.reactor.serve() not implemented yet"
    return r.serve(app, host="127.0.0.1", port=0, protocols=tuple(protocols),
                   config=config, loop=loop)
