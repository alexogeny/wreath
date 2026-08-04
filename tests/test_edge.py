"""The edge prototype: streaming client, proxy header hygiene, and the pool.

Stages 1-3 of `.analysis/edge-and-orchestrator.md`. The streaming client is the
part that matters beyond the prototype -- `wreath.http_client` could not stream
a response at all before it, which is a gap on its own terms.
"""

from __future__ import annotations

import os

import pytest

from wreath import Request, Wreath
from wreath.edge import Ejection, ReverseProxy, Upstream, UpstreamPool, forwardable
from wreath.edge.headers import HOP_BY_HOP
from wreath.http_client import DestinationPolicy, HTTPClient, ResponseTooLarge
from wreath.response import StreamingResponse
from wreath.server import ServerConfig, serve

# `asyncio_mode = "auto"` marks the async tests; a module-level marker would
# also land on the synchronous ones and warn on every run.
#: A private five-digit block, below the ephemeral range (32768+) so the kernel
#: never hands one of these to something else mid-test.
#:
#: **Per xdist worker.** Every worker imports this module and would otherwise
#: start from the same number, so under `-n 8` eight workers bind the same port
#: and seven of them fail -- which is what happened the first time this ran in
#: the full suite, having passed alone. Derived from `PYTEST_XDIST_WORKER`
#: ("gw3" -> 3) and *assigned*, never defaulted in a conftest: the controller
#: imports a conftest during collection and then spawns workers with its own
#: environment, so `setdefault` there silently gives every worker the
#: controller's value. `AGENTS.md` records that trap for database schemas; it is
#: the same one.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
_SLOT = int("".join(c for c in _WORKER if c.isdigit()) or 0)
_PORT = 28150 + _SLOT * 40
_LOCAL = DestinationPolicy(allow_loopback=True)


def _next_port() -> int:
    global _PORT
    _PORT += 1
    return _PORT


def _origin() -> Wreath:
    app = Wreath()

    @app.get("/small")
    async def small(request: Request) -> dict:
        return {"ok": True}

    @app.get("/big")
    async def big(request: Request):
        async def gen():
            for _ in range(80):
                yield b"z" * 65536          # 5.2 MB, past the 2 MiB buffer cap
        return StreamingResponse(
            gen(), headers=[(b"content-type", b"application/octet-stream")]
        )

    @app.get("/echo")
    async def echo(request: Request) -> dict:
        return {
            "hop": request.header("x-hop"),
            "xff": request.header("x-forwarded-for"),
            "via": request.header("via"),
            "host": request.header("host"),
        }

    return app


# --- stage 1: streaming ------------------------------------------------------


async def test_stream_delivers_a_body_larger_than_the_buffer_cap() -> None:
    """The whole reason this exists: `request` cannot carry this body at all."""
    port = _next_port()
    server = await serve(
        _origin(), ServerConfig(host="127.0.0.1", port=port, lifespan="off"))
    client = HTTPClient("o", base_url=f"http://127.0.0.1:{port}", destination=_LOCAL)
    await client.start()
    try:
        total = 0
        async with client.stream("GET", "/big") as response:
            assert response.status == 200
            async for chunk in response.iter_bytes():
                total += len(chunk)
        assert total == 80 * 65536
        assert total > client._limits.max_response_bytes
        # ... and the buffered path still refuses it, so the cap still means
        # something. A streaming API that quietly raised the buffered ceiling
        # would have removed a bound rather than added a capability.
        with pytest.raises(ResponseTooLarge):
            await client.request("GET", "/big")
    finally:
        await client.close()
        await server.close()


async def test_a_fully_read_stream_returns_its_connection_to_the_pool() -> None:
    """A streamed response that ended where it said it would is reusable."""
    port = _next_port()
    server = await serve(
        _origin(), ServerConfig(host="127.0.0.1", port=port, lifespan="off"))
    client = HTTPClient("o", base_url=f"http://127.0.0.1:{port}", destination=_LOCAL)
    await client.start()
    try:
        for _ in range(3):
            async with client.stream("GET", "/small") as response:
                async for _chunk in response.iter_bytes():
                    pass
        assert client.snapshot().reused >= 2
    finally:
        await client.close()
        await server.close()


