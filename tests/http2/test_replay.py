from __future__ import annotations

import pytest

import wreath
from wreath.replay import (
    FaultDescriptor,
    FaultKind,
    FaultSchedule,
    TransportRecording,
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

    @app.post("/echo")
    async def echo(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse((await request.body()).decode())

    return app


def _connection(*frames: bytes) -> bytes:
    return support.PREFACE + support.encode_settings({}) + b"".join(frames)


def _get(stream_id: int, path: bytes = b"/ping") -> bytes:
    return support.build_headers_frame(
        stream_id, support.request_headers(method=b"GET", path=path), end_stream=True
    )


async def test_h2_replay_reproduces_the_owned_response() -> None:
    rec = record_transport_segments([_connection(_get(1))])
    result = await replay_transport_h2(_app(), rec)
    stream = result.streams[1]
    assert stream.status == 200
    assert stream.body == b"pong"
    assert stream.header(b"content-type") == b"text/plain; charset=utf-8"
    assert stream.header(b"content-length") == b"4"


async def test_h2_replay_is_deterministic_modulo_date() -> None:
    rec = record_transport_segments([_connection(_get(1))])
    a = await replay_transport_h2(_app(), rec)
    b = await replay_transport_h2(_app(), rec)
    assert a.matches(b)  # date is normalized out of the comparison


async def test_h2_replay_decodes_multiplexed_streams() -> None:
    rec = record_transport_segments([_connection(_get(1), _get(3), _get(5))])
    result = await replay_transport_h2(_app(), rec)
    assert sorted(result.streams) == [1, 3, 5]
    assert all(s.status == 200 and s.body == b"pong" for s in result.streams.values())


async def test_h2_replay_carries_a_request_body() -> None:
    headers = support.build_headers_frame(
        1, support.request_headers(method=b"POST", path=b"/echo"), end_stream=False
    )
    data = support.encode_frame(support.DATA, support.FLAG_END_STREAM, 1, b"hello h2")
    rec = record_transport_segments([_connection(headers, data)])
    result = await replay_transport_h2(_app(), rec)
    assert result.streams[1].status == 200
    assert result.streams[1].body == b"hello h2"


async def test_h2_replay_segmentation_is_irrelevant() -> None:
    whole = _connection(_get(1))
    rec_whole = record_transport_segments([whole])
    rec_split = record_transport_segments([whole[i : i + 7] for i in range(0, len(whole), 7)])
    a = await replay_transport_h2(_app(), rec_whole)
    b = await replay_transport_h2(_app(), rec_split)
    assert a.matches(b)


async def test_h2_truncated_request_never_fabricates_a_response() -> None:
    conn = _connection(_get(1))
    rec = record_transport_segments([conn[:25], conn[25:]])
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.TRUNCATE), 0, 15),))
    result = await replay_transport_h2(_app(), rec, faults=schedule)
    # The HEADERS frame never completed, so no owned 200 stream exists.
    assert all(s.status == 0 for s in result.streams.values())


async def test_h2_reset_midstream_is_deterministic() -> None:
    conn = _connection(_get(1))
    rec = record_transport_segments([conn[:20], conn[20:40], conn[40:]])
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.RESET), 1),))
    a = await replay_transport_h2(_app(), rec, faults=schedule)
    b = await replay_transport_h2(_app(), rec, faults=schedule)
    assert a.matches(b)


async def test_h2_recording_round_trips() -> None:
    rec = record_transport_segments([_connection(_get(1))])
    restored = TransportRecording.from_bytes(rec.to_bytes())
    result = await replay_transport_h2(_app(), restored)
    assert result.streams[1].body == b"pong"
