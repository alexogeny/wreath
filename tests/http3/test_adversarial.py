from __future__ import annotations

import pytest

import wreath
from wreath.server import ServerConfig, TLSConfig, serve

from .conftest import curl_http3, make_self_signed_cert, requires_curl_h3

pytestmark = [requires_curl_h3, pytest.mark.network, pytest.mark.asyncio]


def _app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/ping")
    async def ping(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("pong")

    @app.post("/echo")
    async def echo(request: wreath.Request) -> dict:
        return {"n": len(await request.body())}

    @app.get("/boom")
    async def boom(request: wreath.Request) -> wreath.Response:
        raise RuntimeError("kaboom")

    return app


async def _serve():
    cert, key = make_self_signed_cert()
    server = await serve(
        _app(),
        ServerConfig(host="127.0.0.1", port=0, lifespan="off", protocols=("h3",)),
        tls=TLSConfig(cert, key),
    )
    return server, server.datagram_addresses[0][1]


async def _status(port: int, path: str, *extra: str) -> int:
    """The owned HTTP status of one adversarial HTTP/3 request."""
    rc, out = await curl_http3(port, path, "-o", "/dev/null", "-w", "%{http_code}", *extra)
    assert rc == 0, f"curl failed rc={rc} (server crashed or hung?)"
    return int(out or b"0")


async def test_healthy_baseline() -> None:
    server, port = await _serve()
    try:
        assert await _status(port, "/ping") == 200
    finally:
        await server.close()
        await server.wait_closed()


async def test_adversarial_requests_get_owned_statuses_not_crashes() -> None:
    server, port = await _serve()
    try:
        cases = {
            "/nonexistent": {404},  # no route
            "/../../etc/passwd": {400, 404},  # traversal-looking path
            "/ping/" + "x" * 4000: {400, 404, 414},  # very long path
            "/boom": {500},  # handler raises -> owned 500
        }
        for path, allowed in cases.items():
            status = await _status(port, path)
            assert status in allowed, f"{path!r} -> {status}, expected {allowed}"
        # Wrong method for a GET route.
        assert await _status(port, "/ping", "-X", "POST", "--data", "x") in {404, 405}
    finally:
        await server.close()
        await server.wait_closed()


async def test_header_flood_is_bounded_not_fatal() -> None:
    server, port = await _serve()
    try:
        headers: list[str] = []
        for i in range(200):
            headers += ["-H", f"X-Pad-{i}: {'v' * 64}"]
        status = await _status(port, "/ping", *headers)
        # Either accepted (200) or refused for oversized headers (a 4xx) — an owned
        # decision, never a crash or hang (curl rc==0 asserted in _status).
        assert status == 200 or 400 <= status < 500
    finally:
        await server.close()
        await server.wait_closed()


async def test_adversarial_outcome_is_deterministic() -> None:
    server, port = await _serve()
    try:
        first = await _status(port, "/boom")
        second = await _status(port, "/boom")
        assert first == second == 500
    finally:
        await server.close()
        await server.wait_closed()


async def test_concurrent_mixed_requests_all_get_owned_answers() -> None:
    import asyncio

    server, port = await _serve()
    try:
        results = await asyncio.gather(
            _status(port, "/ping"),
            _status(port, "/nonexistent"),
            _status(port, "/boom"),
            _status(port, "/ping"),
        )
        assert results == [200, 404, 500, 200]
    finally:
        await server.close()
        await server.wait_closed()


async def test_head_and_hostile_query_are_handled() -> None:
    server, port = await _serve()
    try:
        assert await _status(port, "/ping", "-I") == 200  # HEAD
        # A hostile-looking query string is owned request data, not a route change.
        assert await _status(port, "/ping?x=<script>&y=%00%ff&z=" + "a" * 2000) == 200
    finally:
        await server.close()
        await server.wait_closed()


async def test_request_bodies_are_read_over_quic() -> None:
    server, port = await _serve()
    try:
        # Empty body.
        rc, out = await curl_http3(port, "/echo", "-X", "POST", "--data", "")
        assert rc == 0 and out == b'{"n":0}'
        # A moderate body well under the QUIC flow-control window echoes its length.
        # (Uploads past ~64 KiB are a known flow-control limitation, not exercised
        # here — see the native H3 upload notes.)
        body = "x" * 50000
        rc, out = await curl_http3(port, "/echo", "-X", "POST", "--data", body)
        assert rc == 0 and out == b'{"n":50000}'
    finally:
        await server.close()
        await server.wait_closed()
