"""Graceful shutdown via GOAWAY (RFC 9113 s6.8)."""
from __future__ import annotations

import asyncio

import pytest

from . import support
from .conftest import ok_app, requires_h2

pytestmark = [requires_h2, pytest.mark.asyncio]


async def test_stop_accepting_sends_goaway(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    d.frames()
    # Ask the protocol to stop accepting new requests (server-driven shutdown).
    d.protocol.stop_accepting()
    await d.settle()
    goaways = [f for f in d.frames() if f.type == support.GOAWAY]
    assert goaways, "stop_accepting must emit GOAWAY"
    last_stream, code, _ = support.parse_goaway(goaways[-1].payload)
    assert code == support.NO_ERROR
    assert last_stream >= 1  # last processed stream id


async def test_new_stream_after_goaway_is_refused(make_driver):
    active = []

    async def app(scope, receive, send):
        active.append(scope)
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return

    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(), end_stream=False))
    d.protocol.stop_accepting()
    await d.settle()
    d.frames()
    # A stream created after GOAWAY (id above last-processed) must be refused.
    await d.feed_and_settle(support.build_headers_frame(5, support.request_headers()))
    refused = [f for f in d.frames() if f.type == support.RST_STREAM and f.stream_id == 5]
    assert refused
    assert int.from_bytes(refused[-1].payload, "big") == support.REFUSED_STREAM


async def test_accepted_streams_drain_before_close(make_driver):
    release = asyncio.Event()

    async def app(scope, receive, send):
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})

    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    d.protocol.stop_accepting()
    await d.settle()
    # The accepted stream is still in flight; complete it now.
    release.set()
    await d.settle()
    responded = [f for f in d.frames() if f.type == support.HEADERS and f.stream_id == 1]
    assert responded, "an accepted stream must be allowed to finish after GOAWAY"
