"""What `wreath.edge.serve()` does with a message, beyond the happy path.

`test_edge_native_serve.py` pins the shape of the thing -- no app, no Task, warm
connections, the smuggling refusals. This pins its behaviour as a proxy: which
fields survive a hop, how each body framing is relayed, and what a client is told
when the origin is the one that failed.

Kept separate from `test_edge_forwarding_contract.py`, which asserts the same
rules against `ReverseProxy`. Two implementations of one contract need two sets
of assertions or the second one is only ever as correct as the day it was
written.
"""

from __future__ import annotations

import asyncio
import os
import socket

import pytest

from wreath._native import _reactor
from wreath.edge import Upstream, UpstreamPool, serve

_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
_SLOT = int("".join(c for c in _WORKER if c.isdigit()) or 0)
#: Below the ephemeral range, for the reason given in `test_edge_native_serve`.
_PORT = 25200 + _SLOT * 60


def _next_port() -> int:
    """The next port in this worker's band that will actually bind.

    Probing rather than counting: the counter is deterministic, so the same
    worker asks for the same port every run and a socket left in `TIME_WAIT` by
    the run before turns that into a flake. The probe uses the options
    `create_server` uses, so a port it accepts is one asyncio will accept.
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


class _Origin:
    """An origin that answers with whatever the test hands it.

    `reply` is called with the head it received and returns the bytes to send;
    returning `None` closes the connection without answering, which is how the
    tests reach the failure paths. `close_after` hangs up once the reply is
    written, which is the only way to express a close-delimited body.
    """

    def __init__(self, reply, *, close_after: bool = False) -> None:
        self.heads: list[bytes] = []
        self.bodies: list[bytes] = []
        self._reply = reply
        self._close_after = close_after
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        port = _next_port()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        return port

    async def _handle(self, reader, writer) -> None:
        while True:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
                break
            self.heads.append(head)
            body = b""
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
                    if length:
                        body = await reader.readexactly(length)
            self.bodies.append(body)
            answer = self._reply(head)
            if answer is None:
                break
            writer.write(answer)
            try:
                await writer.drain()
            except OSError:
                break
            if self._close_after:
                break
        writer.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


def _ok(body: bytes = b"ok", extra: bytes = b"") -> bytes:
    return (b"HTTP/1.1 200 OK\r\ncontent-length: %d\r\n%s\r\n%s"
            % (len(body), extra, body))


async def _proxy(origin: _Origin, origin_port: int, **kwargs):
    pool = UpstreamPool([Upstream(f"http://127.0.0.1:{origin_port}")])
    port = _next_port()
    handle = await serve(pool, host="127.0.0.1", port=port, **kwargs)
    return handle, port


async def _exchange(port: int, raw: bytes, *, read: int = -1) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(raw)
        await writer.drain()
        if read < 0:
            return await asyncio.wait_for(reader.read(-1), 5)
        return await asyncio.wait_for(reader.readexactly(read), 5)
    finally:
        writer.close()


async def test_a_chunked_request_reaches_the_origin_with_a_length() -> None:
    """The outbound framing describes what this proxy sends, not what it got.

    `Transfer-Encoding` is hop-by-hop, so it cannot be relayed; the body has to
    be re-framed for the next hop. Forwarding the client's framing verbatim is
    how a proxy and an origin come to disagree about where the message ends.
    """
    origin = _Origin(lambda head: _ok())
    origin_port = await origin.start()
    handle, port = await _proxy(origin, origin_port)
    try:
        response = await _exchange(port, (
            b"POST /p HTTP/1.1\r\nHost: h\r\nTransfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n5\r\nhello\r\n2\r\n, \r\n5\r\nworld\r\n0\r\n\r\n"
        ))
        assert response.startswith(b"HTTP/1.1 200"), response[:120]
        assert origin.bodies == [b"hello, world"]
        assert b"content-length: 12\r\n" in origin.heads[0].lower()
        assert b"transfer-encoding" not in origin.heads[0].lower()
    finally:
        await handle.aclose()
        await origin.close()


async def test_a_chunked_response_is_relayed_as_chunks() -> None:
    """A body with no declared length is not buffered into one, either.

    Reading it whole to re-frame it would make the proxy's memory the origin's
    to choose, which is the failure mode `StreamingResponse` exists to avoid on
    the Python path.
    """
    chunked = (b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"
               b"4\r\nwrea\r\n2\r\nth\r\n0\r\n\r\n")
    origin = _Origin(lambda head: chunked)
    origin_port = await origin.start()
    handle, port = await _proxy(origin, origin_port)
    try:
        response = await _exchange(
            port, b"GET /p HTTP/1.1\r\nHost: h\r\nConnection: close\r\n\r\n")
        head, _, body = response.partition(b"\r\n\r\n")
        assert b"transfer-encoding: chunked" in head.lower(), head
        assert body == b"4\r\nwrea\r\n2\r\nth\r\n0\r\n\r\n", body
    finally:
        await handle.aclose()
        await origin.close()


async def test_a_field_named_by_connection_does_not_reach_the_origin() -> None:
    """`Connection: x-secret` makes `x-secret` hop-by-hop for this message.

    RFC 9110 7.6.1. Both oracles get this wrong -- haproxy 3.4.3 and nginx
    1.30.4 forward such a field -- which is why it is asserted rather than
    inherited.
    """
    origin = _Origin(lambda head: _ok())
    origin_port = await origin.start()
    handle, port = await _proxy(origin, origin_port)
    try:
        await _exchange(port, (
            b"GET /p HTTP/1.1\r\nHost: h\r\nX-Secret: leaked\r\nX-Kept: yes\r\n"
            b"Connection: close, x-secret\r\n\r\n"
        ))
        head = origin.heads[0].lower()
        assert b"x-secret" not in head, head
        assert b"x-kept: yes" in head, head
    finally:
        await handle.aclose()
        await origin.close()


async def test_an_inbound_x_forwarded_for_is_replaced_not_appended() -> None:
    """The audit trail is this hop's to write.

    Appending is the classic spoof: the attacker writes the first element, and
    every parser that reads "the client" reads theirs. `Via` is appended instead,
    and the asymmetry is deliberate -- it is a topology record and an
    authorization input nowhere.
    """
    origin = _Origin(lambda head: _ok())
    origin_port = await origin.start()
    handle, port = await _proxy(origin, origin_port)
    try:
        await _exchange(port, (
            b"GET /p HTTP/1.1\r\nHost: h\r\nX-Forwarded-For: 9.9.9.9\r\n"
            b"Via: 1.1 someone\r\nConnection: close\r\n\r\n"
        ))
        head = origin.heads[0].lower()
        assert b"9.9.9.9" not in head, head
        assert b"x-forwarded-for: 127.0.0.1\r\n" in head, head
        assert b"via: 1.1 someone, 1.1 wreath\r\n" in head, head
    finally:
        await handle.aclose()
        await origin.close()


async def test_hop_by_hop_response_fields_are_not_relayed() -> None:
    """A response describes the connection it arrived on, not the one it leaves.

    Relaying the origin's `Keep-Alive` would hand the client a policy for a
    socket it has no access to.
    """
    origin = _Origin(lambda head: _ok(extra=b"keep-alive: timeout=5\r\nx-app: yes\r\n"))
    origin_port = await origin.start()
    handle, port = await _proxy(origin, origin_port)
    try:
        response = await _exchange(
            port, b"GET /p HTTP/1.1\r\nHost: h\r\nConnection: close\r\n\r\n")
        head = response.partition(b"\r\n\r\n")[0].lower()
        assert b"keep-alive" not in head, head
        assert b"x-app: yes" in head, head
    finally:
        await handle.aclose()
        await origin.close()


async def test_a_response_to_head_relays_no_body() -> None:
    """`Content-Length` on a HEAD response describes the GET that was not made.

    Waiting for octets the origin will never send would hang the connection,
    and relaying the *next* response's bytes as this one's body is a desync.
    """
    origin = _Origin(lambda head: b"HTTP/1.1 200 OK\r\ncontent-length: 12\r\n\r\n")
    origin_port = await origin.start()
    handle, port = await _proxy(origin, origin_port)
    try:
        response = await _exchange(
            port, b"HEAD /p HTTP/1.1\r\nHost: h\r\nConnection: close\r\n\r\n")
        head, _, body = response.partition(b"\r\n\r\n")
        assert b"content-length: 12" in head.lower(), head
        assert body == b"", body
    finally:
        await handle.aclose()
        await origin.close()


async def test_a_body_over_the_bound_is_refused_and_never_forwarded() -> None:
    """413 here, rather than an unbounded read on the origin's behalf.

    Request bodies are buffered, which is the documented edge of this path. The
    bound is what keeps that from being a way to spend the proxy's memory.
    """
    origin = _Origin(lambda head: _ok())
    origin_port = await origin.start()
    handle, port = await _proxy(origin, origin_port, max_body=16)
    try:
        response = await _exchange(port, (
            b"POST /p HTTP/1.1\r\nHost: h\r\nContent-Length: 64\r\n"
            b"Connection: close\r\n\r\n" + b"x" * 64
        ))
        assert response.startswith(b"HTTP/1.1 413"), response[:120]
        assert origin.heads == [], "an over-sized body reached the origin"
    finally:
        await handle.aclose()
        await origin.close()


async def test_an_origin_that_answers_nothing_becomes_a_502() -> None:
    """The request was fine and the upstream was not, so the blame is 5xx.

    A 4xx here would tell whoever reads the log that the client sent something
    wrong, which is the one thing this case rules out.
    """
    origin = _Origin(lambda head: None)
    origin_port = await origin.start()
    handle, port = await _proxy(origin, origin_port, connections=1)
    try:
        response = await _exchange(
            port, b"GET /p HTTP/1.1\r\nHost: h\r\nConnection: close\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 502"), response[:120]
    finally:
        await handle.aclose()
        await origin.close()


async def test_a_request_waits_when_every_upstream_connection_is_busy() -> None:
    """Queued in C, not refused, and not given a connection that does not exist.

    With one connection to the origin, the second request has nowhere to go
    until the first finishes. Answering it 502 would turn the proxy's own
    concurrency limit into the origin's fault.
    """
    gate = asyncio.Event()
    replies: list[bytes] = []

    def reply(head: bytes) -> bytes:
        replies.append(head)
        return _ok(b"served")

    origin = _Origin(reply)
    origin_port = await origin.start()
    handle, port = await _proxy(origin, origin_port, connections=1)
    gate.set()
    try:
        both = await asyncio.gather(
            _exchange(port, b"GET /a HTTP/1.1\r\nHost: h\r\nConnection: close\r\n\r\n"),
            _exchange(port, b"GET /b HTTP/1.1\r\nHost: h\r\nConnection: close\r\n\r\n"),
        )
        for response in both:
            assert response.startswith(b"HTTP/1.1 200"), response[:120]
            assert response.endswith(b"served")
        assert handle.stats()["requests"] == 2
        assert handle.stats()["waiting"] == 0
    finally:
        await handle.aclose()
        await origin.close()


async def test_a_close_delimited_response_ends_the_client_connection_too() -> None:
    """When close *is* the framing, it has to be the framing on both sides.

    Keeping the client's connection open after a body whose only terminator was
    the origin hanging up would leave the client waiting for a length nobody
    ever declared.
    """
    origin = _Origin(lambda head: b"HTTP/1.1 200 OK\r\n\r\nunbounded",
                     close_after=True)
    origin_port = await origin.start()
    handle, port = await _proxy(origin, origin_port, connections=1)
    try:
        response = await _exchange(
            port, b"GET /p HTTP/1.1\r\nHost: h\r\n\r\n")
        head, _, body = response.partition(b"\r\n\r\n")
        assert b"connection: close" in head.lower(), head
        assert body == b"unbounded", body
    finally:
        await handle.aclose()
        await origin.close()


async def test_an_unsupported_upstream_scheme_is_refused_at_configuration_time() -> None:
    """At configuration time, not on the request that first selects it.

    A pool with one upstream this cannot speak to would otherwise answer every
    request until that member is chosen, and then fail for a reason nobody
    connects to the configuration that caused it.

    This used to assert that `https://` was refused. It is not any more -- the
    native transport speaks `SSL_connect`, so refusing would be the proxy
    declining work it can do. What is left is a scheme with no meaning here.
    """
    pool = UpstreamPool([Upstream("unix:///var/run/app.sock")])
    with pytest.raises(ValueError, match="http"):
        await serve(pool, host="127.0.0.1", port=_next_port())


@pytest.mark.skipif(_reactor is None, reason="native reactor not built")
async def test_an_https_upstream_is_forwarded_to_over_native_tls() -> None:
    """The proxy's outbound half, once the transport can speak `SSL_connect`.

    `https://` upstreams were refused at configuration time because the native
    path had no client TLS at all. Now that it does, the refusal would be the
    proxy declining work it can do -- and the alternative for anyone needing it
    is `ReverseProxy`, which costs about six times as much per request.

    The pre-warm still happens while the proxy is being configured, so the
    handshake is paid at startup rather than on the request path.
    """
    import ssl as ssl_module

    from tests.reactor.test_metal_tier import _dev_cert

    cert, key = _dev_cert()
    server_ctx = ssl_module.SSLContext(ssl_module.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)

    origin = _Origin(lambda head: _ok(b"secure"))
    port = _next_port()
    origin._server = await asyncio.start_server(
        origin._handle, "127.0.0.1", port, ssl=server_ctx)

    pool = UpstreamPool([Upstream(f"https://localhost:{port}")])
    listen = _next_port()
    handle = await serve(pool, host="127.0.0.1", port=listen,
                         connections=2, upstream_cafile=cert)
    try:
        response = await _exchange(
            listen, b"GET /p HTTP/1.1\r\nHost: h\r\nConnection: close\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 200"), response[:120]
        assert response.endswith(b"secure"), response[-20:]
        assert origin.heads, "nothing reached the origin"
        assert handle.upstream_connections() == 2
    finally:
        await handle.aclose()
        await origin.close()
