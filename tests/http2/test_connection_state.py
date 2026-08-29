from __future__ import annotations

import pytest

from . import support
from .conftest import ok_app, requires_h2, scope_capture_app

pytestmark = [requires_h2, pytest.mark.asyncio]


def _goaway_code(d):
    goaways = [f for f in d.frames() if f.type == support.GOAWAY]
    assert goaways, "expected a GOAWAY"
    _, code, _ = support.parse_goaway(goaways[-1].payload)
    return code


def _rst_code(d, stream_id):
    rsts = [f for f in d.frames() if f.type == support.RST_STREAM and f.stream_id == stream_id]
    assert rsts, f"expected RST_STREAM on stream {stream_id}"
    return int.from_bytes(rsts[-1].payload, "big")


async def test_settings_ack_flag_with_payload_is_frame_size_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(
        support.encode_frame(support.SETTINGS, support.FLAG_ACK, 0, b"\x00\x03\x00\x00\x00\x64")
    )
    assert _goaway_code(d) == support.FRAME_SIZE_ERROR


async def test_settings_non_multiple_of_six_is_frame_size_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.encode_frame(support.SETTINGS, 0, 0, b"\x00\x03\x00"))
    assert _goaway_code(d) == support.FRAME_SIZE_ERROR


async def test_settings_on_nonzero_stream_is_protocol_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.encode_frame(support.SETTINGS, 0, 1, b""))
    assert _goaway_code(d) == support.PROTOCOL_ERROR


async def test_settings_enable_push_invalid_value_is_protocol_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.encode_settings({support.SETTINGS_ENABLE_PUSH: 2}))
    assert _goaway_code(d) == support.PROTOCOL_ERROR


async def test_settings_initial_window_too_large_is_flow_control_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(
        support.encode_settings({support.SETTINGS_INITIAL_WINDOW_SIZE: 1 << 31})
    )
    assert _goaway_code(d) == support.FLOW_CONTROL_ERROR


async def test_settings_max_frame_size_out_of_range_is_protocol_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.encode_settings({support.SETTINGS_MAX_FRAME_SIZE: 100}))
    assert _goaway_code(d) == support.PROTOCOL_ERROR


async def test_server_disables_push(make_driver):
    d = make_driver(ok_app)
    d.connection_made()
    await d.settle()
    settings_frames = [f for f in d.frames() if f.type == support.SETTINGS]
    assert settings_frames
    parsed = support.parse_settings(settings_frames[0].payload)
    assert parsed.get(support.SETTINGS_ENABLE_PUSH, 0) == 0


async def test_headers_with_continuation_fragments(make_driver):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    block = support.HpackEncoder().encode(
        support.request_headers(extra=[(b"x-a", b"1"), (b"x-b", b"2")])
    )
    mid = len(block) // 2
    headers = support.encode_frame(support.HEADERS, 0, 1, block[:mid])  # no END_HEADERS
    cont = support.encode_frame(support.CONTINUATION, support.FLAG_END_HEADERS, 1, block[mid:])
    await d.feed_and_settle(headers)
    await d.feed_and_settle(cont)
    assert len(captured) == 1


async def test_interleaved_frame_during_header_block_is_protocol_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    block = support.HpackEncoder().encode(support.request_headers())
    mid = len(block) // 2
    await d.feed_and_settle(support.encode_frame(support.HEADERS, 0, 1, block[:mid]))
    # A DATA frame between HEADERS and its CONTINUATION is illegal.
    await d.feed_and_settle(support.encode_frame(support.DATA, 0, 1, b"x"))
    assert _goaway_code(d) == support.PROTOCOL_ERROR


async def test_continuation_on_wrong_stream_is_protocol_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    block = support.HpackEncoder().encode(support.request_headers())
    mid = len(block) // 2
    await d.feed_and_settle(support.encode_frame(support.HEADERS, 0, 1, block[:mid]))
    await d.feed_and_settle(
        support.encode_frame(support.CONTINUATION, support.FLAG_END_HEADERS, 3, block[mid:])
    )
    assert _goaway_code(d) == support.PROTOCOL_ERROR


async def test_data_on_idle_stream_is_protocol_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.encode_frame(support.DATA, 0, 1, b"hello"))
    assert _goaway_code(d) == support.PROTOCOL_ERROR


async def test_data_after_end_stream_is_stream_closed(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    # Complete request (END_STREAM), then send more DATA on the half-closed stream.
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    await d.feed_and_settle(support.encode_frame(support.DATA, 0, 1, b"late"))
    # Either a stream error (RST_STREAM) or connection error is acceptable per RFC.
    frames = d.frames()
    rst = [f for f in frames if f.type == support.RST_STREAM and f.stream_id == 1]
    goaway = [f for f in frames if f.type == support.GOAWAY]
    assert rst or goaway


async def test_rst_stream_on_idle_stream_is_protocol_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.encode_rst_stream(1, support.CANCEL))
    assert _goaway_code(d) == support.PROTOCOL_ERROR


async def test_rst_stream_wrong_length_is_frame_size_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    await d.feed_and_settle(support.encode_frame(support.RST_STREAM, 0, 1, b"\x00\x00"))
    assert _goaway_code(d) == support.FRAME_SIZE_ERROR


async def test_client_rst_stream_cancels_request(make_driver):
    # An app that never finishes; the client resets the stream.
    started = []

    async def slow(scope, receive, send):
        started.append(scope)
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return

    d = make_driver(slow)
    await d.preface()
    await d.feed_and_settle(
        support.build_headers_frame(1, support.request_headers(), end_stream=False)
    )
    await d.feed_and_settle(support.encode_rst_stream(1, support.CANCEL))
    assert started, "request should have started before reset"
    # No GOAWAY: a stream reset is not a connection error.
    assert not [f for f in d.frames() if f.type == support.GOAWAY]