async def test_an_unread_stream_body_does_not_poison_the_pool() -> None:
    """Leaving the body unread must close the connection, never pool it.

    The socket is mid-message, so returning it would hand the next caller the
    remainder of someone else's response as the head of theirs.
    """
    port = _next_port()
    server = await serve(
        _origin(), ServerConfig(host="127.0.0.1", port=port, lifespan="off"))
    client = HTTPClient("o", base_url=f"http://127.0.0.1:{port}", destination=_LOCAL)
    await client.start()
    try:
        async with client.stream("GET", "/big") as response:
            assert response.status == 200          # head read, body abandoned
        after = await client.request("GET", "/small")
        assert after.status == 200
        assert after.body == b'{"ok":true}'
    finally:
        await client.close()
        await server.close()


async def test_streaming_works_from_inside_a_handler() -> None:
    """The trap this hit: the native driver steps a request before it owns a Task.

    `asyncio.timeout` needs a current task, so the first streaming call from a
    handler died on "Timeout should be used inside a task" while the buffered
    call beside it worked -- `_send_with_retries` already carried the
    `sleep(0)` guard and the streaming path did not.
    """
    upstream_port, edge_port = _next_port(), _next_port()
    upstream = await serve(
        _origin(), ServerConfig(host="127.0.0.1", port=upstream_port, lifespan="off"))
    client = HTTPClient(
        "o", base_url=f"http://127.0.0.1:{upstream_port}", destination=_LOCAL)
    await client.start()

    edge = Wreath()

    @edge.get("/relay")
    async def relay(request: Request) -> dict:
        async with client.stream("GET", "/small") as response:
            body = b"".join([c async for c in response.iter_bytes()])
        return {"status": response.status, "len": len(body)}

    edge_server = await serve(
        edge, ServerConfig(host="127.0.0.1", port=edge_port, lifespan="off"))
    probe = HTTPClient(
        "p", base_url=f"http://127.0.0.1:{edge_port}", destination=_LOCAL)
    await probe.start()
    try:
        response = await probe.request("GET", "/relay")
        assert response.status == 200
        assert b'"status":200' in response.body
    finally:
        await probe.close()
        await client.close()
        await edge_server.close()
        await upstream.close()


# --- stage 2: header hygiene -------------------------------------------------


def test_hop_by_hop_and_connection_named_fields_are_dropped() -> None:
    """RFC 9110 §7.6.1, plus whatever `Connection` names for this message."""
    headers = (
        (b"host", b"example.test"),
        (b"connection", b"close, x-hop"),
        (b"x-hop", b"1"),
        (b"transfer-encoding", b"chunked"),
        (b"accept", b"*/*"),
    )
    survived = dict(forwardable(headers))
    assert b"x-hop" not in survived          # named by Connection
    assert b"connection" not in survived
    assert b"transfer-encoding" not in survived
    assert survived[b"accept"] == b"*/*"
    assert all(name not in survived for name in HOP_BY_HOP)


def test_a_client_supplied_forwarding_header_is_replaced_not_appended() -> None:
    """Appending is the spoof: the attacker writes the first element.

    Every parser that reads "the client" reads the leftmost value, so a proxy
    that appends lets the caller choose what the origin believes about them.
    """
    headers = ((b"x-forwarded-for", b"9.9.9.9"), (b"forwarded", b'for="9.9.9.9"'))
    assert forwardable(headers) == []


