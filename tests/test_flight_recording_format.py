"""Stage 5 slice 5c — WFR1 recording container + async recording sink.

Round-trips the container, proves it recovers a torn tail and rejects an
incompatible header, and drives the real native recorder through the async sink
to an owner-only file, including the disk-error drop-and-count path.
"""

from __future__ import annotations

import os
import time

import pytest

from wreath import _flight_schema as fs
from wreath._flight_schema import CaptureFieldClass as FC
from wreath._flight_schema import MetadataImage, NamedMeta
from wreath._recording_format import (
    MAGIC,
    DecodedRecording,
    RecordingSink,
    WFR1Writer,
    read_recording,
)

_flight = pytest.importorskip("wreath._native._flight")
KEY = (0x1122334455667788, 0x99AABBCCDDEEFF00)


def _image() -> MetadataImage:
    return MetadataImage(
        version=1,
        routes=(),
        plans=(),
        dependencies=(),
        middleware=(),
        auth_policies=(),
        serializers=(),
        validators=(),
        limits=(),
        clients=(NamedMeta(entry_id=1, name="upstream"),),
        databases=(),
        models=(),
    )


def _recorder(**kw: object) -> object:
    kw.setdefault("ring_records", 256)
    kw.setdefault("active_requests", 16)
    kw.setdefault("capture_slabs", 8)
    kw.setdefault("slab_bytes", 4096)
    kw.setdefault("detailed_sample_rate", 1.0)
    kw.setdefault("capture_hash_key", KEY)
    return _flight.Recorder(_flight.MODE_FORENSIC, **kw)


def _commit(rec: object, count: int) -> None:
    """Capture + finish `count` requests, committing slabs but NOT draining them
    (the caller or the sink drains)."""
    for i in range(count):
        req = rec.begin(connection_id=i, protocol=1, start_ns=i)
        req.capture(int(FC.REQUEST_HEADER), 1, _flight.CAP_RAW, b"trace-%d" % i)
        req.capture(int(FC.REQUEST_BODY), 0, _flight.CAP_HASHED, b"secret-%d" % i)
        req.finish(now_ns=i + 1, status=200)


def _capture_slabs(rec: object, count: int) -> list[bytes]:
    _commit(rec, count)
    return rec.drain_captures()


# --- container round trip ----------------------------------------------------


def _write(image: MetadataImage, slabs: list[bytes], events: bytes = b"") -> bytes:
    import io

    buf = io.BytesIO()
    writer = WFR1Writer(buf, image)
    writer.write_captures(slabs)
    if events:
        writer.write_events(events)
    writer.close()
    return buf.getvalue()


def test_wfr1_round_trips_metadata_and_slabs() -> None:
    image = _image()
    rec = _recorder()
    slabs = _capture_slabs(rec, 3)
    events = rec.drain()
    blob = _write(image, slabs, events)
    assert blob[:4] == MAGIC

    decoded = read_recording(blob)
    assert isinstance(decoded, DecodedRecording)
    assert decoded.clean
    assert decoded.image.image_hash_short() == image.image_hash_short()
    assert len(decoded.slabs) == 3
    assert decoded.footer_capture_slabs == 3
    # Slab contents survived the trip.
    raw = {fs.CaptureSlab.decode(s).request_id for s in slabs}
    assert {slab.request_id for slab in decoded.slabs} == raw
    assert decoded.clock_unix_ns > 0 and decoded.created_unix_nano > 0
    assert len(decoded.recording_uuid) == 16


def test_wfr1_recovers_a_torn_tail() -> None:
    image = _image()
    slabs = _capture_slabs(_recorder(), 4)
    blob = _write(image, slabs)
    # Chop off the footer (and part of the last capture chunk): an abrupt end.
    torn = blob[: len(blob) - 40]
    decoded = read_recording(torn)
    assert not decoded.clean  # no footer reached
    assert decoded.image.image_hash_short() == image.image_hash_short()
    # Everything before the cut is still recoverable (metadata at least).


def test_wfr1_stops_at_a_corrupt_chunk() -> None:
    image = _image()
    slabs = _capture_slabs(_recorder(), 2)
    blob = bytearray(_write(image, slabs))
    # Corrupt a byte inside the capture chunk payload (past header + META).
    blob[-8] ^= 0xFF
    decoded = read_recording(bytes(blob))
    assert not decoded.clean  # CRC failure ends parsing before the footer
    assert decoded.image is not None  # the clean prefix (metadata) survived


