from __future__ import annotations

import os

import pytest

from wreath import Request, Wreath
from wreath.edge import Ejection, ReverseProxy, Upstream, UpstreamPool, forwardable
from wreath.edge.headers import HOP_BY_HOP, via_token
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


@pytest.mark.parametrize(
    "ejection",
    [
        Ejection(failures=0),
        Ejection(seconds=0),
        Ejection(cap=0),
        Ejection(seconds=10, cap=5),
    ],
)
def test_upstream_pool_refuses_invalid_ejection_policy(ejection: Ejection) -> None:
    with pytest.raises(ValueError, match="ejection"):
        UpstreamPool([Upstream("http://origin.test")], ejection=ejection)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"attempts": 0}, "attempts must be at least 1"),
        ({"max_body": -1}, "max_body must be non-negative"),
        ({"buffer_below": -1}, "buffer_below must be non-negative"),
    ],
)
def test_reverse_proxy_refuses_invalid_limits(options: dict[str, int], message: str) -> None:
    upstream = Upstream("http://origin.test")
    with pytest.raises(ValueError, match=message):
        ReverseProxy(UpstreamPool([upstream]), {upstream.url: object()}, **options)
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
                yield b"z" * 65536  # 5.2 MB, past the 2 MiB buffer cap

        return StreamingResponse(gen(), headers=[(b"content-type", b"application/octet-stream")])

    @app.get("/echo")
    async def echo(request: Request) -> dict:
        return {
            "hop": request.header("x-hop"),
            "xff": request.header("x-forwarded-for"),
            "via": request.header("via"),
            "host": request.header("host"),
        }

    return app


async def test_stream_delivers_a_body_larger_than_the_buffer_cap() -> None:
    port = _next_port()
    server = await serve(_origin(), ServerConfig(host="127.0.0.1", port=port, lifespan="off"))
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
    port = _next_port()
    server = await serve(_origin(), ServerConfig(host="127.0.0.1", port=port, lifespan="off"))
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
    port = _next_port()
    server = await serve(_origin(), ServerConfig(host="127.0.0.1", port=port, lifespan="off"))
    client = HTTPClient("o", base_url=f"http://127.0.0.1:{port}", destination=_LOCAL)
    await client.start()
    try:
        async with client.stream("GET", "/big") as response:
            assert response.status == 200  # head read, body abandoned
        after = await client.request("GET", "/small")
        assert after.status == 200
        assert after.body == b'{"ok":true}'
    finally:
        await client.close()
        await server.close()


async def test_streaming_works_from_inside_a_handler() -> None:
    upstream_port, edge_port = _next_port(), _next_port()
    upstream = await serve(
        _origin(), ServerConfig(host="127.0.0.1", port=upstream_port, lifespan="off")
    )
    client = HTTPClient("o", base_url=f"http://127.0.0.1:{upstream_port}", destination=_LOCAL)
    await client.start()

    edge = Wreath()

    @edge.get("/relay")
    async def relay(request: Request) -> dict:
        async with client.stream("GET", "/small") as response:
            body = b"".join([c async for c in response.iter_bytes()])
        return {"status": response.status, "len": len(body)}

    edge_server = await serve(edge, ServerConfig(host="127.0.0.1", port=edge_port, lifespan="off"))
    probe = HTTPClient("p", base_url=f"http://127.0.0.1:{edge_port}", destination=_LOCAL)
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


def test_hop_by_hop_and_connection_named_fields_are_dropped() -> None:
    headers = (
        (b"host", b"example.test"),
        (b"connection", b"close, x-hop"),
        (b"x-hop", b"1"),
        (b"transfer-encoding", b"chunked"),
        (b"accept", b"*/*"),
    )
    survived = dict(forwardable(headers))
    assert b"x-hop" not in survived  # named by Connection
    assert b"connection" not in survived
    assert b"transfer-encoding" not in survived
    assert survived[b"accept"] == b"*/*"
    assert all(name not in survived for name in HOP_BY_HOP)


def test_only_connection_values_name_hop_by_hop_fields() -> None:
    headers = (
        (b"x-declare", b"x-innocent"),
        (b"connection", b"close, x-hop"),
        (b"x-innocent", b"kept"),
        (b"x-hop", b"dropped"),
        (b"close", b"ordinary-field"),
    )
    assert forwardable(headers) == [
        (b"x-declare", b"x-innocent"),
        (b"x-innocent", b"kept"),
        (b"close", b"ordinary-field"),
    ]


def test_a_client_supplied_forwarding_header_is_replaced_not_appended() -> None:
    headers = ((b"x-forwarded-for", b"9.9.9.9"), (b"forwarded", b'for="9.9.9.9"'))
    assert forwardable(headers) == []


