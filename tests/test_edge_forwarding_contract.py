"""What `wreath.edge` puts on the wire, pinned byte by byte.

These assertions were not written from the RFC. They were **discovered
differentially**: the same corpus of raw requests driven through `wreath.edge`,
haproxy 3.4.3 and nginx 1.30.4 in front of an origin that records exactly what
arrived, then adjudicated case by case. That matters now, because the forwarding
path is being reimplemented in C and this file is the contract it has to meet --
written while the Python implementation still exists to check it against, which
is the last moment that comparison is possible.

The oracle is two independent implementations rather than a Python twin of our
own, because a twin can be wrong in both halves and still agree with itself. It
is deliberately *not* a byte-equality assertion: each proxy legitimately signs
its own work (`Via`, the `X-Forwarded-*` family) and the three disagree on real
questions. Where they disagreed, the RFC decided, and the reasoning is recorded
on the test rather than lost.

Driven over raw sockets, never through `HTTPClient`: half of what is under test
is what happens to a header a well-behaved client refuses to send.
"""

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

_REPLY = (b"HTTP/1.1 200 OK\r\n"
          b"Content-Type: text/plain\r\n"
          b"Content-Length: 2\r\n"
          b"\r\n"
          b"ok")


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

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
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
        return [ln.split(b":", 1)[0].decode("latin-1").lower()
                for ln in head.split(b"\r\n")[1:] if ln]

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
            url, base_url=url, destination=_LOCAL,
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
            app, ServerConfig(host="127.0.0.1", port=self.port, lifespan="off"))

    async def send(self, payload: bytes) -> bytes:
        """Write raw bytes, return the client-visible status line."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(payload)
            await writer.drain()
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            except (asyncio.IncompleteReadError, TimeoutError):
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


def _request(host_port: int, *extra: bytes, method: bytes = b"GET",
             body: bytes = b"") -> bytes:
    lines = [b"%s /p HTTP/1.1" % method, b"Host: 127.0.0.1:%d" % host_port, *extra]
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


async def test_a_header_named_by_connection_is_stripped_before_forwarding() -> None:
    """RFC 9110 7.6.1: every name in `Connection` is hop-by-hop. Strip it.

    **Both oracles get this wrong and `wreath.edge` gets it right**, which is the
    clearest argument in this file for adjudicating the differential rather than
    asserting equality with it. Given `Connection: keep-alive, X-Hop`, haproxy
    3.4.3 forwards `X-Hop` *and* the `Connection` header itself, and nginx 1.30.4
    forwards `X-Hop`; both leak a control the previous hop meant for this hop
    alone. Pinned here because the C path must not "fix" itself into agreeing
    with them.
    """
    edge = _Edge()
    await edge.start()
    try:
        status = await edge.send(_request(
            edge.port, b"Connection: keep-alive, X-Hop", b"X-Hop: secret",
            b"X-Keep: yes"))
        assert status.startswith(b"HTTP/1.1 200")
        assert edge.recorder.heads, "nothing reached the origin"
        names = edge.recorder.names()
        assert "x-hop" not in names, names
        assert "connection" not in names, names
        assert "x-keep" in names, "a header not named by Connection must survive"
    finally:
        await edge.close()


async def test_the_standard_hop_by_hop_headers_never_reach_the_origin() -> None:
    """The fixed list, separate from the `Connection`-named case above.

    One test per hypothesis would be four near-identical bodies; these share a
    single mechanism -- a name in the fixed hop-by-hop set -- so they share a
    test, and the assertion names which one failed.
    """
    edge = _Edge()
    await edge.start()
    try:
        status = await edge.send(_request(
            edge.port, b"TE: trailers", b"Upgrade: websocket",
            b"Keep-Alive: timeout=5, max=100", b"X-Keep: yes"))
        assert status.startswith(b"HTTP/1.1 200")
        names = edge.recorder.names()
        for hop in ("te", "upgrade", "keep-alive"):
            assert hop not in names, f"{hop} was forwarded: {names}"
        assert "x-keep" in names
    finally:
        await edge.close()


async def test_host_is_rewritten_to_the_upstream_authority() -> None:
    """The client's `Host` does not survive; the upstream's authority replaces it.

    A real divergence rather than a detail, and the three proxies each answer
    differently: haproxy forwards the client's `Host` untouched, nginx sends the
    name of its `upstream` block, and `wreath.edge` sends the upstream's own
    host:port. It decides which vhost a name-based origin serves, so it is
    pinned rather than left to whichever the implementation happens to do.
    """
    edge = _Edge()
    await edge.start()
    try:
        await edge.send(_request(edge.port, b"X-Keep: yes"))
        host = edge.recorder.value("host")
        assert host is not None
        assert host != f"127.0.0.1:{edge.port}", (
            "the client's own Host reached the origin unrewritten")
        assert host.startswith("127.0.0.1:"), host
    finally:
        await edge.close()


async def test_a_client_supplied_forwarded_for_is_replaced_not_appended() -> None:
    """A client-sent `X-Forwarded-For` is discarded, not extended.

    `wreath.edge` owns this header: whatever the client claimed is dropped and
    the observed peer replaces it. Both oracles do the opposite -- haproxy and
    nginx forward `203.0.113.9` unchanged -- so this is the one case where being
    stricter has a cost worth naming rather than celebrating.

    Deny-by-default is the right posture for a *edge* proxy, where the peer is
    unauthenticated and a forged chain is how a client claims someone else's
    address. The cost is that `wreath.edge` **cannot be chained behind another
    proxy without losing the real client address**, because it cannot tell a
    trusted front door from a hostile client. Pinned as behaviour, and recorded
    on the roadmap as the thing a trusted-peer setting would change.
    """
    edge = _Edge()
    await edge.start()
    try:
        await edge.send(_request(
            edge.port, b"X-Forwarded-For: 203.0.113.9", b"X-Keep: yes"))
        forwarded_for = edge.recorder.value("x-forwarded-for")
        assert forwarded_for is not None, "the header was dropped entirely"
        assert "203.0.113.9" not in forwarded_for, (
            "a client-supplied forwarded-for chain was trusted and passed on")
        assert forwarded_for == "127.0.0.1", forwarded_for
    finally:
        await edge.close()


async def test_the_proxy_signs_the_hop_it_added() -> None:
    """`Via` and the `Forwarded` family identify the hop, per RFC 7239.

    Neither oracle sends `Forwarded` at all, so there is nothing to compare
    against; it is asserted from the RFC. Without it an origin cannot tell it is
    behind a proxy, which is what `ProxyHeadersMiddleware` reads on the far side.
    """
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
    """CL+TE is refused outright, and nothing is forwarded.

    RFC 9112 6.1 permits resolving in favour of `Transfer-Encoding`, and haproxy
    does exactly that -- it strips the `Content-Length` and forwards the request.
    That is legal and it is how a desync begins when the next hop disagrees about
    which header won. `wreath.edge` refuses, as nginx does. The assertion that
    matters is the second one: the origin saw nothing at all.
    """
    edge = _Edge()
    await edge.start()
    try:
        status = await edge.send(_request(
            edge.port, b"Content-Length: 6", b"Transfer-Encoding: chunked",
            method=b"POST", body=b"0\r\n\r\n"))
        assert status.startswith(b"HTTP/1.1 400"), status
        assert edge.recorder.heads == [], "a smuggling vector reached the origin"
    finally:
        await edge.close()


async def test_a_duplicated_content_length_is_refused() -> None:
    """Two `Content-Length` headers are a desync primitive, not a parse choice.

    All three proxies agree here; pinned so the C path keeps agreeing.
    """
    edge = _Edge()
    await edge.start()
    try:
        status = await edge.send(_request(
            edge.port, b"Content-Length: 5", b"Content-Length: 6",
            method=b"POST", body=b"hello"))
        assert status.startswith(b"HTTP/1.1 400"), status
        assert edge.recorder.heads == []
    finally:
        await edge.close()


async def test_a_duplicated_host_is_refused() -> None:
    """Two `Host` headers let a client pick which vhost the *next* hop sees."""
    edge = _Edge()
    await edge.start()
    try:
        status = await edge.send(_request(edge.port, b"Host: evil.example"))
        assert status.startswith(b"HTTP/1.1 400"), status
        assert edge.recorder.heads == []
    finally:
        await edge.close()