async def test_the_proxy_rewrites_forwarding_headers_and_host() -> None:
    upstream_port, edge_port = _next_port(), _next_port()
    upstream = await serve(
        _origin(), ServerConfig(host="127.0.0.1", port=upstream_port, lifespan="off"))
    url = f"http://127.0.0.1:{upstream_port}"
    client = HTTPClient(url, base_url=url, destination=_LOCAL)
    await client.start()
    proxy = ReverseProxy(UpstreamPool([Upstream(url)]), {url: client}, via_name="edge")

    edge = Wreath()

    @edge.get("/{path:path}")
    async def relay(request: Request, path: str):
        return await proxy(request)

    edge_server = await serve(
        edge, ServerConfig(host="127.0.0.1", port=edge_port, lifespan="off"))
    probe = HTTPClient(
        "p", base_url=f"http://127.0.0.1:{edge_port}", destination=_LOCAL)
    await probe.start()
    try:
        import json

        response = await probe.request("GET", "/echo", headers=(
            (b"connection", b"close, x-hop"),
            (b"x-hop", b"leaked"),
            (b"x-forwarded-for", b"9.9.9.9"),
        ))
        seen = json.loads(response.body)
        assert seen["hop"] is None                     # dropped by Connection
        assert seen["xff"] == "127.0.0.1"              # the real peer, not the spoof
        assert seen["via"] == "1.1 edge"
        # Host is the upstream's authority: relaying the client's would ask one
        # origin to answer for another's name.
        assert seen["host"] == f"127.0.0.1:{upstream_port}"
    finally:
        await probe.close()
        await client.close()
        await edge_server.close()
        await upstream.close()


# --- stage 3: the pool -------------------------------------------------------


def test_every_upstream_is_tried_before_any_is_scored() -> None:
    """A cold upstream must not starve.

    Its latency starts at a *guess*, and the moment a warm upstream measures
    faster than that guess the cold one stops being selected -- so it never gets
    a measurement to replace the guess with. Caught by a two-origin pool sending
    40 of 40 requests to one origin while the other showed exactly the cold
    default and a request count of zero.
    """
    pool = UpstreamPool([Upstream("a"), Upstream("b"), Upstream("c")])
    assert sorted(pool.choose().url for _ in range(3)) == ["a", "b", "c"]


def test_the_pool_prefers_the_faster_upstream_once_both_are_warm() -> None:
    pool = UpstreamPool([Upstream("fast"), Upstream("slow")], policy="ewma")
    fast, slow = pool.upstreams
    for _ in range(5):
        fast.total = slow.total = 1
        pool.succeeded(fast, 0.001)
        pool.succeeded(slow, 0.100)
    assert pool.choose().url == "fast"


def test_an_upstream_is_ejected_after_consecutive_failures_and_comes_back() -> None:
    """Ejection is a cooldown, not a removal: it has to be probed back."""
    pool = UpstreamPool([Upstream("a"), Upstream("b")],
                        ejection=Ejection(failures=2, seconds=10.0))
    bad = pool.upstreams[0]
    pool.failed(bad, now=100.0)
    assert bad.healthy(100.0)                    # one failure is not enough
    pool.failed(bad, now=100.0)
    assert not bad.healthy(100.0)
    assert bad.healthy(111.0)                    # the cooldown expires
    assert pool.stats()["upstreams"] == 2


def test_a_pool_with_everything_ejected_still_answers() -> None:
    """Refusing while every origin is briefly ejected is an outage of our own.

    The request declined is the one that would have proved recovery.
    """
    pool = UpstreamPool([Upstream("a")], ejection=Ejection(failures=1))
    pool.failed(pool.upstreams[0], now=100.0)
    assert not pool.upstreams[0].healthy(100.0)
    assert pool.choose(now=100.0).url == "a"


def test_a_pool_refuses_an_upstream_it_has_no_client_for() -> None:
    """At construction, not at the first request that happens to pick it."""
    pool = UpstreamPool([Upstream("http://a"), Upstream("http://b")])
    with pytest.raises(ValueError, match="no client for upstream"):
        ReverseProxy(pool, {"http://a": object()})  # type: ignore[dict-item]


# --- retry across upstreams --------------------------------------------------


