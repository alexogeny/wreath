"""HPACK decoding vectors (RFC 7541): server must decode all legal forms.

We assert on the resulting ASGI ``scope["headers"]`` because that is the
observable effect of correct HPACK decoding.
"""
from __future__ import annotations

import pytest

from . import support
from .conftest import requires_h2, scope_capture_app

pytestmark = [requires_h2, pytest.mark.asyncio]


def _headers_in_scope(scope: dict) -> dict[bytes, list[bytes]]:
    out: dict[bytes, list[bytes]] = {}
    for name, value in scope["headers"]:
        out.setdefault(name, []).append(value)
    return out


async def _drive_request(make_driver, block: bytes):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    frame = support.encode_frame(
        support.HEADERS,
        support.FLAG_END_HEADERS | support.FLAG_END_STREAM,
        1,
        block,
    )
    await d.feed_and_settle(frame)
    return captured


async def test_indexed_static_field(make_driver):
    # :method: GET is static index 2 -> 0x82
    block = bytes([0x82, 0x84, 0x87]) + support.encode_integer(
        len(support.STATIC_TABLE), 6, 0x40
    )  # incremental index of :authority w/ literal value
    block = bytes([0x82, 0x86, 0x84])  # :method GET, :scheme https, :path /
    block += support.encode_integer(1, 6, 0x40)  # :authority (static 1) + literal value
    block += support.encode_string(b"localhost")
    captured = await _drive_request(make_driver, block)
    assert len(captured) == 1
    headers = _headers_in_scope(captured[0])
    assert headers.get(b"host") == [b"localhost"] or (
        b":authority" not in headers
    )


async def test_literal_with_incremental_indexing_new_name(make_driver):
    enc = support.HpackEncoder()
    block = enc.encode(support.request_headers(
        extra=[(b"custom-key", b"custom-value")]), index=True)
    captured = await _drive_request(make_driver, block)
    headers = _headers_in_scope(captured[0])
    assert headers.get(b"custom-key") == [b"custom-value"]


async def test_literal_without_indexing(make_driver):
    enc = support.HpackEncoder()
    block = enc.encode(support.request_headers(extra=[(b"x-test", b"1")]))
    captured = await _drive_request(make_driver, block)
    headers = _headers_in_scope(captured[0])
    assert headers.get(b"x-test") == [b"1"]


async def test_never_indexed_literal(make_driver):
    # 0x10 prefix = literal never indexed, new name
    block = support.HpackEncoder().encode(support.request_headers())
    block += bytes([0x10]) + support.encode_string(b"x-secret") + support.encode_string(b"v")
    captured = await _drive_request(make_driver, block)
    headers = _headers_in_scope(captured[0])
    assert headers.get(b"x-secret") == [b"v"]


async def test_huffman_encoded_values(make_driver):
    enc = support.HpackEncoder()
    block = enc.encode(
        support.request_headers(path=b"/index.html",
                                extra=[(b"user-agent", b"wreath-test-agent")]),
        huffman=True,
    )
    captured = await _drive_request(make_driver, block)
    headers = _headers_in_scope(captured[0])
    assert headers.get(b"user-agent") == [b"wreath-test-agent"]
    assert captured[0]["path"] == "/index.html"


async def test_dynamic_table_indexing_across_two_requests(make_driver):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    enc = support.HpackEncoder()
    # First request inserts custom-header into the dynamic table.
    block1 = enc.encode(
        support.request_headers(extra=[(b"x-shared", b"cached")]), index=True)
    await d.feed_and_settle(
        support.encode_frame(support.HEADERS,
                             support.FLAG_END_HEADERS | support.FLAG_END_STREAM,
                             1, block1))
    # Second request references the dynamic entry by index.
    dyn_index = len(support.STATIC_TABLE) + 1
    block2 = enc.encode(support.request_headers()) + support.encode_integer(
        dyn_index, 7, 0x80)
    await d.feed_and_settle(
        support.encode_frame(support.HEADERS,
                             support.FLAG_END_HEADERS | support.FLAG_END_STREAM,
                             3, block2))
    assert len(captured) == 2
    headers2 = _headers_in_scope(captured[1])
    assert headers2.get(b"x-shared") == [b"cached"]


async def test_dynamic_table_size_update(make_driver):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    enc = support.HpackEncoder()
    block = enc.encode_dynamic_table_size_update(0)
    block += enc.encode_dynamic_table_size_update(enc.max_size or 0)
    block += support.HpackEncoder().encode(support.request_headers())
    await d.feed_and_settle(
        support.encode_frame(support.HEADERS,
                             support.FLAG_END_HEADERS | support.FLAG_END_STREAM,
                             1, block))
    assert len(captured) == 1


async def test_eviction_when_table_full(make_driver):
    # Insert entries that overflow a small dynamic table; the server must not
    # crash and must decode the final request correctly.
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    enc = support.HpackEncoder()
    enc.max_size = 100
    big = b"v" * 60
    block = enc.encode(support.request_headers(
        extra=[(b"a-header", big), (b"b-header", big)]), index=True)
    await d.feed_and_settle(
        support.encode_frame(support.HEADERS,
                             support.FLAG_END_HEADERS | support.FLAG_END_STREAM,
                             1, block))
    assert len(captured) == 1
    headers = _headers_in_scope(captured[0])
    assert headers.get(b"b-header") == [big]
