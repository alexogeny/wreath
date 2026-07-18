"""A QUIC connection must keep serving past its initial stream budget.

`initial_max_streams_bidi` is a *budget*, not a concurrency limit: it is how
many streams the peer may ever open, and it only replenishes when the server
sends MAX_STREAMS. HTTP/3 opens one bidirectional stream per request, so a
server that never extends it serves exactly `max_concurrent_streams` requests
on a connection and then stalls -- the next request waits for a stream it will
never be granted. With the default of 100, a browser holding a connection open
broke at request 101.

Why this uses h2load rather than curl
-------------------------------------
Every other HTTP/3 test here drives curl, and curl **cannot detect this bug**:
when the stream budget runs out it transparently opens a new connection and
retries, so each request gets a fresh budget and every one succeeds. Measured
against the unfixed server with a budget of 4: curl reported 20/20 OK, while
h2load on one connection reported 4 done and 16 errored.

So this test needs a client that keeps one connection and reports the failure
instead of papering over it. That is h2load with `-c 1`, and the test skips
when it is unavailable rather than quietly proving nothing.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from wreath.server import ServerConfig, TLSConfig, serve

from .conftest import make_self_signed_cert, requires_h3

pytestmark = [requires_h3, pytest.mark.asyncio]

#: Small, so exhausting it is fast and the arithmetic is obvious.
BUDGET = 4
_DONE = re.compile(r"requests:\s+\d+\s+total,\s+\d+\s+started,\s+(\d+)\s+done,"
                   r"\s+(\d+)\s+succeeded,\s+(\d+)\s+failed")


def _h2load_with_http3() -> str | None:
    from benchmarks.h2load import capabilities

    found = capabilities()
    return found.path if found is not None and found.http3 else None


requires_h2load_h3 = pytest.mark.skipif(
    _h2load_with_http3() is None,
    reason="needs an h2load built with HTTP/3 (see benchmarks/README.md)",
)


async def _serve(app, **config):
    cert, key = make_self_signed_cert()
    server = await serve(
        app,
        ServerConfig(host="127.0.0.1", port=0, lifespan="off", protocols=("h3",),
                     **config),
        tls=TLSConfig(cert, key),
    )
    return server, server.datagram_addresses[0][1]


async def _ok_app(scope, receive, send) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


async def _one_connection(port: int, count: int) -> tuple[int, int, int]:
    """`count` requests over exactly one QUIC connection. Returns (done, ok, failed)."""
    binary = _h2load_with_http3()
    assert binary is not None
    proc = await asyncio.create_subprocess_exec(
        binary, "--h3", "-n", str(count), "-c", "1", f"https://127.0.0.1:{port}/",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    match = _DONE.search(out.decode("utf-8", "replace"))
    assert match is not None, f"could not parse h2load output: {out[:400]!r}"
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


@requires_h2load_h3
@pytest.mark.network
async def test_a_connection_serves_past_its_initial_stream_budget() -> None:
    count = BUDGET * 5
    server, port = await _serve(_ok_app, max_concurrent_streams=BUDGET)
    try:
        done, succeeded, failed = await _one_connection(port, count)
        assert (done, succeeded, failed) == (count, count, 0), (
            f"one connection served {succeeded} of {count} requests with a budget of "
            f"{BUDGET}; the stream budget is not being extended when a stream closes"
        )
    finally:
        await server.close()


@requires_h2load_h3
@pytest.mark.network
async def test_the_budget_is_concurrency_not_a_lifetime_cap() -> None:
    # `max_concurrent_streams` must mean concurrent: a connection is not
    # entitled to exactly that many requests and no more.
    server, port = await _serve(_ok_app, max_concurrent_streams=1)
    try:
        done, succeeded, failed = await _one_connection(port, 10)
        assert (done, succeeded, failed) == (10, 10, 0)
    finally:
        await server.close()


@requires_h2load_h3
@pytest.mark.network
async def test_the_default_budget_survives_a_long_lived_connection() -> None:
    # The shipped default is 100, which is exactly where this used to break.
    server, port = await _serve(_ok_app)
    try:
        done, succeeded, failed = await _one_connection(port, 250)
        assert (done, succeeded, failed) == (250, 250, 0)
    finally:
        await server.close()