def test_wfr1_rejects_bad_versions_and_hash() -> None:
    blob = bytearray(_write(_image(), _capture_slabs(_recorder(), 1)))
    good = bytes(blob)
    with pytest.raises(fs.SchemaError):
        read_recording(good[:8])  # shorter than the header
    bad_magic = bytearray(good)
    bad_magic[0:4] = b"WFR0"
    with pytest.raises(fs.SchemaError):
        read_recording(bytes(bad_magic))
    bad_container = bytearray(good)
    bad_container[4] = 9  # container_ver byte
    with pytest.raises(fs.SchemaError):
        read_recording(bytes(bad_container))
    bad_hash = bytearray(good)
    bad_hash[8] ^= 0xFF  # corrupt the header image_hash -> mismatch vs META
    with pytest.raises(fs.SchemaError):
        read_recording(bytes(bad_hash))


def test_wfr1_requires_a_metadata_chunk() -> None:
    import io

    # A header with no chunks at all is missing its metadata.
    from wreath._recording_format import _HEADER

    buf = io.BytesIO()
    buf.write(
        _HEADER.pack(MAGIC, 1, fs.SCHEMA_VERSION, 0, b"\x00" * 16, b"\x00" * 16,
                     1, 1, 1, 0, 0)
    )
    with pytest.raises(fs.SchemaError):
        read_recording(buf.getvalue())


def test_wfr1_writer_rejects_ragged_event_bytes() -> None:
    import io

    writer = WFR1Writer(io.BytesIO(), _image())
    with pytest.raises(fs.SchemaError):
        writer.write_events(b"not a whole cell")


# --- async recording sink ----------------------------------------------------


def test_recording_sink_writes_owner_only_wfr1(tmp_path: object) -> None:
    path = str(tmp_path / "flight.wfr1")
    rec = _recorder(capture_slabs=8)
    _commit(rec, 5)  # commit 5 slabs; the sink is the drainer
    assert rec.capture_committed == 5

    sink = RecordingSink(rec, _image(), path, interval=0.02)
    sink.start()
    sink.stop()  # stop() does a final drain + footer

    assert sink.stats["written"] == 5
    assert sink.stats["dropped"] == 0
    assert not sink.stats["degraded"]
    # Owner-only file.
    assert (os.stat(path).st_mode & 0o777) == 0o600

    with open(path, "rb") as fh:
        decoded = read_recording(fh.read())
    assert decoded.clean
    assert len(decoded.slabs) == 5
    # request ids are worker-assigned 1..5 for the five requests.
    assert {s.request_id for s in decoded.slabs} == {1, 2, 3, 4, 5}
    # The recorder's slabs all returned to the pool after draining.
    assert rec.capture_committed == 0


def test_recording_sink_degrades_on_open_failure_without_raising(
    tmp_path: object,
) -> None:
    # A path in a nonexistent directory cannot be opened: the sink degrades to
    # drain-and-drop and never raises, and the slab pool still empties.
    path = str(tmp_path / "missing-dir" / "flight.wfr1")
    rec = _recorder(capture_slabs=4)
    _commit(rec, 3)
    assert rec.capture_committed == 3

    sink = RecordingSink(rec, _image(), path, interval=0.02)
    sink.start()
    sink.stop()

    assert sink.stats["degraded"] is True
    assert sink.stats["written"] == 0
    assert sink.stats["dropped"] == 3
    assert not os.path.exists(path)
    # Dropped output must not stall the bounded pool: the sink drained the whole
    # commit ring (drained slabs return to the free pool on the next reserve).
    assert rec.capture_committed == 0


def test_recording_sink_survives_a_mid_run_write_error(tmp_path: object) -> None:
    path = str(tmp_path / "flight.wfr1")
    rec = _recorder(capture_slabs=8)
    sink = RecordingSink(rec, _image(), path, interval=0.02)
    sink.start()

    # First batch writes fine.
    _commit(rec, 2)
    _wait(lambda: sink.stats["written"] >= 2)

    # Now sabotage the open file handle so the next write raises OSError.
    os.close(sink._fh.fileno())  # underlying fd closed under the buffered writer
    _commit(rec, 2)
    _wait(lambda: sink.stats["degraded"] is True)
    sink.stop()

    assert sink.stats["write_errors"] >= 1
    assert sink.stats["dropped"] >= 2
    assert rec.capture_committed == 0  # sink drained the ring despite the error


def _wait(cond: object, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")
