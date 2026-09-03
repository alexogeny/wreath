from __future__ import annotations

import asyncio
import importlib
import os
import socket
from typing import cast

import pytest

from wreath._native import _reactor
from wreath.edge import Upstream, UpstreamPool
from wreath.edge.serve import EdgeHandle, _endpoint

_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
_SLOT = int("".join(c for c in _WORKER if c.isdigit()) or 0)
#: Below the ephemeral range (`ip_local_port_range` starts at 32768), because a
#: proxy makes an outbound connection per request and the kernel draws its
#: source ports from there -- a listener inside that range can lose its own bind.
_PORT = 27800 + _SLOT * 40


@pytest.mark.parametrize(
    ("url", "endpoint"),
    [
        ("http://example.test", ("example.test", 80, b"example.test", False)),
        ("https://example.test", ("example.test", 443, b"example.test", True)),
        ("http://example.test:8080", ("example.test", 8080, b"example.test:8080", False)),
        ("https://example.test:8443", ("example.test", 8443, b"example.test:8443", True)),
    ],
)
def test_native_endpoint_preserves_explicit_ports_and_scheme_defaults(
    url: str, endpoint: tuple[str, int, bytes, bool]
) -> None:
    assert _endpoint(url) == endpoint


@pytest.mark.parametrize(
    "url",
    [
        "http://user:secret@example.test",
        "http://example.test/api",
        "http://example.test?tenant=acme",
        "http://example.test#origin",
    ],
)
async def test_native_serve_refuses_upstream_urls_that_are_not_origins(url: str) -> None:
    with pytest.raises(ValueError, match="origin URL"):
        await _serve()(UpstreamPool([Upstream(url)]), port=0)


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("connections", 0, "connections must be at least 1"),
        ("max_body", -1, "max_body must be non-negative"),
        ("backlog", 0, "backlog must be at least 1"),
        ("queue_timeout", float("nan"), "queue_timeout must be finite"),
        ("queue_timeout", float("inf"), "queue_timeout must be finite"),
    ],
)
async def test_native_serve_refuses_invalid_resource_limits(
    option: str, value: int | float, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await _serve()(
            UpstreamPool([Upstream("http://127.0.0.1:1")]),
            port=0,
            **{option: value},
        )


async def test_native_serve_closes_the_table_when_prewarming_fails(monkeypatch) -> None:
    module = importlib.import_module("wreath.edge.serve")
    tables = []

    class Table:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False
            tables.append(self)

        def close(self) -> None:
            self.closed = True

    class Loop:
        async def create_connection(self, *args, **kwargs):
            raise OSError("upstream refused")

    monkeypatch.setattr(module._edge, "UpstreamTable", Table)
    monkeypatch.setattr(module.asyncio, "get_running_loop", lambda: Loop())

    with pytest.raises(OSError, match="upstream refused"):
        await module.serve(
            UpstreamPool([Upstream("http://127.0.0.1:1")]),
            connections=1,
        )

    assert len(tables) == 1 and tables[0].closed


def _next_port() -> int:
    """The next port in this worker's band that will actually bind.

    Probing rather than counting, because the counter is deterministic: the same
    worker asks for the same port on every run, and a socket left in `TIME_WAIT`
    by the run before makes that a flake that reproduces about one run in three.
    The probe uses the options `create_server` uses, so a port it accepts is one
    asyncio will accept.
    """
    global _PORT
    while True:
        _PORT += 1
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", _PORT))
        except OSError:
            continue
        finally:
            probe.close()
        return _PORT


def _self_signed() -> tuple[str, str]:
    """A throwaway certificate and key on disk, for the TLS cases."""
    pytest.importorskip("cryptography")
    import datetime
    import tempfile

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


def _serve():
    """The acceptance entrypoint, or a red failure naming what is missing."""
    import wreath.edge as edge

    assert hasattr(edge, "serve"), (
        "wreath.edge.serve() is not implemented yet: the native proxy protocol "
        "in src/wreath/_native/ that forwards a request without entering "
        "Python. This spec line stays RED until then."
    )
    return edge.serve


class _CloseServer:
    sockets = ()

    def __init__(self) -> None:
        self.closed = False
        self.waited = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


class _CloseTable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


async def test_close_cancels_and_joins_pending_upstream_reopens() -> None:
    server = _CloseServer()
    table = _CloseTable()
    task = asyncio.create_task(asyncio.sleep(60))
    tasks = {task}
    handle = EdgeHandle(cast(asyncio.Server, server), table, [], 0, tasks)

    await handle.aclose()

    assert task.cancelled()
    assert tasks == set()
    assert table.closed
    assert server.closed and server.waited


class _Origin:
    """Records the head it received and answers a fixed 200."""

    def __init__(self) -> None:
        self.heads: list[bytes] = []
        self._server: asyncio.Server | None = None

    async def start(self, port: int) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)

    async def _handle(self, reader, writer) -> None:
        while True:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError:
                break
            self.heads.append(head)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            try:
                await writer.drain()
            except OSError:
                break
        writer.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


