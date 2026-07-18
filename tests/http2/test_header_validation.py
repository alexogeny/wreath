"""Pseudo-header and header-field validation (RFC 9113 s8.3, s8.2).

Malformed request headers are a stream error of type PROTOCOL_ERROR
(RST_STREAM) and must not reach the application.
"""
from __future__ import annotations

import pytest

from . import support
from .conftest import requires_h2, scope_capture_app

pytestmark = [requires_h2, pytest.mark.asyncio]


async def _send_raw_headers(make_driver, headers, *, end_stream=True):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    block = support.HpackEncoder().encode(headers)
    flags = support.FLAG_END_HEADERS | (support.FLAG_END_STREAM if end_stream else 0)
    await d.feed_and_settle(support.encode_frame(support.HEADERS, flags, 1, block))
    return d, captured


def _stream_error(d, stream_id=1):
    frames = d.frames()
    rst = [f for f in frames if f.type == support.RST_STREAM and f.stream_id == stream_id]
    goaway = [f for f in frames if f.type == support.GOAWAY]
    if rst:
        return int.from_bytes(rst[-1].payload, "big")
    if goaway:
        return support.parse_goaway(goaway[-1].payload)[1]
    return None


async def test_missing_method_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(make_driver, [
        (b":path", b"/"), (b":scheme", b"https"), (b":authority", b"x")])
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_missing_path_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(make_driver, [
        (b":method", b"GET"), (b":scheme", b"https"), (b":authority", b"x")])
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_missing_scheme_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(make_driver, [
        (b":method", b"GET"), (b":path", b"/"), (b":authority", b"x")])
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_duplicate_pseudo_header_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(make_driver, [
        (b":method", b"GET"), (b":method", b"POST"),
        (b":path", b"/"), (b":scheme", b"https"), (b":authority", b"x")])
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_unknown_pseudo_header_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(make_driver, [
        (b":method", b"GET"), (b":path", b"/"), (b":scheme", b"https"),
        (b":authority", b"x"), (b":bogus", b"1")])
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_pseudo_header_after_regular_header_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(make_driver, [
        (b":method", b"GET"), (b":path", b"/"), (b":scheme", b"https"),
        (b"x-regular", b"1"), (b":authority", b"x")])
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_uppercase_header_name_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(make_driver, [
        (b":method", b"GET"), (b":path", b"/"), (b":scheme", b"https"),
        (b":authority", b"x"), (b"X-Uppercase", b"1")])
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_connection_specific_header_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(make_driver, [
        (b":method", b"GET"), (b":path", b"/"), (b":scheme", b"https"),
        (b":authority", b"x"), (b"connection", b"keep-alive")])
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_te_header_non_trailers_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(make_driver, [
        (b":method", b"GET"), (b":path", b"/"), (b":scheme", b"https"),
        (b":authority", b"x"), (b"te", b"gzip")])
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_te_trailers_is_allowed(make_driver):
    d, captured = await _send_raw_headers(make_driver, [
        (b":method", b"GET"), (b":path", b"/"), (b":scheme", b"https"),
        (b":authority", b"x"), (b"te", b"trailers")])
    assert captured, "te: trailers is explicitly permitted"


async def test_empty_path_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(make_driver, [
        (b":method", b"GET"), (b":path", b""), (b":scheme", b"https"),
        (b":authority", b"x")])
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_connect_without_authority_is_protocol_error(make_driver):
    # CONNECT uses :authority and omits :scheme/:path (RFC 9113 s8.5).
    d, captured = await _send_raw_headers(make_driver, [
        (b":method", b"CONNECT")])
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured
