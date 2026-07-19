"""ASGI mapping for HTTP/2 (RFC 9113 + ASGI HTTP spec).

Verifies scope shape, request body delivery, disconnect, response
start/body/trailers, application exceptions, and out-of-order multiplexing.
"""
from __future__ import annotations

import pytest

from wreath.server import ServerConfig

from . import support
from .conftest import requires_h2, scope_capture_app

pytestmark = [requires_h2, pytest.mark.asyncio]


def _decode_response(d):
    """Return {stream_id: (status, headers, body)} from server output."""
    dec = support.HpackDecoder()
    streams: dict[int, dict] = {}
    for f in d.frames():
        s = streams.setdefault(f.stream_id, {"headers": None, "body": b"", "trailers": None})
        if f.type == support.HEADERS:
            hdrs = dec.decode(f.payload)
            if s["headers"] is None:
                s["headers"] = hdrs
            else:
                s["trailers"] = hdrs
        elif f.type == support.DATA:
            s["body"] += f.payload
    return streams


async def test_scope_shape(make_driver):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(method=b"GET", path=b"/a/b?x=1",
                                   authority=b"example.com")))
    assert len(captured) == 1
    scope = captured[0]
    assert scope["type"] == "http"
    assert scope["http_version"] == "2"
    assert scope["scheme"] == "https"
    assert scope["method"] == "GET"
    assert scope["path"] == "/a/b"
    assert scope["query_string"] == b"x=1"


async def test_authority_maps_to_host_header(make_driver):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(authority=b"example.com")))
    headers = dict(captured[0]["headers"])
    assert headers.get(b"host") == b"example.com"
    # pseudo-headers must not leak into scope headers
    assert all(not name.startswith(b":") for name, _ in captured[0]["headers"])


async def test_explicit_host_header_preserved_over_authority(make_driver):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    block = support.HpackEncoder().encode([
        (b":method", b"GET"), (b":path", b"/"), (b":scheme", b"https"),
        (b":authority", b"authority.example"), (b"host", b"host.example")])
    await d.feed_and_settle(support.encode_frame(
        support.HEADERS, support.FLAG_END_HEADERS | support.FLAG_END_STREAM, 1, block))
    headers = [v for n, v in captured[0]["headers"] if n == b"host"]
    assert b"host.example" in headers


async def test_duplicate_regular_headers_preserved_in_order(make_driver):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    block = support.HpackEncoder().encode(support.request_headers(
        extra=[(b"x-multi", b"a"), (b"x-multi", b"b")]))
    await d.feed_and_settle(support.encode_frame(
        support.HEADERS, support.FLAG_END_HEADERS | support.FLAG_END_STREAM, 1, block))
    multi = [v for n, v in captured[0]["headers"] if n == b"x-multi"]
    assert multi == [b"a", b"b"]


async def test_request_body_delivered(make_driver):
    seen = []

    async def app(scope, receive, send):
        body = b""
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break
        seen.append(body)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(method=b"POST"), end_stream=False))
    await d.feed_and_settle(support.encode_frame(support.DATA, 0, 1, b"chunk1"))
    await d.feed_and_settle(support.encode_frame(support.DATA, support.FLAG_END_STREAM,
                                                 1, b"chunk2"))
    assert seen == [b"chunk1chunk2"]
    streams = _decode_response(d)
    assert streams[1]["body"] == b"chunk1chunk2"


async def test_response_status_and_body(make_driver):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 201,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"created"})

    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    streams = _decode_response(d)
    status = dict(streams[1]["headers"])[b":status"]
    assert status == b"201"
    assert streams[1]["body"] == b"created"


async def test_server_default_response_headers(make_driver):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    headers = dict(_decode_response(d)[1]["headers"])
    assert headers[b"server"] == b"wreath"
    assert b"date" in headers

    d = make_driver(
        app,
        ServerConfig(protocols=("h2",), server_header=None, date_header=False),
    )
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    headers = dict(_decode_response(d)[1]["headers"])
    assert b"server" not in headers
    assert b"date" not in headers


async def test_response_trailers(make_driver):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [],
                    "trailers": True})
        await send({"type": "http.response.body", "body": b"x", "more_body": True})
        await send({"type": "http.response.body", "body": b""})
        await send({"type": "http.response.trailers",
                    "headers": [(b"x-trailer", b"done")]})

    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    streams = _decode_response(d)
    assert streams[1]["trailers"] is not None
    assert dict(streams[1]["trailers"]).get(b"x-trailer") == b"done"


async def test_application_exception_before_start_yields_500(make_driver):
    async def app(scope, receive, send):
        raise RuntimeError("boom")

    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    streams = _decode_response(d)
    if streams.get(1, {}).get("headers"):
        assert dict(streams[1]["headers"])[b":status"] == b"500"
    else:
        # Alternatively an INTERNAL_ERROR reset is acceptable.
        rst = [f for f in d.frames() if f.type == support.RST_STREAM]
        assert rst


async def test_disconnect_delivered_on_stream_reset(make_driver):
    events = []

    async def app(scope, receive, send):
        while True:
            msg = await receive()
            events.append(msg["type"])
            if msg["type"] == "http.disconnect":
                return

    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(), end_stream=False))
    await d.feed_and_settle(support.encode_rst_stream(1, support.CANCEL))
    assert "http.disconnect" in events


async def test_multiplexed_streams_complete_out_of_order(make_driver):
    import asyncio

    fast_done = asyncio.Event()

    async def app(scope, receive, send):
        # stall /slow until /fast has responded, forcing out-of-order completion
        path = scope["path"]
        if path == "/slow":
            await fast_done.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": path.encode()})
        if path == "/fast":
            fast_done.set()

    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(path=b"/slow")))
    await d.feed_and_settle(support.build_headers_frame(
        3, support.request_headers(path=b"/fast")))
    await d.settle()
    streams = _decode_response(d)
    assert streams[1]["body"] == b"/slow"
    assert streams[3]["body"] == b"/fast"


# --- buffered request queue ------------------------------------------------
#
# The queue is an owned list plus a head index: it drops its consumed prefix in
# one slice instead of shifting on every take. These drive enough separately
# framed chunks to cross the compaction threshold, and assert only on
# app-visible behavior (order, more_body, completeness) rather than internals.

async def _queue_driver(make_driver, chunks: list[bytes]):
    """Buffer `chunks` while the app is not reading, then let it drain."""
    import asyncio

    gate = asyncio.Event()
    seen: dict = {"parts": [], "more": [], "done": asyncio.Event()}

    async def app(scope, receive, send):
        await gate.wait()  # do not read while the peer streams: force queueing
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            seen["parts"].append(msg.get("body", b""))
            seen["more"].append(msg.get("more_body", False))
            if not msg.get("more_body", False):
                break
        seen["done"].set()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(method=b"POST", path=b"/"), end_stream=False))
    for chunk in chunks:
        d.feed(support.encode_frame(support.DATA, 0, 1, chunk))
    d.feed(support.encode_frame(support.DATA, support.FLAG_END_STREAM, 1, b""))
    await d.settle()
    gate.set()
    await d.settle()
    await asyncio.wait_for(seen["done"].wait(), timeout=10)
    return d, seen


async def test_queued_body_chunks_coalesce_without_losing_order(make_driver):
    chunks = [f"{i:04d}".encode() for i in range(300)]
    _d, seen = await _queue_driver(make_driver, chunks)
    assert b"".join(seen["parts"]) == b"".join(chunks)
    assert len(seen["parts"]) < len(chunks)


async def test_queued_body_more_body_tracks_logical_queue_length(make_driver):
    chunks = [f"{i:04d}".encode() for i in range(300)]
    _d, seen = await _queue_driver(make_driver, chunks)
    # more_body stays true for every chunk that has a successor; the final
    # message (the END_STREAM empty DATA) closes the body.
    assert all(seen["more"][:-1]), "a queued chunk wrongly reported more_body=False"
    assert seen["more"][-1] is False


async def test_delivery_is_correct_across_coalescing_boundaries(make_driver):
    """Chunks larger than one coalescing block still arrive in exact byte order."""
    chunks = [bytes([index % 251]) * 1024 for index in range(32)]
    d, seen = await _queue_driver(make_driver, chunks)
    assert b"".join(seen["parts"]) == b"".join(chunks)
    assert len(seen["parts"]) >= 2
    # The stream completed normally, so the drained queue was reusable rather
    # than left holding consumed references.
    streams = _decode_response(d)
    assert streams[1]["body"] == b"ok"
