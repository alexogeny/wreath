from __future__ import annotations

import asyncio
import os

from wreath import Request, Wreath
from wreath.edge import ReverseProxy, Upstream, UpstreamPool
from wreath.http_client import ClientLimits, DestinationPolicy, HTTPClient
from wreath.server import ServerConfig, serve

#: Per xdist worker, and below the ephemeral range. `ip_local_port_range` starts
#: at 32768 on Linux, so a listener above it can collide with the kernel's own
#: choice of source port for an outbound connection -- which is exactly what a
#: proxy makes on every request. `tests/test_edge.py` records the xdist half of
#: this trap; this is the other half, and it cost a bind failure to find.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
_SLOT = int("".join(c for c in _WORKER if c.isdigit()) or 0)
_PORT = 27600 + _SLOT * 40
_LOCAL = DestinationPolicy(allow_loopback=True)

_REPLY = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"


def _next_port() -> int:
    global _PORT
    _PORT += 1
    return _PORT


class _Recorder:
    """An origin that keeps the request head verbatim, before any parsing.

    A Wreath app would work for reading a named header, but not for proving one
    is *absent* in the bytes rather than merely absent from a parsed view -- and
    absence is most of what this file asserts.
    """

    def __init__(self) -> None:
        self.heads: list[bytes] = []
        self._server: asyncio.Server | None = None

    async def start(self, port: int) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError:
                break
            self.heads.append(head)
            for line in head.lower().split(b"\r\n"):
                if line.startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
                    if length:
                        await reader.readexactly(length)
                    break
            writer.write(_REPLY)
            try:
                await writer.drain()
            except OSError:
                break
        writer.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def names(self, index: int = 0) -> list[str]:
        head = self.heads[index]
        return [
            ln.split(b":", 1)[0].decode("latin-1").lower() for ln in head.split(b"\r\n")[1:] if ln
        ]

    def value(self, name: str, index: int = 0) -> str | None:
        head = self.heads[index]
        for ln in head.split(b"\r\n")[1:]:
            if ln.lower().startswith(name.encode() + b":"):
                return ln.split(b":", 1)[1].strip().decode("latin-1")
        return None