async def _get(port: int, path: bytes = b"/p") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            b"GET " + path + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"X-Keep: 1\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        return await asyncio.wait_for(reader.read(-1), 5)
    finally:
        writer.close()


async def test_serve_forwards_a_request_without_an_asgi_app() -> None:
    serve = _serve()
    origin = _Origin()
    origin_port = _next_port()
    await origin.start(origin_port)
    url = f"http://127.0.0.1:{origin_port}"
    port = _next_port()
    handle = await serve(UpstreamPool([Upstream(url)]), host="127.0.0.1", port=port)
    try:
        response = await _get(port)
        assert response.startswith(b"HTTP/1.1 200"), response[:120]
        assert response.endswith(b"ok")
        assert origin.heads, "nothing reached the origin"
        assert b"x-keep" in origin.heads[0].lower()
    finally:
        await handle.aclose()
        await origin.close()


async def test_no_task_is_created_for_a_forwarded_request() -> None:
    serve = _serve()
    origin = _Origin()
    origin_port = _next_port()
    await origin.start(origin_port)
    url = f"http://127.0.0.1:{origin_port}"
    port = _next_port()
    handle = await serve(UpstreamPool([Upstream(url)]), host="127.0.0.1", port=port)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    loop = asyncio.get_running_loop()
    created = 0
    original = loop.create_task

    def counting(*args, **kwargs):
        nonlocal created
        created += 1
        return original(*args, **kwargs)

    loop.create_task = counting  # type: ignore[method-assign]
    try:
        for i in range(3):
            writer.write(b"GET /p%d HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" % i)
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert head.startswith(b"HTTP/1.1 200"), head
            assert await reader.readexactly(2) == b"ok"
        assert created == 0, f"{created} Task(s) created for three forwarded requests"
        assert len(origin.heads) == 3, "the upstream connection was not reused"
    finally:
        loop.create_task = original  # type: ignore[method-assign]
        writer.close()
        await handle.aclose()
        await origin.close()


async def test_upstream_connections_are_open_before_the_first_request() -> None:
    serve = _serve()
    origin = _Origin()
    origin_port = _next_port()
    await origin.start(origin_port)
    url = f"http://127.0.0.1:{origin_port}"
    port = _next_port()
    handle = await serve(UpstreamPool([Upstream(url)]), host="127.0.0.1", port=port)
    try:
        assert handle.upstream_connections() > 0, (
            "no upstream connection was opened during configuration"
        )
    finally:
        await handle.aclose()
        await origin.close()


@pytest.mark.parametrize(
    "raw,reason",
    [
        (b"GET /p HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n", "two Host headers"),
        (
            b"POST /p HTTP/1.1\r\nHost: a\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\nhello",
            "two Content-Length headers",
        ),
        (
            b"POST /p HTTP/1.1\r\nHost: a\r\nContent-Length: 6\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            "Content-Length with Transfer-Encoding",
        ),
    ],
)
async def test_the_native_path_keeps_the_smuggling_refusals(raw: bytes, reason: str) -> None:
    serve = _serve()
    origin = _Origin()
    origin_port = _next_port()
    await origin.start(origin_port)
    url = f"http://127.0.0.1:{origin_port}"
    port = _next_port()
    handle = await serve(UpstreamPool([Upstream(url)]), host="127.0.0.1", port=port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(raw)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(-1), 5)
        finally:
            writer.close()
        assert response.startswith(b"HTTP/1.1 400"), f"{reason}: {response[:120]!r}"
        assert origin.heads == [], f"{reason} reached the origin"
    finally:
        await handle.aclose()
        await origin.close()


@pytest.mark.skipif(_reactor is None, reason="native reactor not built")
async def test_serve_terminates_tls_natively() -> None:
    import ssl as ssl_module

    from wreath.reactor import metal_tls_context

    serve = _serve()
    cert, key = _self_signed()
    origin = _Origin()
    origin_port = _next_port()
    await origin.start(origin_port)
    url = f"http://127.0.0.1:{origin_port}"
    port = _next_port()
    handle = await serve(
        UpstreamPool([Upstream(url)]),
        host="127.0.0.1",
        port=port,
        ssl=metal_tls_context(certfile=cert, keyfile=key),
    )
    try:
        client = ssl_module.SSLContext(ssl_module.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.verify_mode = ssl_module.CERT_NONE
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=client, server_hostname="localhost"
        )
        try:
            writer.write(
                b"GET /p HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Keep: 1\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(-1), 10)
        finally:
            writer.close()
        assert response.startswith(b"HTTP/1.1 200"), response[:120]
        assert response.endswith(b"ok")
        assert origin.heads, "nothing reached the origin"
        # The hop to the origin stays plaintext here, so the origin sees a
        # forwarded record saying the *client* arrived over https.
        assert b"x-forwarded-proto: https" in origin.heads[0].lower(), origin.heads[0]
    finally:
        await handle.aclose()
        await origin.close()