async def test_an_idempotent_request_survives_a_dead_upstream() -> None:
    """One bad origin in the pool must not reach the client as a 502.

    This is the difference between a demo and a proxy. Before the retry the same
    arrangement returned a 502 for every request that happened to select the
    dead origin, which is a failure the client can see and the proxy could have
    absorbed.
    """
    live_port, dead_port, edge_port = _next_port(), _next_port(), _next_port()
    origin = await serve(
        _origin(), ServerConfig(host="127.0.0.1", port=live_port, lifespan="off"))
    urls = [f"http://127.0.0.1:{dead_port}", f"http://127.0.0.1:{live_port}"]
    pool = UpstreamPool([Upstream(u) for u in urls], policy="round-robin")
    clients = {}
    for url in urls:
        client = HTTPClient(url, base_url=url, destination=_LOCAL)
        await client.start()
        clients[url] = client
    proxy = ReverseProxy(pool, clients)

    edge = Wreath()

    @edge.get("/{path:path}")
    async def relay(request: Request, path: str):
        return await proxy(request)

    edge_server = await serve(
        edge, ServerConfig(host="127.0.0.1", port=edge_port, lifespan="off"))
    probe = HTTPClient(
        "p", base_url=f"http://127.0.0.1:{edge_port}", destination=_LOCAL)
    await probe.start()
    try:
        for _ in range(10):
            assert (await probe.request("GET", "/small")).status == 200
        dead = next(u for u in pool.upstreams if str(dead_port) in u.url)
        assert not dead.healthy(__import__("time").monotonic())
    finally:
        await probe.close()
        for client in clients.values():
            await client.close()
        await edge_server.close()
        await origin.close()


async def test_a_post_is_not_replayed_on_a_second_upstream() -> None:
    """A POST that may have been received once must not be sent twice.

    RFC 9110 §9.2.2: only the idempotent methods are defined to have the same
    effect applied once or several times. A failure *after* the request reached
    the origin is indistinguishable here from one before it, so retrying a POST
    risks a second order rather than a second attempt. An honest 502 is the
    right answer, and the client can decide.
    """
    dead_port, edge_port = _next_port(), _next_port()
    url = f"http://127.0.0.1:{dead_port}"
    pool = UpstreamPool([Upstream(url)])
    client = HTTPClient(url, base_url=url, destination=_LOCAL)
    await client.start()
    proxy = ReverseProxy(pool, {url: client})

    edge = Wreath()

    @edge.post("/{path:path}")
    async def relay(request: Request, path: str):
        return await proxy(request)

    edge_server = await serve(
        edge, ServerConfig(host="127.0.0.1", port=edge_port, lifespan="off"))
    probe = HTTPClient(
        "p", base_url=f"http://127.0.0.1:{edge_port}", destination=_LOCAL)
    await probe.start()
    try:
        response = await probe.request("POST", "/small", body=b"{}")
        assert response.status == 502
        # One attempt, not two: the upstream was tried once and given up on.
        assert pool.upstreams[0].failures == 1
    finally:
        await probe.close()
        await client.close()
        await edge_server.close()


async def test_the_outbound_framing_is_recomputed_not_relayed() -> None:
    """The egress half of re-framing, which is the smuggling defence.

    A proxy that forwards the client's `Content-Length` lets the pair disagree
    about where one message ends and the next begins. The outbound length has to
    describe what this proxy actually sends -- and `transfer-encoding` is
    already gone as hop-by-hop, so nothing about the inbound framing survives.
    """
    origin_port, edge_port = _next_port(), _next_port()
    app = Wreath()

    @app.post("/size")
    async def size(request: Request) -> dict:
        return {"len": len(await request.body())}

    origin = await serve(
        app, ServerConfig(host="127.0.0.1", port=origin_port, lifespan="off"))
    url = f"http://127.0.0.1:{origin_port}"
    client = HTTPClient(url, base_url=url, destination=_LOCAL)
    await client.start()
    proxy = ReverseProxy(UpstreamPool([Upstream(url)]), {url: client})

    edge = Wreath()

    @edge.post("/{path:path}")
    async def relay(request: Request, path: str):
        return await proxy(request)

    edge_server = await serve(
        edge, ServerConfig(host="127.0.0.1", port=edge_port, lifespan="off"))
    probe = HTTPClient(
        "p", base_url=f"http://127.0.0.1:{edge_port}", destination=_LOCAL)
    await probe.start()
    try:
        response = await probe.request("POST", "/size", body=b'{"a":1}')
        assert response.status == 200
        assert response.body == b'{"len":7}'
    finally:
        await probe.close()
        await client.close()
        await edge_server.close()
        await origin.close()
