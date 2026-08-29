from __future__ import annotations

import pytest

from . import support
from .conftest import ok_app, requires_h2, scope_capture_app

pytestmark = [requires_h2, pytest.mark.asyncio]


async def _server_frames_after_preface(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    return d


async def test_server_emits_settings_as_first_frame(make_driver):
    d = make_driver(ok_app)
    d.connection_made()
    await d.settle()
    frames = d.frames()
    assert frames, "server must send a SETTINGS preface"
    assert frames[0].type == support.SETTINGS
    assert frames[0].flags & support.FLAG_ACK == 0
    assert frames[0].stream_id == 0


async def test_server_acks_client_settings(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    frames = d.frames()
    assert any(f.type == support.SETTINGS and f.flags & support.FLAG_ACK for f in frames), (
        "server must ACK the client SETTINGS"
    )


@pytest.mark.parametrize("truncate", list(range(1, len(support.PREFACE))))
async def test_truncated_preface_prefixes_do_not_start_connection(make_driver, truncate):
    # Every truncated prefix of the client preface must not be accepted as a
    # full preface; the server must not process application frames.
    d = make_driver(ok_app)
    d.connection_made()
    await d.settle()
    d.feed(support.PREFACE[:truncate])
    await d.settle()
    # No HEADERS/DATA response can appear (no request was ever delivered).
    frames = d.frames()
    assert all(f.type not in (support.DATA,) for f in frames)


async def test_wrong_preface_is_rejected(make_driver):
    d = make_driver(ok_app)
    d.connection_made()
    await d.settle()
    d.feed(b"GET / HTTP/1.1\r\n\r\n" + b"\x00" * 8)
    await d.settle()
    assert d.transport.closed or d.transport.aborted


@pytest.mark.parametrize("split", list(range(1, 9)))
async def test_frame_header_split_at_every_byte(make_driver, split):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    frame = support.build_headers_frame(1, support.request_headers())
    # split the 9-byte header (and the rest) at `split`
    await d.feed_and_settle(frame[:split])
    await d.feed_and_settle(frame[split:])
    assert len(captured) == 1
    resp = [f for f in d.frames() if f.type == support.HEADERS]
    assert resp, "a response HEADERS frame must be emitted"


@pytest.mark.parametrize("split", list(range(1, 40)))
async def test_headers_payload_split_at_every_byte(make_driver, split):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    frame = support.build_headers_frame(1, support.request_headers())
    if split >= len(frame):
        pytest.skip("split beyond frame length")
    await d.feed_and_settle(frame[:split])
    await d.feed_and_settle(frame[split:])
    assert len(captured) == 1


async def test_reserved_bit_in_stream_id_is_ignored(make_driver):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    block = support.HpackEncoder().encode(support.request_headers())
    # set the reserved high bit on the stream identifier
    raw = support.encode_frame(
        support.HEADERS, support.FLAG_END_HEADERS | support.FLAG_END_STREAM, 1, block
    )
    raw = raw[:5] + bytes([raw[5] | 0x80]) + raw[6:]
    await d.feed_and_settle(raw)
    assert len(captured) == 1


async def test_even_client_stream_id_is_connection_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(2, support.request_headers()))
    goaways = [f for f in d.frames() if f.type == support.GOAWAY]
    assert goaways, "even client-initiated stream id must be a connection error"
    _, code, _ = support.parse_goaway(goaways[-1].payload)
    assert code == support.PROTOCOL_ERROR


async def test_stream_ids_must_monotonically_increase(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(3, support.request_headers()))
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    goaways = [f for f in d.frames() if f.type == support.GOAWAY]
    assert goaways
    _, code, _ = support.parse_goaway(goaways[-1].payload)
    assert code == support.PROTOCOL_ERROR


async def test_oversized_frame_is_frame_size_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    # A frame larger than the advertised SETTINGS_MAX_FRAME_SIZE (default 16384).
    payload = b"\x00" * (support.DEFAULT_MAX_FRAME_SIZE + 1)
    await d.feed_and_settle(support.encode_frame(support.DATA, 0, 1, payload))
    goaways = [f for f in d.frames() if f.type == support.GOAWAY]
    assert goaways
    _, code, _ = support.parse_goaway(goaways[-1].payload)
    assert code in (support.FRAME_SIZE_ERROR, support.PROTOCOL_ERROR)


async def test_unknown_frame_type_is_ignored(make_driver):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.encode_frame(0xEF, 0, 0, b"\x01\x02\x03"))
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    assert len(captured) == 1, "unknown frame must be ignored, not fatal"


async def test_ping_is_acked_with_same_payload(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    d.frames()  # drain preface frames
    opaque = b"12345678"
    await d.feed_and_settle(support.encode_ping(opaque))
    pings = [f for f in d.frames() if f.type == support.PING]
    assert pings
    assert pings[-1].flags & support.FLAG_ACK
    assert pings[-1].payload == opaque


async def test_ping_ack_is_not_reacked(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    d.frames()
    await d.feed_and_settle(support.encode_ping(b"abcdefgh", ack=True))
    pings = [f for f in d.frames() if f.type == support.PING]
    assert not pings, "a PING ACK must not be answered"


async def test_ping_wrong_length_is_frame_size_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.encode_frame(support.PING, 0, 0, b"short"))
    goaways = [f for f in d.frames() if f.type == support.GOAWAY]
    assert goaways
    _, code, _ = support.parse_goaway(goaways[-1].payload)
    assert code == support.FRAME_SIZE_ERROR


async def test_ping_on_nonzero_stream_is_protocol_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.encode_frame(support.PING, 0, 1, b"12345678"))
    goaways = [f for f in d.frames() if f.type == support.GOAWAY]
    assert goaways
    _, code, _ = support.parse_goaway(goaways[-1].payload)
    assert code == support.PROTOCOL_ERROR
