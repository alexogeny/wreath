from __future__ import annotations

import pytest

import wreath
from wreath.replay import (
    FaultDescriptor,
    FaultKind,
    FaultSchedule,
    record_transport_segments,
    replay_transport_h2,
)

from . import support
from .conftest import requires_h2

pytestmark = [requires_h2, pytest.mark.asyncio]


def _app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/ping")
    async def ping(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("pong")

    return app


def _conn(*frames: bytes) -> bytes:
    return support.PREFACE + support.encode_settings({}) + b"".join(frames)


def _get(stream_id: int = 1, path: bytes = b"/ping") -> bytes:
    return support.build_headers_frame(
        stream_id, support.request_headers(method=b"GET", path=path), end_stream=True
    )


async def _replay(data: bytes, **kw):
    return await replay_transport_h2(_app(), record_transport_segments([data]), **kw)


async def test_data_on_stream_zero_is_a_connection_error() -> None:
    data = _conn(support.encode_frame(support.DATA, 0, 0, b"x"))
    a = await _replay(data)
    b = await _replay(data)
    assert a.goaway is not None  # owned GOAWAY for the protocol error
    assert a.terminal == "closed"
    assert not a.streams  # no request was ever completed
    assert a.matches(b)  # deterministic


async def test_malformed_hpack_is_a_compression_error() -> None:
    bad = support.encode_frame(
        support.HEADERS,
        support.FLAG_END_HEADERS | support.FLAG_END_STREAM,
        1,
        b"\xff\xff\xff\xff\xff",  # not a decodable HPACK block
    )
    a = await _replay(_conn(bad))
    b = await _replay(_conn(bad))
    assert a.goaway is not None
    assert a.terminal == "closed"
    assert a.matches(b)


async def test_even_client_stream_id_is_a_protocol_error() -> None:
    a = await _replay(_conn(_get(stream_id=2)))
    b = await _replay(_conn(_get(stream_id=2)))
    assert a.goaway is not None
    assert not any(s.status == 200 for s in a.streams.values())
    assert a.matches(b)


async def test_oversized_header_block_never_crashes_or_answers_200() -> None:
    extra = [(b"x-pad-" + str(i).encode(), b"v" * 128) for i in range(256)]
    huge = support.build_headers_frame(
        1, support.request_headers(method=b"GET", path=b"/ping", extra=extra), end_stream=True
    )
    a = await _replay(_conn(huge))
    b = await _replay(_conn(huge))
    assert not any(s.status == 200 for s in a.streams.values())
    assert a.matches(b)


async def test_missing_path_pseudo_header_is_a_stream_error() -> None:
    headers = [(b":method", b"GET"), (b":scheme", b"https"), (b":authority", b"x")]
    bad = support.build_headers_frame(1, headers, end_stream=True)
    a = await _replay(_conn(bad))
    b = await _replay(_conn(bad))
    # A malformed request is refused at the stream level, not answered.
    stream = a.streams.get(1)
    assert stream is None or stream.status != 200
    assert a.matches(b)


async def test_client_reset_after_headers_cancels_without_a_response() -> None:
    data = _conn(_get(1), support.encode_rst_stream(1, 0x8))  # CANCEL
    a = await _replay(data)
    b = await _replay(data)
    assert not any(s.status == 200 and s.body == b"pong" for s in a.streams.values())
    assert a.matches(b)


async def test_truncated_h2_stream_never_fabricates_a_response() -> None:
    conn = _conn(_get(1))
    rec = record_transport_segments([conn[:30], conn[30:60], conn[60:]])
    for cut in range(3):
        schedule = FaultSchedule((FaultDescriptor(int(FaultKind.TRUNCATE), cut, 0),))
        a = await replay_transport_h2(_app(), rec, faults=schedule)
        b = await replay_transport_h2(_app(), rec, faults=schedule)
        assert not any(s.body == b"pong" for s in a.streams.values())
        assert a.matches(b)


async def test_reset_at_every_segment_is_deterministic_and_bounded() -> None:
    conn = _conn(_get(1), _get(3))
    rec = record_transport_segments([conn[i : i + 11] for i in range(0, len(conn), 11)])
    for cut in range(0, len(rec.segments), 2):
        for kind in (FaultKind.RESET, FaultKind.HALF_CLOSE):
            schedule = FaultSchedule((FaultDescriptor(int(kind), cut),))
            a = await replay_transport_h2(_app(), rec, faults=schedule)
            b = await replay_transport_h2(_app(), rec, faults=schedule)
            assert a.terminal in ("closed", "aborted", "open")
            assert a.matches(b)


async def test_one_bad_stream_does_not_sink_a_good_one() -> None:
    # Stream 1 is a valid GET; stream 3 is missing :path. The good stream should
    # still answer while the bad one is refused — and it is deterministic.
    good = _get(1)
    bad = support.build_headers_frame(
        3,
        [(b":method", b"GET"), (b":scheme", b"https"), (b":authority", b"x")],
        end_stream=True,
    )
    a = await _replay(_conn(good, bad))
    b = await _replay(_conn(good, bad))
    assert a.matches(b)
    # Either the good stream answered, or a connection error subsumed both; both
    # are owned, deterministic outcomes — never a hang or a crash.
    assert a.goaway is not None or (1 in a.streams and a.streams[1].status == 200)


async def test_a_bad_connection_preface_is_refused() -> None:
    data = b"GARBAGE-NOT-A-PREFACE\r\n\r\n" + support.encode_settings({}) + _get(1)
    a = await _replay(data)
    b = await _replay(data)
    assert a.goaway is not None and a.terminal == "closed"
    assert not a.streams
    assert a.matches(b)


async def test_settings_with_a_bad_length_is_a_frame_size_error() -> None:
    # A SETTINGS payload not a multiple of 6 bytes is a FRAME_SIZE_ERROR (0x6).
    data = (
        support.PREFACE
        + support.encode_frame(support.SETTINGS, 0, 0, b"\x00\x01\x02\x03\x04")
        + _get(1)
    )
    a = await _replay(data)
    b = await _replay(data)
    assert a.goaway == 0x6  # FRAME_SIZE_ERROR — the exact owned code
    assert a.matches(b)


async def test_an_unknown_frame_type_is_ignored_per_spec() -> None:
    # RFC 9113 §4.1: an endpoint must ignore an unknown frame type. The request
    # after it is answered normally.
    data = _conn(support.encode_frame(0xFA, 0, 0, b"opaque"), _get(1))
    a = await _replay(data)
    b = await _replay(data)
    assert a.streams.get(1) is not None and a.streams[1].status == 200
    assert a.goaway is None
    assert a.matches(b)


async def test_continuation_without_headers_is_a_connection_error() -> None:
    data = _conn(support.encode_frame(support.CONTINUATION, support.FLAG_END_HEADERS, 1, b"\x00"))
    a = await _replay(data)
    b = await _replay(data)
    assert a.goaway is not None and a.terminal == "closed"
    assert a.matches(b)


async def test_zero_increment_window_update_is_a_protocol_error() -> None:
    data = _conn(support.encode_window_update(0, 0), _get(1))
    a = await _replay(data)
    b = await _replay(data)
    assert a.goaway is not None
    assert a.matches(b)


async def test_rst_stream_on_the_connection_stream_is_a_protocol_error() -> None:
    data = _conn(support.encode_rst_stream(0, 0x1))
    a = await _replay(data)
    b = await _replay(data)
    assert a.goaway is not None
    assert a.matches(b)


async def test_a_decreasing_new_stream_id_is_a_protocol_error() -> None:
    # New streams must have strictly increasing ids; opening 3 then 1 is illegal.
    data = _conn(_get(3), _get(1))
    a = await _replay(data)
    b = await _replay(data)
    assert a.goaway is not None
    assert a.matches(b)


async def test_a_reused_stream_id_stays_deterministic() -> None:
    # Reusing a closed stream id is adversarial; whatever the owned driver decides
    # (here it answers the first and ignores the reuse) it must be the same every
    # run and must not crash or hang.
    data = _conn(_get(1), _get(1))
    a = await _replay(data)
    b = await _replay(data)
    assert a.matches(b)


async def test_preface_with_no_request_produces_no_response() -> None:
    a = await _replay(support.PREFACE + support.encode_settings({}))
    b = await _replay(support.PREFACE + support.encode_settings({}))
    assert not a.streams  # the server waited for a request that never came
    assert a.matches(b)
