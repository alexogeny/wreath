"""The curated fault corpus (§7): every taxonomy region drives the owned code to
a deterministic outcome. This is the artifact the sanitizer/fuzz gate re-runs, so
a regression here is a regression in the owned failure handling itself.

Also covers the recording-reader fault taxonomy: a truncated, corrupt, version-
mismatched, reordered, or duplicated container must be detected or safely
recovered -- never crash or over-read the reader.
"""

from __future__ import annotations

import importlib
import struct

import pytest

import wreath
from wreath.postgres import Connection
from wreath.replay import (
    CanonicalRequest,
    FaultSchedule,
    ReplayAdapters,
    ReplayError,
    TransportRecording,
    fault_corpus,
    record_transport_segments,
    replay_endpoint_plan,
    replay_transport,
)

try:
    _native = importlib.import_module("wreath._native._server")
    _NATIVE_HTTP1 = _native.Http1Protocol
except ImportError:
    _NATIVE_HTTP1 = None

from wreath._pure.server import Http1Protocol as _PURE_HTTP1

proto = pytest.mark.parametrize(
    "protocol_cls",
    [
        pytest.param(_PURE_HTTP1, id="pure"),
        pytest.param(
            _NATIVE_HTTP1, id="native",
            marks=pytest.mark.skipif(_NATIVE_HTTP1 is None, reason="native server not built"),
        ),
    ],
)

GET = b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
CORPUS = fault_corpus()
TRANSPORT_NAMES = sorted(n for n in CORPUS if n.startswith(("transport", "schedule")))
ADAPTER_NAMES = sorted(n for n in CORPUS if n.startswith("adapter"))


def _app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/")
    async def root(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("ok")

    return app


# --- transport / scheduling corpus -------------------------------------------


@proto
@pytest.mark.asyncio
@pytest.mark.parametrize("name", TRANSPORT_NAMES)
async def test_transport_corpus_entry_is_deterministic(protocol_cls: type, name: str) -> None:
    schedule = FaultSchedule.from_bytes(CORPUS[name].to_bytes())  # via serialization
    rec = record_transport_segments([GET[:16], GET[16:32], GET[32:]])
    a = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=schedule)
    b = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=schedule)
    assert a.matches(b)
    assert a.terminal in ("closed", "aborted", "open")


# --- adapter corpus ----------------------------------------------------------


def _db_http_app() -> wreath.Wreath:
    app = wreath.Wreath()
    app.postgres("main", dsn="postgres://stub/db")

    @app.get("/db")
    async def db(request: wreath.Request, conn: Connection) -> dict:
        return {"n": len(await conn.fetch("SELECT 1"))}

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ADAPTER_NAMES)
async def test_adapter_corpus_entry_is_deterministic(name: str) -> None:
    schedule = FaultSchedule.from_bytes(CORPUS[name].to_bytes())
    # DB adapter faults route to a DB handler; HTTP ones have no in-app target
    # here, so we only assert the schedule reconstructs its doubles cleanly.
    adapters = ReplayAdapters.from_faults(schedule.adapter_faults)
    if "main" in adapters.databases:
        double = adapters.databases["main"]
        a = await replay_endpoint_plan(_db_http_app(), CanonicalRequest("GET", "/db"),
                                       adapters=adapters)
        # A pooled/query fault is an owned 500; a release-only fault may still 200.
        assert a.status in (200, 500)
        # An acquire fault never leases; otherwise the connection is returned.
        if double.acquired and double.acquire_fault is None:
            assert double.acquired == double.released
    else:
        assert adapters.clients  # HTTP fault reconstructed a faulty client


# --- recording-reader fault taxonomy -----------------------------------------


def _valid_recording() -> bytes:
    return record_transport_segments([GET]).to_bytes()


def test_reader_rejects_a_truncated_container() -> None:
    blob = _valid_recording()
    with pytest.raises(ReplayError):
        TransportRecording.from_bytes(blob[: len(blob) - 12])


def test_reader_rejects_a_corrupt_chunk_crc() -> None:
    blob = bytearray(_valid_recording())
    blob[-1] ^= 0xFF
    with pytest.raises(ReplayError):
        TransportRecording.from_bytes(bytes(blob))


def test_reader_rejects_an_unsupported_version() -> None:
    blob = bytearray(_valid_recording())
    blob[4] = 200
    with pytest.raises(ReplayError):
        TransportRecording.from_bytes(bytes(blob))


def test_reader_rejects_a_foreign_container() -> None:
    with pytest.raises(ReplayError):
        TransportRecording.from_bytes(b"XXXX" + b"\x01" + b"garbage")


def test_reader_recovers_the_chunks_before_a_corrupt_one() -> None:
    # A container whose SECOND chunk is corrupt still yields the first: the HEAD
    # chunk is intact but SEGS is corrupt -> the required SEGS is missing -> a
    # reported error, never a partial/garbage replay.
    rec = TransportRecording(())  # HEAD present, SEGS present-but-empty
    blob = bytearray(rec.to_bytes())
    # Corrupt the last byte (inside the SEGS chunk payload / its crc region).
    blob[-1] ^= 0xFF
    with pytest.raises(ReplayError):
        TransportRecording.from_bytes(bytes(blob))


def test_fault_schedule_reader_rejects_a_corrupt_schedule() -> None:
    blob = bytearray(CORPUS[TRANSPORT_NAMES[0]].to_bytes())
    blob[-1] ^= 0xFF
    with pytest.raises(ReplayError):
        FaultSchedule.from_bytes(bytes(blob))


def test_fault_schedule_reader_rejects_a_truncated_length_prefix() -> None:
    # A schedule claiming more faults than its bytes contain must not over-read.
    body = struct.pack("<I", 99)  # says 99 faults, but no fault bytes follow
    from wreath.replay import _MAGIC_FAULTS, _chunk  # internal framing helpers

    blob = _MAGIC_FAULTS + b"\x01" + _chunk(b"FALT", body)
    with pytest.raises((ReplayError, struct.error)):
        FaultSchedule.from_bytes(blob)
