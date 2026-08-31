from __future__ import annotations

import struct
import zlib

import pytest

from wreath import _flight_reference as reference
from wreath import _flight_schema as schema


def _image() -> schema.MetadataImage:
    return schema.MetadataImage(
        version=schema.METADATA_VERSION,
        routes=(),
        plans=(),
        dependencies=(),
        middleware=(),
        auth_policies=(),
        serializers=(),
        validators=(),
        limits=(),
        clients=(),
        databases=(),
        models=(),
    )


def _recording_with_chunk(tag: bytes, payload: bytes, *, flags: int = 0) -> bytes:
    image = _image()
    header = reference._HEADER.pack(
        reference.MAGIC,
        reference._CONTAINER_VERSION,
        schema.SCHEMA_VERSION,
        flags,
    )
    chunk = reference._CHUNK.pack(tag, len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
    return header + image.image_hash_short() + chunk + payload


def test_encode_recording_refuses_a_non_cell_event() -> None:
    with pytest.raises(schema.SchemaError, match="event cell must be 64 bytes, got 63"):
        reference.encode_recording(_image(), (b"x" * (schema.CELL_SIZE - 1),))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "recording is shorter than its header"),
        (
            reference._HEADER.pack(reference.MAGIC, 2, schema.SCHEMA_VERSION, 0)
            + b"\0" * schema.IMAGE_HASH_BYTES,
            "unsupported container version 2",
        ),
        (
            reference._HEADER.pack(reference.MAGIC, 1, schema.SCHEMA_VERSION + 1, 0)
            + b"\0" * schema.IMAGE_HASH_BYTES,
            f"unsupported schema version {schema.SCHEMA_VERSION + 1}",
        ),
    ],
)
def test_decode_recording_refuses_invalid_headers(data: bytes, message: str) -> None:
    with pytest.raises(schema.SchemaError, match=message):
        reference.decode_recording(data)


def test_decode_recording_refuses_a_false_metadata_hash() -> None:
    encoded = bytearray(reference.encode_recording(_image()))
    encoded[reference._HEADER.size] ^= 1
    with pytest.raises(schema.SchemaError, match="metadata image hash"):
        reference.decode_recording(bytes(encoded))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "truncated chunk header"),
        (
            reference._CHUNK.pack(b"NOPE", 0, 0),
            "expected chunk b'META', found b'NOPE'",
        ),
        (
            reference._CHUNK.pack(b"META", schema.MAX_CHUNK_BYTES + 1, 0),
            "declares .* bytes, over the limit",
        ),
        (reference._CHUNK.pack(b"META", 1, 0), "truncated: need 1 bytes"),
        (reference._CHUNK.pack(b"META", 1, 0) + b"x", "failed its CRC32 check"),
    ],
)
def test_read_chunk_refuses_invalid_chunks(data: bytes, message: str) -> None:
    with pytest.raises(schema.SchemaError, match=message):
        reference._read_chunk(data, 0, b"META")


def test_decode_recording_refuses_partial_and_wrong_version_event_cells() -> None:
    image = _image()
    meta = reference._chunk(b"META", image.canonical_bytes())
    prefix = (
        reference._HEADER.pack(reference.MAGIC, 1, schema.SCHEMA_VERSION, 1)
        + image.image_hash_short()
        + meta
    )
    partial = prefix + reference._chunk(b"EVNT", b"x")
    with pytest.raises(schema.SchemaError, match="whole number of cells"):
        reference.decode_recording(partial)

    bad_cell = bytes([schema.SCHEMA_VERSION + 1]) + b"\0" * (schema.CELL_SIZE - 1)
    with pytest.raises(schema.SchemaError, match="unsupported schema version"):
        reference.decode_recording(prefix + reference._chunk(b"EVNT", bad_cell))


@pytest.mark.parametrize("ring_records", [3, 6])
def test_reference_recorder_refuses_non_power_of_two_rings(ring_records: int) -> None:
    with pytest.raises(ValueError, match="ring_records must be a power of two"):
        reference.ReferenceRecorder(schema.Mode.PULSE, ring_records=ring_records)


