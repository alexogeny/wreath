from __future__ import annotations

import asyncio

import pytest

from wreath.server import ServerConfig, TLSConfig, serve

from .conftest import curl_http3, make_self_signed_cert, requires_curl_h3

pytestmark = [requires_curl_h3, pytest.mark.asyncio, pytest.mark.network]


async def _serve(app):
    cert, key = make_self_signed_cert()
    server = await serve(
        app,
        ServerConfig(host="127.0.0.1", port=0, lifespan="off", protocols=("h3",)),
        tls=TLSConfig(cert, key),
    )
    return server, server.datagram_addresses[0][1]


async def _echo_app(scope, receive, send):
    assert scope["type"] == "http"
    assert scope["http_version"] == "3"
    assert scope["scheme"] == "https"
    body = b""
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            return
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    if scope["path"] == "/big":
        payload = b"x" * 100_000
    else:
        payload = b"path=" + scope["path"].encode() + b";body=" + body
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def test_end_to_end_request_response():
    server, port = await _serve(_echo_app)
    try:
        rc, body = await curl_http3(port, "/hello")
        assert rc == 0, f"curl failed rc={rc}"
        assert body == b"path=/hello;body="
    finally:
        await server.close()


async def test_scope_http_version_is_3():
    captured = {}

    async def app(scope, receive, send):
        captured.update(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    server, port = await _serve(app)
    try:
        rc, body = await curl_http3(port, "/x?y=1")
        assert rc == 0
        assert body == b"ok"
        assert captured["http_version"] == "3"
        assert captured["scheme"] == "https"
        assert captured["path"] == "/x"
        assert captured["query_string"] == b"y=1"
    finally:
        await server.close()


async def test_request_body_delivered():
    server, port = await _serve(_echo_app)
    try:
        rc, body = await curl_http3(port, "/echo", "-d", "hello-body")
        assert rc == 0
        assert body == b"path=/echo;body=hello-body"
    finally:
        await server.close()


async def test_large_multi_packet_response():
    server, port = await _serve(_echo_app)
    try:
        rc, body = await curl_http3(port, "/big")
        assert rc == 0
        assert len(body) == 100_000
        assert body == b"x" * 100_000
    finally:
        await server.close()


async def test_concurrent_streams_complete():
    server, port = await _serve(_echo_app)
    try:
        results = await asyncio.gather(
            curl_http3(port, "/a"),
            curl_http3(port, "/b"),
            curl_http3(port, "/c"),
        )
        for rc, _ in results:
            assert rc == 0
        bodies = {body for _, body in results}
        assert bodies == {b"path=/a;body=", b"path=/b;body=", b"path=/c;body="}
    finally:
        await server.close()


async def test_close_drains_a_response_that_is_still_in_flight():
    started = asyncio.Event()

    async def slow_app(scope, receive, send):
        while True:
            msg = await receive()
            if not msg.get("more_body"):
                break
        started.set()
        await asyncio.sleep(0.5)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"drained"})

    server, port = await _serve(slow_app)
    request = asyncio.create_task(curl_http3(port, "/slow"))
    await asyncio.wait_for(started.wait(), timeout=5)
    loop = asyncio.get_running_loop()
    began = loop.time()
    await server.close()
    waited = loop.time() - began
    rc, body = await asyncio.wait_for(request, timeout=5)
    assert rc == 0, f"curl failed rc={rc}"
    assert body == b"drained", "close() cut off a response it should have drained"
    assert waited >= 0.3, f"close() returned in {waited:.2f}s without draining"


async def test_close_does_not_wait_out_the_timeout_for_an_idle_connection():
    server, port = await _serve(_echo_app)
    rc, _ = await curl_http3(port, "/hello")
    assert rc == 0
    loop = asyncio.get_running_loop()
    began = loop.time()
    await server.close()
    waited = loop.time() - began
    assert waited < 1.0, (
        f"close() took {waited:.2f}s with nothing in flight; "
        f"shutdown_timeout is {ServerConfig().shutdown_timeout}s"
    )


async def test_malformed_packets_never_invoke_asgi():
    invoked = []

    async def app(scope, receive, send):
        invoked.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    server, port = await _serve(app)
    try:
        loop = asyncio.get_event_loop()
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=("127.0.0.1", port)
        )
        try:
            for _ in range(20):
                transport.sendto(b"\x00\x01\x02not-a-quic-packet\xff" * 8)
            await asyncio.sleep(0.2)
        finally:
            transport.close()
        assert invoked == [], "malformed packets must never invoke ASGI"
        rc, body = await curl_http3(port, "/ok")
        assert rc == 0 and body == b"ok"
    finally:
        await server.close()


async def test_repeated_endpoint_create_close_cycles():
    for _ in range(5):
        server, port = await _serve(_echo_app)
        try:
            rc, body = await curl_http3(port, "/cycle")
            assert rc == 0
            assert body == b"path=/cycle;body="
        finally:
            await server.close()
