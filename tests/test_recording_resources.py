import hashlib
import io
import struct
import uuid
import zlib

import pytest

from wreath import _recording_format
from wreath._flight_schema import SCHEMA_VERSION, MetadataImage
from wreath._recording_format import WFR1Writer, read_recording


def _image():
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
        clients=(),
        databases=(),
        models=(),
    )


def test_recording_writer_serializes_metadata_once(monkeypatch):
    calls = []
    original = MetadataImage.canonical_bytes

    def counted(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(MetadataImage, "canonical_bytes", counted)
    output = io.BytesIO()
    writer = WFR1Writer(output, _image())
    writer.close()
    assert len(calls) == 1


def test_recording_header_and_metadata_match_independent_wire_oracle(monkeypatch):
    monkeypatch.setattr(_recording_format.time, "time_ns", lambda: 123456)
    monkeypatch.setattr(_recording_format.time, "monotonic_ns", lambda: 789)
    monkeypatch.setattr(_recording_format.uuid, "uuid4", lambda: uuid.UUID(int=42))
    monkeypatch.setattr(_recording_format, "_build_id", lambda: 99)
    image = _image()
    payload = image.canonical_bytes()
    digest = hashlib.blake2b(payload, digest_size=32).digest()[:16]
    output = io.BytesIO()
    writer = WFR1Writer(output, image)
    header = struct.pack(
        "<4sBBH16s16sQQQQQ",
        b"WFR1",
        1,
        SCHEMA_VERSION,
        0,
        digest,
        uuid.UUID(int=42).bytes,
        123456,
        789,
        123456,
        99,
        0,
    )
    metadata = struct.pack("<4sII", b"META", len(payload), zlib.crc32(payload)) + payload
    assert output.getvalue() == header + metadata
    writer.close()
    completed = output.getvalue()
    writer.close()
    assert output.getvalue() == completed
    assert read_recording(completed).clean


def test_serialization_failure_publishes_no_header(monkeypatch):
    def fail(self):
        raise ValueError("metadata serialization failed")

    monkeypatch.setattr(MetadataImage, "canonical_bytes", fail)
    output = io.BytesIO()
    with pytest.raises(ValueError, match="metadata serialization failed"):
        WFR1Writer(output, _image())
    assert output.getvalue() == b""


@pytest.mark.parametrize("canonical", [b"", b"preencoded metadata"])
def test_preencoded_hash_does_not_serialize_again(monkeypatch, canonical):
    def fail(self):
        raise AssertionError("preencoded input must not serialize")

    image = _image()
    monkeypatch.setattr(MetadataImage, "canonical_bytes", fail)
    expected = hashlib.blake2b(canonical, digest_size=32).digest()
    assert image.image_hash(canonical=canonical) == expected
    assert image.image_hash_short(canonical=canonical) == expected[:16]


def test_no_argument_hash_still_serializes_current_image():
    image = _image()
    expected = hashlib.blake2b(image.canonical_bytes(), digest_size=32).digest()
    assert image.image_hash() == expected
    assert image.image_hash_short() == expected[:16]