def test_capture_storage_is_disabled_outside_forensic_mode() -> None:
    recorder = reference.ReferenceRecorder(
        schema.Mode.DETAILED,
        detailed_sample_rate=1.0,
        capture_slabs=2,
        slab_bytes=128,
    )
    request = recorder.begin(1, schema.Protocol.HTTP1, 1)
    request.capture(1, disposition=schema.CaptureDisposition.RAW, data=b"secret")
    assert recorder.capture_capacity == 0
    assert recorder.capture_slab_bytes == 0
    assert request.capture_slot == -1
    assert recorder.loss(schema.LossReason.CAPTURE_POOL_FULL) == 0


def test_zero_capture_slabs_disable_forensic_storage() -> None:
    recorder = reference.ReferenceRecorder(
        schema.Mode.FORENSIC,
        detailed_sample_rate=1.0,
        capture_slabs=0,
        slab_bytes=128,
    )
    request = recorder.begin(1, schema.Protocol.HTTP1, 1)
    request.capture(1, disposition=schema.CaptureDisposition.RAW, data=b"secret")
    assert recorder.capture_capacity == 0
    assert recorder.capture_slab_bytes == 0
    assert request.capture_slot == -1
    assert recorder.loss(schema.LossReason.CAPTURE_POOL_FULL) == 0


def test_unarmed_forensic_request_does_not_reserve_capture_storage() -> None:
    recorder = reference.ReferenceRecorder(
        schema.Mode.FORENSIC,
        detailed_sample_rate=0.0,
        capture_slabs=1,
        slab_bytes=128,
    )
    request = recorder.begin(1, schema.Protocol.HTTP1, 1)
    request.capture(1, disposition=schema.CaptureDisposition.RAW, data=b"secret")
    assert request.capture_slot == -1
    assert recorder.capture_in_use == 0


def test_zero_max_bytes_does_not_truncate_raw_capture() -> None:
    recorder = reference.ReferenceRecorder(
        schema.Mode.FORENSIC,
        detailed_sample_rate=1.0,
        capture_slabs=1,
        slab_bytes=128,
    )
    request = recorder.begin(1, schema.Protocol.HTTP1, 1)
    request.capture(1, disposition=schema.CaptureDisposition.RAW, data=b"secret", max_bytes=0)
    request.finish(2, status=200)
    slab = recorder.drain_captures()[0]
    field = slab[schema.CAPTURE_SLAB_HEADER_SIZE :]
    stored = struct.unpack_from("<H", field, 6)[0]
    assert stored == len(b"secret")
    assert field[schema.CAPTURE_FIELD_HEADER_SIZE :][:stored] == b"secret"


def test_large_max_bytes_does_not_extend_raw_capture() -> None:
    recorder = reference.ReferenceRecorder(
        schema.Mode.FORENSIC,
        detailed_sample_rate=1.0,
        capture_slabs=1,
        slab_bytes=128,
    )
    request = recorder.begin(1, schema.Protocol.HTTP1, 1)
    request.capture(1, disposition=schema.CaptureDisposition.RAW, data=b"secret", max_bytes=10)
    request.finish(2, status=200)
    slab = recorder.drain_captures()[0]
    field = slab[schema.CAPTURE_SLAB_HEADER_SIZE :]
    stored = struct.unpack_from("<H", field, 6)[0]
    assert stored == len(b"secret")
    assert field[schema.CAPTURE_FIELD_HEADER_SIZE :][:stored] == b"secret"


def test_capture_refuses_a_field_when_its_header_cannot_fit() -> None:
    recorder = reference.ReferenceRecorder(
        schema.Mode.FORENSIC,
        detailed_sample_rate=1.0,
        capture_slabs=1,
        slab_bytes=schema.CAPTURE_SLAB_HEADER_SIZE,
    )
    request = recorder.begin(1, schema.Protocol.HTTP1, 1)
    request.capture(1, disposition=schema.CaptureDisposition.RAW, data=b"x")
    assert request.capture_slot == 0
    assert recorder.loss(schema.LossReason.CAPTURE_POOL_FULL) == 1
