"""`wreath.edge.serve()`: a proxy whose request path contains no Python.

Written as a failing spec before the protocol existed, so it is a contract rather
than a description of whatever the code turned out to do. It is green now; the
shape it pins is the reason to keep reading it.

**The point is the absence, not the speed.** `serve()` takes an `UpstreamPool`
and *no ASGI app*, because there is nothing for an app to do: a native protocol
owns the listening socket, and on a complete request head it selects an upstream
from a compiled table, builds the outbound head in C, writes it to an
already-open upstream transport, and pipes the response back. No scope, no
`Request`, no coroutine, no Task. Having no app to call is what makes that
structural instead of aspirational -- there is no seam through which Python can
creep back onto the request path later.

Why the pool is warmed at startup: `loop.create_connection` is a coroutine, so
opening an upstream connection mid-request pulls asyncio's Task and Future
machinery straight back in -- 6.3 CPU-microseconds for the Task alone, plus the
orchestration around it. Connections are established while the proxy is being
configured, which is Python's half of the job, so the request path only ever
picks an open transport and writes to it.

Sizing, so a later reader knows what this is worth. Measured on one machine, 32
connections, three 5s runs, CPU-microseconds per forwarded request:

    ReverseProxy (ASGI), on metal      112
    single-threaded haproxy 3.4.3       26
    nginx 1.30.4, one worker            23
    wreath.edge.serve()                 19
    wreath's own server, answering      10

The gap was never the language or the syscalls (4.6 against haproxy's 4.8) --
wreath *serves* a request for less than either proxy spends forwarding one. It
was the Python orchestration between primitives that were already in C, and
`serve()` removes it rather than shrinking it.
"""

from __future__ import annotations

import asyncio
import os
import socket
from typing import cast

import pytest

from wreath.edge import Upstream, UpstreamPool
from wreath.edge.serve import EdgeHandle

_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
_SLOT = int("".join(c for c in _WORKER if c.isdigit()) or 0)
#: Below the ephemeral range (`ip_local_port_range` starts at 32768), because a
#: proxy makes an outbound connection per request and the kernel draws its
#: source ports from there -- a listener inside that range can lose its own bind.
_PORT = 27800 + _SLOT * 40


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
    """A reconnect task is owned until shutdown, never left pending on the loop."""
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
    """The signature is the specification: a pool, and nothing to call.

    If this ever grows an `app` parameter, the design has been lost -- that is
    the seam Python returns through.
    """
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
    """The measurable form of "no Python": nothing schedules on the loop.

    A Task per request is 6.3 CPU-microseconds and, more importantly, proof that
    a coroutine ran -- which means a scope was built and the request went through
    the framework rather than around it. Counting `create_task` is the cheapest
    assertion that distinguishes the native path from a fast Python one.

    **The connection is established before the counter goes on, and that is not
    a loophole.** `BaseSelectorEventLoop._accept_connection` creates one Task per
    accepted connection unconditionally -- it is how stock asyncio runs its own
    handshake step, it is paid by every asyncio server including this test's
    origin, and no protocol can decline it. Counting it would measure asyncio
    accepting a socket and call the answer wreath's. What is wreath's is
    everything after: three keep-alive requests down one open connection, which
    is the claim this file exists to pin and a stricter one than the original two
    connections made -- it also proves the second request costs nothing the first
    did not.
    """
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
    """Warmed at configuration time, so the request path never awaits a connect.

    `loop.create_connection` is a coroutine; reaching for one mid-request drags
    the Task and Future machinery back onto the path this exists to keep clear.
    """
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
    """The refusals survive the rewrite, and nothing reaches the origin.

    Pinned separately from `test_edge_forwarding_contract.py` because that file
    tests the Python implementation being replaced. These are the same rules
    asserted against the thing replacing it -- a rewrite that gets faster by
    relaxing framing checks has not got faster, it has become a desync vector.
    """
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


async def test_serve_terminates_tls_natively() -> None:
    """The proxy faces the internet, which means it terminates TLS.

    Until this passes `wreath.edge.serve()` is an east-west proxy: fine for
    internal plaintext hops, and not deployable at an edge, because TLS
    termination is the primary job of the thing it replaces.

    Native termination is the whole point of doing it here. Measured on one
    physical core, handshakes amortised: 21,500 req/s through
    `asyncio.sslproto` against 46,000 through the reactor's C transport, with
    nginx at 48,200.
    """
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
