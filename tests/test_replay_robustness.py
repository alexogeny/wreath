from __future__ import annotations

import pytest

import wreath
from wreath.orm import Model
from wreath.postgres import Connection
from wreath.replay import (
    AdapterFault,
    CanonicalRequest,
    DatabaseDouble,
    FaultDescriptor,
    FaultKind,
    FaultSchedule,
    PlanMode,
    ReplayAdapters,
    ReplayError,
    SegmentKind,
    TransportRecording,
    TransportSegment,
    open_recording,
    record_transport_segments,
    replay_endpoint_plan,
    replay_transport,
)

GET = b"GET /ping HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"


class Item(Model):
    name: str


def _app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/ping")
    async def ping(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("pong")

    @app.post("/echo")
    async def echo(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse((await request.body()).decode())

    @app.get("/boom")
    async def boom(request: wreath.Request) -> wreath.Response:
        raise RuntimeError("kaboom")

    return app


def test_a_corrupt_chunk_crc_is_detected_not_silently_accepted() -> None:
    blob = bytearray(record_transport_segments([GET]).to_bytes())
    blob[-1] ^= 0xFF  # flip a payload byte -> CRC mismatch on the SEGS chunk
    # The reader stops at the bad CRC; the required chunk is then missing, so it
    # reports rather than replaying corrupted bytes.
    with pytest.raises(ReplayError):
        TransportRecording.from_bytes(bytes(blob))


def test_open_recording_rejects_foreign_and_garbage_containers(tmp_path) -> None:
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.RESET), 0),))
    (tmp_path / "sched.wfs1").write_bytes(schedule.to_bytes())
    with pytest.raises(ReplayError):  # a fault schedule is not a recording
        open_recording(str(tmp_path / "sched.wfs1"))
    (tmp_path / "garbage.bin").write_bytes(b"\x00\x01\x02\x03nonsense")
    with pytest.raises(ReplayError):
        open_recording(str(tmp_path / "garbage.bin"))


def test_unsupported_container_version_is_rejected() -> None:
    blob = bytearray(record_transport_segments([GET]).to_bytes())
    blob[4] = 99  # bump the container version byte
    with pytest.raises(ReplayError):
        TransportRecording.from_bytes(bytes(blob))


def test_fault_schedule_round_trips_edge_values() -> None:
    schedule = FaultSchedule(
        (
            FaultDescriptor(int(FaultKind.SHORT_READ), 0, 0),
            FaultDescriptor(int(FaultKind.TRUNCATE), 5, 2**31 - 1),
            FaultDescriptor(int(FaultKind.HALF_CLOSE), 999999),
        )
    )
    assert FaultSchedule.from_bytes(schedule.to_bytes()) == schedule


@pytest.mark.asyncio
async def test_empty_recording_replays_to_nothing_safely() -> None:
    result = await replay_transport(_app(), TransportRecording(()))
    assert result.response == b"" and result.terminal in ("open", "closed")


@pytest.mark.asyncio
async def test_a_recording_that_opens_with_an_immediate_reset_is_safe() -> None:
    rec = TransportRecording((TransportSegment(0, int(SegmentKind.RESET), b""),))
    result = await replay_transport(_app(), rec)
    assert b"pong" not in result.response


@pytest.mark.asyncio
async def test_a_fault_index_past_the_last_segment_is_a_noop() -> None:
    rec = record_transport_segments([GET[:20], GET[20:]])
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.RESET), 99),))
    result = await replay_transport(_app(), rec, faults=schedule)
    assert b"pong" in result.response  # the request completed untouched


@pytest.mark.asyncio
async def test_short_read_beyond_the_segment_length_clamps() -> None:
    rec = record_transport_segments([GET])
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.SHORT_READ), 0, 10**9),))
    result = await replay_transport(_app(), rec, faults=schedule)
    assert b"pong" in result.response  # min(value, len) => the whole segment


@pytest.mark.asyncio
async def test_multiple_faults_in_one_schedule_all_apply() -> None:
    # A short-read that keeps the head incomplete, plus a truncate later: the
    # request cannot complete, and it does so deterministically.
    rec = record_transport_segments([GET[:15], GET[15:30], GET[30:]])
    schedule = FaultSchedule(
        (
            FaultDescriptor(int(FaultKind.SHORT_READ), 0, 5),
            FaultDescriptor(int(FaultKind.TRUNCATE), 2, 0),
        )
    )
    a = await replay_transport(_app(), rec, faults=schedule)
    b = await replay_transport(_app(), rec, faults=schedule)
    assert b"pong" not in a.response
    assert a.matches(b)


@pytest.mark.asyncio
async def test_body_split_across_segments_with_a_midbody_reset() -> None:
    post = b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\nConnection: close\r\n\r\n01234"
    tail = b"56789"
    rec = record_transport_segments([post, tail])
    # Reset after the head+partial-body segment: the body never completes.
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.RESET), 0),))
    a = await replay_transport(_app(), rec, faults=schedule)
    b = await replay_transport(_app(), rec, faults=schedule)
    assert b"0123456789" not in a.response  # the full body never echoed back
    assert a.matches(b)


@pytest.mark.asyncio
async def test_replace_with_a_plain_exception_maps_to_500() -> None:
    result = await replay_endpoint_plan(
        _app(),
        CanonicalRequest("GET", "/ping"),
        mode=PlanMode.REPLACE,
        recorded_exception=RuntimeError("unexpected"),
    )
    assert result.status == 500
    assert result.deterministic is True


@pytest.mark.asyncio
async def test_invoke_handler_that_raises_maps_to_500() -> None:
    result = await replay_endpoint_plan(_app(), CanonicalRequest("GET", "/boom"))
    assert result.status == 500


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"{}", b"not json", b"", b'{"name": 123}'])
async def test_invalid_bodies_get_an_owned_validation_status(body: bytes) -> None:
    app = wreath.Wreath()

    @app.post("/items")
    async def create(request: wreath.Request, item: Item) -> dict:
        return {"name": item.name}

    result = await replay_endpoint_plan(
        app,
        CanonicalRequest(
            "POST", "/items", headers=((b"content-type", b"application/json"),), body=body
        ),
    )
    assert result.status in (400, 422)  # owned rejection, never a 500 or a crash


@pytest.mark.asyncio
async def test_a_faulted_adapter_is_restored_after_the_replay() -> None:
    app = wreath.Wreath()
    app.postgres("main", dsn="postgres://stub/db")

    @app.get("/u")
    async def u(request: wreath.Request, db: Connection) -> dict:
        return {"n": len(await db.fetch("SELECT 1"))}

    original = app._databases["main"]
    double = DatabaseDouble("main", query_faults={0: AdapterFault.SERVER_ERROR})
    result = await replay_endpoint_plan(
        app,
        CanonicalRequest("GET", "/u"),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status == 500
    # The double is gone; the app is exactly as it was before the replay.
    assert app._databases["main"] is original


@pytest.mark.asyncio
async def test_db_map_fanout_fault_releases_and_maps_to_500() -> None:
    app = wreath.Wreath()
    app.postgres("main", dsn="postgres://stub/db")

    @app.get("/fan")
    async def fan(request: wreath.Request, db: Connection) -> dict:
        return {"n": len(await db.map("fetch", [(1,), (2,)]))}

    double = DatabaseDouble("main", query_faults={0: AdapterFault.SERVER_ERROR})
    result = await replay_endpoint_plan(
        app,
        CanonicalRequest("GET", "/fan"),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status == 500
    assert not double.leaked
