"""Native TLS via OpenSSL memory BIO — replaces asyncio's Python SSLProtocol.

Exercises the reactor's own create_server/create_connection TLS path: handshake,
ALPN (how H1/H2 are chosen), SNI, data, and clean close_notify. RED until built.
"""
from __future__ import annotations

import asyncio
import socket
import threading

import pytest

from .support import make_tls_contexts, run


class _Echo(asyncio.Protocol):
    def connection_made(self, transport):
        self._t = transport

    def data_received(self, data):
        self._t.write(data)


def _tls_client(port, client_ctx, out):
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    tls = client_ctx.wrap_socket(raw, server_hostname="localhost")
    out["alpn"] = tls.selected_alpn_protocol()
    out["peercert"] = bool(tls.getpeercert())
    tls.sendall(b"tls-echo")
    out["echo"] = tls.recv(100)
    tls.close()


def test_tls_handshake_alpn_and_echo(loop):
    pytest.importorskip("cryptography")
    server_ctx, client_ctx = make_tls_contexts(alpn=["h2", "http/1.1"])
    out: dict = {}

    async def main():
        server = await loop.create_server(_Echo, "127.0.0.1", 0, ssl=server_ctx)
        port = server.sockets[0].getsockname()[1]
        th = threading.Thread(target=_tls_client, args=(port, client_ctx, out))
        th.start()
        while th.is_alive():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        server.close()
        await server.wait_closed()

    run(loop, main())
    assert out["echo"] == b"tls-echo"
    assert out["alpn"] in ("h2", "http/1.1")
    assert out["peercert"] is True


def test_start_tls_upgrades_a_plain_connection(loop):
    """create_connection + start_tls is how opportunistic upgrades work."""
    pytest.importorskip("cryptography")
    server_ctx, client_ctx = make_tls_contexts()
    out: dict = {}

    async def main():
        server = await loop.create_server(_Echo, "127.0.0.1", 0, ssl=server_ctx)
        port = server.sockets[0].getsockname()[1]

        def client():
            raw = socket.create_connection(("127.0.0.1", port), timeout=5)
            tls = client_ctx.wrap_socket(raw, server_hostname="localhost")
            tls.sendall(b"x")
            out["echo"] = tls.recv(10)
            tls.close()

        th = threading.Thread(target=client)
        th.start()
        while th.is_alive():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        server.close()
        await server.wait_closed()

    run(loop, main())
    assert out["echo"] == b"x"