class _Edge:
    """`wreath.edge` in front of a recorder, driven over a raw socket."""

    def __init__(self) -> None:
        self.recorder = _Recorder()
        self.port = 0
        self._server: object | None = None
        self._client: HTTPClient | None = None

    async def start(self) -> None:
        origin_port = _next_port()
        await self.recorder.start(origin_port)
        url = f"http://127.0.0.1:{origin_port}"
        self._client = HTTPClient(
            url,
            base_url=url,
            destination=_LOCAL,
            limits=ClientLimits(max_connections=16, max_keepalive_connections=16),
        )
        await self._client.start()
        proxy = ReverseProxy(UpstreamPool([Upstream(url)]), {url: self._client})
        app = Wreath()

        @app.get("/{path:path}")
        async def relay_get(request: Request, path: str):
            return await proxy(request)

        @app.post("/{path:path}")
        async def relay_post(request: Request, path: str):
            return await proxy(request)

        self.port = _next_port()
        self._server = await serve(
            app, ServerConfig(host="127.0.0.1", port=self.port, lifespan="off")
        )

    async def send(self, payload: bytes) -> bytes:
        """Write raw bytes, return the client-visible status line."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(payload)
            await writer.drain()
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            except asyncio.IncompleteReadError, TimeoutError:
                return b""
            return head.split(b"\r\n", 1)[0]
        finally:
            writer.close()

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()
        if self._client is not None:
            await self._client.close()
        await self.recorder.close()


def _request(host_port: int, *extra: bytes, method: bytes = b"GET", body: bytes = b"") -> bytes:
    lines = [b"%s /p HTTP/1.1" % method, b"Host: 127.0.0.1:%d" % host_port, *extra]
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


async def test_a_header_named_by_connection_is_stripped_before_forwarding() -> None:
    edge = _Edge()
    await edge.start()
    try:
        status = await edge.send(
            _request(edge.port, b"Connection: keep-alive, X-Hop", b"X-Hop: secret", b"X-Keep: yes")
        )
        assert status.startswith(b"HTTP/1.1 200")
        assert edge.recorder.heads, "nothing reached the origin"
        names = edge.recorder.names()
        assert "x-hop" not in names, names
        assert "connection" not in names, names
        assert "x-keep" in names, "a header not named by Connection must survive"
    finally:
        await edge.close()


async def test_the_standard_hop_by_hop_headers_never_reach_the_origin() -> None:
    edge = _Edge()
    await edge.start()
    try:
        status = await edge.send(
            _request(
                edge.port,
                b"TE: trailers",
                b"Upgrade: websocket",
                b"Keep-Alive: timeout=5, max=100",
                b"X-Keep: yes",
            )
        )
        assert status.startswith(b"HTTP/1.1 200")
        names = edge.recorder.names()
        for hop in ("te", "upgrade", "keep-alive"):
            assert hop not in names, f"{hop} was forwarded: {names}"
        assert "x-keep" in names
    finally:
        await edge.close()


async def test_host_is_rewritten_to_the_upstream_authority() -> None:
    edge = _Edge()
    await edge.start()
    try:
        await edge.send(_request(edge.port, b"X-Keep: yes"))
        host = edge.recorder.value("host")
        assert host is not None
        assert host != f"127.0.0.1:{edge.port}", (
            "the client's own Host reached the origin unrewritten"
        )
        assert host.startswith("127.0.0.1:"), host
    finally:
        await edge.close()


async def test_a_client_supplied_forwarded_for_is_replaced_not_appended() -> None:
    edge = _Edge()
    await edge.start()
    try:
        await edge.send(_request(edge.port, b"X-Forwarded-For: 203.0.113.9", b"X-Keep: yes"))
        forwarded_for = edge.recorder.value("x-forwarded-for")
        assert forwarded_for is not None, "the header was dropped entirely"
        assert "203.0.113.9" not in forwarded_for, (
            "a client-supplied forwarded-for chain was trusted and passed on"
        )
        assert forwarded_for == "127.0.0.1", forwarded_for
    finally:
        await edge.close()


async def test_the_proxy_signs_the_hop_it_added() -> None:
    edge = _Edge()
    await edge.start()
    try:
        await edge.send(_request(edge.port, b"X-Keep: yes"))
        names = edge.recorder.names()
        assert "via" in names, names
        assert "forwarded" in names, names
        assert edge.recorder.value("forwarded")
    finally:
        await edge.close()


async def test_content_length_with_transfer_encoding_is_refused() -> None:
    edge = _Edge()
    await edge.start()
    try:
        status = await edge.send(
            _request(
                edge.port,
                b"Content-Length: 6",
                b"Transfer-Encoding: chunked",
                method=b"POST",
                body=b"0\r\n\r\n",
            )
        )
        assert status.startswith(b"HTTP/1.1 400"), status
        assert edge.recorder.heads == [], "a smuggling vector reached the origin"
    finally:
        await edge.close()


async def test_a_duplicated_content_length_is_refused() -> None:
    edge = _Edge()
    await edge.start()
    try:
        status = await edge.send(
            _request(
                edge.port, b"Content-Length: 5", b"Content-Length: 6", method=b"POST", body=b"hello"
            )
        )
        assert status.startswith(b"HTTP/1.1 400"), status
        assert edge.recorder.heads == []
    finally:
        await edge.close()


async def test_a_duplicated_host_is_refused() -> None:
    edge = _Edge()
    await edge.start()
    try:
        status = await edge.send(_request(edge.port, b"Host: evil.example"))
        assert status.startswith(b"HTTP/1.1 400"), status
        assert edge.recorder.heads == []
    finally:
        await edge.close()
