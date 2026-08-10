"""Stage 6/7: transport replay, fault injection, and endpoint-plan replay.

Transport cases run over the HTTP/1 protocol driven by a fake transport; the
replay module ships its own fake transport so these never depend on the server
test harness. We only ever assert owned outcomes: normalized response bytes,
terminal disposition, and owned status/headers/body.
"""

from __future__ import annotations

import importlib

import pytest

import wreath
from wreath.replay import (
    CanonicalRequest,
    FaultDescriptor,
    FaultKind,
    FaultSchedule,
    PlanMode,
    ReplayError,
    SegmentKind,
    TransportRecording,
    open_recording,
    record_transport_segments,
    replay_endpoint_plan,
    replay_transport,
)

try:
    _native = importlib.import_module("wreath._native._server")
    _NATIVE_HTTP1 = _native.Http1Protocol
except ImportError:
    _NATIVE_HTTP1 = None


PROTOCOLS = [
    pytest.param(
        _NATIVE_HTTP1,
        id="http1",
        marks=pytest.mark.skipif(_NATIVE_HTTP1 is None, reason="native server not built"),
    )
]
proto = pytest.mark.parametrize("protocol_cls", PROTOCOLS)

GET_PING = b"GET /ping HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"


def _app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/ping")
    async def ping(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("pong")

    @app.post("/echo")
    async def echo(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse((await request.body()).decode())

    return app


# --- recording model + serialization ----------------------------------------


def test_transport_recording_round_trips() -> None:
    rec = record_transport_segments([GET_PING[:20], GET_PING[20:]], close=int(SegmentKind.EOF))
    restored = TransportRecording.from_bytes(rec.to_bytes())
    assert restored.segments == rec.segments
    assert restored.peername == rec.peername
    assert restored.sockname == rec.sockname


def test_reader_recovers_a_torn_tail() -> None:
    rec = record_transport_segments([GET_PING])
    blob = rec.to_bytes()
    # A truncated container still yields the chunks that were complete; a missing
    # required chunk is reported rather than silently accepted.
    with pytest.raises(ReplayError):
        TransportRecording.from_bytes(blob[: len(blob) - 10])


def test_open_recording_dispatches_on_magic(tmp_path) -> None:
    path = tmp_path / "conn.wtr1"
    path.write_bytes(record_transport_segments([GET_PING]).to_bytes())
    rec = open_recording(str(path))
    assert rec.segments[0].data == GET_PING
    (tmp_path / "bad.bin").write_bytes(b"XXXX not a recording")
    with pytest.raises(ReplayError):
        open_recording(str(tmp_path / "bad.bin"))


# --- transport replay --------------------------------------------------------


@proto
@pytest.mark.asyncio
async def test_replay_reproduces_the_owned_response(protocol_cls: type) -> None:
    rec = record_transport_segments([GET_PING])
    result = await replay_transport(_app(), rec, protocol_cls=protocol_cls)
    assert result.response.split(b"\r\n", 1)[0] == b"HTTP/1.1 200 OK"
    assert b"pong" in result.response
    assert result.terminal == "closed"  # Connection: close


@proto
@pytest.mark.asyncio
async def test_replay_is_deterministic(protocol_cls: type) -> None:
    rec = record_transport_segments([GET_PING])
    a = await replay_transport(_app(), rec, protocol_cls=protocol_cls)
    b = await replay_transport(_app(), rec, protocol_cls=protocol_cls)
    assert a.matches(b)
    # Date is normalized, so equality does not depend on wall-clock skew.
    assert b"date: <normalized>" in a.normalized.lower() or b"date:" not in a.response.lower()


@proto
@pytest.mark.asyncio
async def test_segmentation_does_not_change_the_owned_response(protocol_cls: type) -> None:
    whole = record_transport_segments([GET_PING])
    byte_at_a_time = record_transport_segments([GET_PING[i : i + 1] for i in range(len(GET_PING))])
    a = await replay_transport(_app(), whole, protocol_cls=protocol_cls)
    b = await replay_transport(_app(), byte_at_a_time, protocol_cls=protocol_cls)
    assert a.normalized == b.normalized


@proto
@pytest.mark.asyncio
async def test_body_request_replays(protocol_cls: type) -> None:
    post = (
        b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n"
        b"Connection: close\r\n\r\nhello"
    )
    rec = record_transport_segments([post])
    result = await replay_transport(_app(), rec, protocol_cls=protocol_cls)
    assert b"hello" in result.response


# --- fault injection ---------------------------------------------------------


def test_fault_schedule_round_trips() -> None:
    sched = FaultSchedule(
        (
            FaultDescriptor(int(FaultKind.SHORT_READ), 0, 4),
            FaultDescriptor(int(FaultKind.RESET), 2),
        )
    )
    assert FaultSchedule.from_bytes(sched.to_bytes()) == sched


@proto
@pytest.mark.asyncio
async def test_reset_midstream_stops_the_parser_deterministically(protocol_cls: type) -> None:
    rec = record_transport_segments([GET_PING[:20], GET_PING[20:35], GET_PING[35:]])
    sched = FaultSchedule((FaultDescriptor(int(FaultKind.RESET), 1),))
    a = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
    b = await replay_transport(
        _app(), rec, protocol_cls=protocol_cls, faults=FaultSchedule.from_bytes(sched.to_bytes())
    )
    # The request never completed, so no owned 200 response was produced.
    assert b"pong" not in a.response
    assert a.matches(b)  # same recording + same schedule => same owned outcome


@proto
@pytest.mark.asyncio
async def test_truncate_suppresses_the_tail(protocol_cls: type) -> None:
    rec = record_transport_segments([GET_PING[:20], GET_PING[20:35], GET_PING[35:]])
    sched = FaultSchedule((FaultDescriptor(int(FaultKind.TRUNCATE), 0, 10),))
    result = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
    # Only 10 bytes of the head ever reached the parser; nothing after.
    assert b"pong" not in result.response
    assert result.segments_fed == 1


@proto
@pytest.mark.asyncio
async def test_half_close_midstream_is_deterministic(protocol_cls: type) -> None:
    rec = record_transport_segments([GET_PING[:20], GET_PING[20:]])
    sched = FaultSchedule((FaultDescriptor(int(FaultKind.HALF_CLOSE), 0),))
    a = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
    b = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
    assert a.matches(b)


# --- endpoint-plan replay ----------------------------------------------------


@pytest.mark.asyncio
async def test_plan_invoke_runs_the_owned_pipeline() -> None:
    result = await replay_endpoint_plan(
        _app(), CanonicalRequest("GET", "/ping", headers=((b"host", b"x"),))
    )
    assert result.status == 200
    assert result.body == b"pong"
    assert result.best_effort is True and result.deterministic is False


@pytest.mark.asyncio
async def test_plan_invoke_binds_and_validates_a_body() -> None:
    result = await replay_endpoint_plan(
        _app(),
        CanonicalRequest(
            "POST", "/echo", headers=((b"content-type", b"text/plain"),), body=b"hi there"
        ),
    )
    assert result.status == 200
    assert result.body == b"hi there"


@pytest.mark.asyncio
async def test_plan_invoke_is_deterministic_for_a_pure_handler() -> None:
    canonical = CanonicalRequest("GET", "/ping")
    a = await replay_endpoint_plan(_app(), canonical)
    b = await replay_endpoint_plan(_app(), canonical)
    assert a.matches(b)


@pytest.mark.asyncio
async def test_plan_replace_uses_the_recorded_return() -> None:
    result = await replay_endpoint_plan(
        _app(), CanonicalRequest("GET", "/ping"), mode=PlanMode.REPLACE, recorded_return="stubbed"
    )
    assert result.status == 200
    assert result.body == b"stubbed"
    assert result.deterministic is True and result.best_effort is False


@pytest.mark.asyncio
async def test_plan_replace_maps_a_recorded_exception_through_owned_handling() -> None:
    from wreath.exceptions import NotFound

    result = await replay_endpoint_plan(
        _app(),
        CanonicalRequest("GET", "/ping"),
        mode=PlanMode.REPLACE,
        recorded_exception=NotFound("gone"),
    )
    assert result.status == 404
    assert result.deterministic is True


@pytest.mark.asyncio
async def test_plan_replace_requires_a_recorded_result() -> None:
    with pytest.raises(ValueError):
        await replay_endpoint_plan(_app(), CanonicalRequest("GET", "/ping"), mode=PlanMode.REPLACE)


@pytest.mark.asyncio
async def test_plan_skip_reports_route_resolution() -> None:
    hit = await replay_endpoint_plan(_app(), CanonicalRequest("GET", "/ping"), mode=PlanMode.SKIP)
    miss = await replay_endpoint_plan(_app(), CanonicalRequest("GET", "/nope"), mode=PlanMode.SKIP)
    assert hit.note == "route matched"
    assert miss.note == "no route matched"
    assert hit.body == b"" and hit.status == 0