@pytest.mark.parametrize("name", ["", "edge proxy", "edge\r\nx-owned: forged"])
def test_via_name_refuses_values_that_are_not_http_tokens(name: str) -> None:
    with pytest.raises(ValueError, match="via_name.*HTTP token"):
        via_token("1.1", name)


async def test_the_proxy_rewrites_forwarding_headers_and_host() -> None:
    upstream_port, edge_port = _next_port(), _next_port()
    upstream = await serve(
        _origin(), ServerConfig(host="127.0.0.1", port=upstream_port, lifespan="off")
    )
    url = f"http://127.0.0.1:{upstream_port}"
    client = HTTPClient(url, base_url=url, destination=_LOCAL)
    await client.start()
    proxy = ReverseProxy(UpstreamPool([Upstream(url)]), {url: client}, via_name="edge")

    edge = Wreath()

    @edge.get("/{path:path}")
    async def relay(request: Request, path: str):
        return await proxy(request)

    edge_server = await serve(edge, ServerConfig(host="127.0.0.1", port=edge_port, lifespan="off"))
    probe = HTTPClient("p", base_url=f"http://127.0.0.1:{edge_port}", destination=_LOCAL)
    await probe.start()
    try:
        import json

        response = await probe.request(
            "GET",
            "/echo",
            headers=(
                (b"connection", b"close, x-hop"),
                (b"x-hop", b"leaked"),
                (b"x-forwarded-for", b"9.9.9.9"),
            ),
        )
        seen = json.loads(response.body)
        assert seen["hop"] is None  # dropped by Connection
        assert seen["xff"] == "127.0.0.1"  # the real peer, not the spoof
        assert seen["via"] == "1.1 edge"
        # Host is the upstream's authority: relaying the client's would ask one
        # origin to answer for another's name.
        assert seen["host"] == f"127.0.0.1:{upstream_port}"
    finally:
        await probe.close()
        await client.close()
        await edge_server.close()
        await upstream.close()


def test_every_upstream_is_tried_before_any_is_scored() -> None:
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
    pool = UpstreamPool([Upstream("a"), Upstream("b")], ejection=Ejection(failures=2, seconds=10.0))
    bad = pool.upstreams[0]
    pool.failed(bad, now=100.0)
    assert bad.healthy(100.0)  # one failure is not enough
    pool.failed(bad, now=100.0)
    assert not bad.healthy(100.0)
    assert bad.healthy(111.0)  # the cooldown expires
    assert pool.stats()["upstreams"] == 2


def test_a_pool_with_everything_ejected_still_answers() -> None:
    pool = UpstreamPool([Upstream("a")], ejection=Ejection(failures=1))
    pool.failed(pool.upstreams[0], now=100.0)
    assert not pool.upstreams[0].healthy(100.0)
    assert pool.choose(now=100.0).url == "a"


def test_a_pool_refuses_an_upstream_it_has_no_client_for() -> None:
    pool = UpstreamPool([Upstream("http://a"), Upstream("http://b")])
    with pytest.raises(ValueError, match="no client for upstream"):
        ReverseProxy(pool, {"http://a": object()})  # type: ignore[dict-item]


async def test_an_idempotent_request_survives_a_dead_upstream() -> None:
    live_port, dead_port, edge_port = _next_port(), _next_port(), _next_port()
    origin = await serve(_origin(), ServerConfig(host="127.0.0.1", port=live_port, lifespan="off"))
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

    edge_server = await serve(edge, ServerConfig(host="127.0.0.1", port=edge_port, lifespan="off"))
    probe = HTTPClient("p", base_url=f"http://127.0.0.1:{edge_port}", destination=_LOCAL)
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

    edge_server = await serve(edge, ServerConfig(host="127.0.0.1", port=edge_port, lifespan="off"))
    probe = HTTPClient("p", base_url=f"http://127.0.0.1:{edge_port}", destination=_LOCAL)
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
    origin_port, edge_port = _next_port(), _next_port()
    app = Wreath()

    @app.post("/size")
    async def size(request: Request) -> dict:
        return {"len": len(await request.body())}

    origin = await serve(app, ServerConfig(host="127.0.0.1", port=origin_port, lifespan="off"))
    url = f"http://127.0.0.1:{origin_port}"
    client = HTTPClient(url, base_url=url, destination=_LOCAL)
    await client.start()
    proxy = ReverseProxy(UpstreamPool([Upstream(url)]), {url: client})

    edge = Wreath()

    @edge.post("/{path:path}")
    async def relay(request: Request, path: str):
        return await proxy(request)

    edge_server = await serve(edge, ServerConfig(host="127.0.0.1", port=edge_port, lifespan="off"))
    probe = HTTPClient("p", base_url=f"http://127.0.0.1:{edge_port}", destination=_LOCAL)
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
