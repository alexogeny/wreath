from __future__ import annotations

import random
import struct

import pytest

from wreath.replay import (
    _CHUNK,
    _MAGIC_FAULTS,
    _MAGIC_TRANSPORT,
    MAX_CHUNK_BYTES,
    AdapterFaultDescriptor,
    AdapterSeam,
    FaultDescriptor,
    FaultKind,
    FaultSchedule,
    ReplayError,
    SegmentKind,
    TransportRecording,
    TransportSegment,
    _chunk,
    fault_corpus,
    record_transport_segments,
)

GET = b"GET /ping HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"


def _recording() -> bytes:
    return record_transport_segments([GET]).to_bytes()


def test_transport_container_refuses_magic_version_and_short_header_by_name() -> None:
    with pytest.raises(ReplayError, match="not a WTR1 transport recording"):
        TransportRecording.from_bytes(b"NOPE\x01")
    with pytest.raises(ReplayError, match="unsupported WTR1 container version"):
        TransportRecording.from_bytes(_MAGIC_TRANSPORT)
    with pytest.raises(ReplayError, match="unsupported WTR1 container version"):
        TransportRecording.from_bytes(_MAGIC_TRANSPORT + b"\xff")


def test_transport_container_names_each_missing_required_chunk() -> None:
    version = bytes((1,))
    head_only = _MAGIC_TRANSPORT + version + _chunk(b"HEAD", b"\x00" * 24)
    segs_only = _MAGIC_TRANSPORT + version + _chunk(b"SEGS", struct.pack("<I", 0))

    with pytest.raises(ReplayError, match="missing a required chunk"):
        TransportRecording.from_bytes(head_only)
    with pytest.raises(ReplayError, match="missing a required chunk"):
        TransportRecording.from_bytes(segs_only)


def test_fault_container_refuses_magic_and_version_by_name() -> None:
    with pytest.raises(ReplayError, match="not a WFS1 fault schedule"):
        FaultSchedule.from_bytes(b"NOPE\x01")
    with pytest.raises(ReplayError, match="unsupported WFS1 container version"):
        FaultSchedule.from_bytes(_MAGIC_FAULTS)
    with pytest.raises(ReplayError, match="unsupported WFS1 container version"):
        FaultSchedule.from_bytes(_MAGIC_FAULTS + b"\xff")


def test_a_second_copy_of_a_chunk_is_refused_rather_than_preferred() -> None:
    original = record_transport_segments([GET])
    forged = TransportRecording((TransportSegment(0, int(SegmentKind.DATA), b"XXXX"),))
    # Take the forged recording's SEGS chunk and staple it onto the real one.
    tampered = original.to_bytes() + _chunk(b"SEGS", _segs_payload(forged))
    with pytest.raises(ReplayError, match="repeats"):
        TransportRecording.from_bytes(tampered)


def _segs_payload(recording: TransportRecording) -> bytes:
    """The bytes `to_bytes` puts in the SEGS chunk, without re-framing them."""
    body = bytearray(struct.pack("<I", len(recording.segments)))
    for segment in recording.segments:
        body += struct.pack("<QBI", segment.offset_us, segment.kind, len(segment.data))
        body += segment.data
    return bytes(body)


def test_the_refusal_names_the_chunk_it_found_twice() -> None:
    blob = _recording() + _chunk(b"HEAD", b"\x00" * 24)
    with pytest.raises(ReplayError) as caught:
        TransportRecording.from_bytes(blob)
    assert "HEAD" in str(caught.value)


def test_a_repeated_chunk_in_a_fault_schedule_is_refused_too() -> None:
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.RESET), 0),))
    blob = schedule.to_bytes() + _chunk(b"FALT", struct.pack("<I", 0))
    with pytest.raises(ReplayError, match="repeats"):
        FaultSchedule.from_bytes(blob)


def test_trailing_bytes_that_are_not_a_chunk_are_recovered_past() -> None:
    original = TransportRecording.from_bytes(_recording())
    torn = TransportRecording.from_bytes(_recording() + b"\x00\x01\x02")
    assert torn == original


def test_an_unknown_trailing_chunk_is_ignored_by_a_recording() -> None:
    blob = _recording() + _chunk(b"XTRA", b"a field from the future")
    assert TransportRecording.from_bytes(blob) == TransportRecording.from_bytes(_recording())


def test_a_schedule_refuses_a_chunk_tag_it_does_not_know() -> None:
    schedule = FaultSchedule(
        (FaultDescriptor(int(FaultKind.RESET), 0),),
        (AdapterFaultDescriptor(int(AdapterSeam.DB_QUERY), "main", "server_error", 0),),
    )
    blob = bytearray(schedule.to_bytes())
    tag_at = blob.index(b"ADPT")
    blob[tag_at + 2] ^= 0x80  # one bit of the 'P'
    with pytest.raises(ReplayError, match="unrecognised"):
        FaultSchedule.from_bytes(bytes(blob))


def test_the_unknown_tag_refusal_says_a_tag_is_not_checksummed() -> None:
    blob = FaultSchedule().to_bytes() + _chunk(b"XTRA", b"")
    with pytest.raises(ReplayError) as caught:
        FaultSchedule.from_bytes(blob)
    assert "not covered by the CRC" in str(caught.value)


def test_a_truncated_schedule_is_refused_rather_than_half_applied() -> None:
    schedule = FaultSchedule(
        faults=(FaultDescriptor(int(FaultKind.TRUNCATE), 1, 4),),
        adapter_faults=(
            AdapterFaultDescriptor(int(AdapterSeam.DB_QUERY), "main", "server_error", 0),
        ),
    )
    blob = schedule.to_bytes()
    assert FaultSchedule.from_bytes(blob) == schedule  # the control: it does decode
    with pytest.raises(ReplayError, match="trailing bytes"):
        FaultSchedule.from_bytes(blob[: len(blob) - 6])


def test_the_refusal_says_how_much_of_the_schedule_was_lost() -> None:
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.RESET), 0),))
    with pytest.raises(ReplayError) as caught:
        FaultSchedule.from_bytes(schedule.to_bytes() + b"\x00\x01\x02")
    assert "3 trailing bytes" in str(caught.value)


def test_a_recording_still_recovers_the_tail_a_schedule_refuses() -> None:
    original = TransportRecording.from_bytes(_recording())
    assert TransportRecording.from_bytes(_recording() + b"\x00\x01\x02") == original


def test_a_chunk_length_that_overruns_the_buffer_is_not_read() -> None:
    payload = b"nowhere near this long"
    header = _CHUNK.pack(b"SEGS", 10_000, 0)
    blob = _MAGIC_TRANSPORT + b"\x01" + _chunk(b"HEAD", b"\x00" * 24) + header + payload
    with pytest.raises(ReplayError, match="missing a required chunk"):
        TransportRecording.from_bytes(blob)


def test_a_chunk_length_past_the_hard_cap_is_not_allocated() -> None:
    header = _CHUNK.pack(b"SEGS", MAX_CHUNK_BYTES + 1, 0)
    blob = _MAGIC_TRANSPORT + b"\x01" + _chunk(b"HEAD", b"\x00" * 24) + header
    with pytest.raises(ReplayError, match="missing a required chunk"):
        TransportRecording.from_bytes(blob)


def test_the_writer_refuses_to_frame_a_chunk_past_the_cap() -> None:
    with pytest.raises(ReplayError, match="exceeds"):
        _chunk(b"SEGS", b"\x00" * (MAX_CHUNK_BYTES + 1))


def test_a_schedule_claiming_more_faults_than_it_carries_does_not_over_read() -> None:
    body = struct.pack("<I", 4096)  # says 4096 faults; carries none
    blob = _MAGIC_FAULTS + b"\x01" + _chunk(b"FALT", body)
    with pytest.raises((ReplayError, struct.error)):
        FaultSchedule.from_bytes(blob)


def _random_schedule(rng: random.Random) -> FaultSchedule:
    kinds = list(FaultKind)
    seams = list(AdapterSeam)
    faults = tuple(
        FaultDescriptor(
            int(rng.choice(kinds)),
            rng.choice([0, 1, 7, 4096, 2**31 - 1]),
            rng.choice([0, 1, 8, 65535, 2**32 - 1]),
        )
        for _ in range(rng.randrange(0, 6))
    )
    adapter = tuple(
        AdapterFaultDescriptor(
            int(rng.choice(seams)),
            rng.choice(["main", "api", "objects", "", "a" * 300]),
            rng.choice(["server_error", "claim_lost", "decode_error", "прив"]),
            rng.choice([0, 1, 2**31 - 1]),
        )
        for _ in range(rng.randrange(0, 4))
    )
    return FaultSchedule(faults, adapter)


@pytest.mark.parametrize("seed", range(64))
def test_a_fault_schedule_round_trips_whatever_it_is_given(seed: int) -> None:
    schedule = _random_schedule(random.Random(seed))
    assert FaultSchedule.from_bytes(schedule.to_bytes()) == schedule


@pytest.mark.parametrize("seed", range(64))
def test_serialising_a_schedule_is_deterministic(seed: int) -> None:
    schedule = _random_schedule(random.Random(seed))
    assert schedule.to_bytes() == schedule.to_bytes()
    assert FaultSchedule.from_bytes(schedule.to_bytes()).to_bytes() == schedule.to_bytes()


@pytest.mark.parametrize("name", sorted(fault_corpus()))
def test_every_corpus_entry_survives_its_own_container(name: str) -> None:
    schedule = fault_corpus()[name]
    assert FaultSchedule.from_bytes(schedule.to_bytes()) == schedule


@pytest.mark.parametrize("seed", range(32))
def test_a_flipped_byte_anywhere_in_a_schedule_is_caught_or_harmless(seed: int) -> None:
    schedule = _random_schedule(random.Random(seed))
    blob = schedule.to_bytes()
    rng = random.Random(seed ^ 0x5EED)
    for _ in range(8):
        index = rng.randrange(len(blob))
        corrupted = bytearray(blob)
        corrupted[index] ^= 1 << rng.randrange(8)
        try:
            decoded = FaultSchedule.from_bytes(bytes(corrupted))
        except ReplayError, struct.error, UnicodeDecodeError:
            continue
        assert decoded == schedule, (
            f"byte {index} was altered and the container still decoded, to a "
            f"different schedule: {decoded} != {schedule}"
        )


def test_a_transport_recording_round_trips_its_segments_and_addresses() -> None:
    recording = TransportRecording(
        segments=(
            TransportSegment(0, int(SegmentKind.DATA), b"\x00\xff" * 40),
            TransportSegment(1_000_000, int(SegmentKind.DATA), b""),
            TransportSegment(2_000_000, int(SegmentKind.RESET), b""),
        ),
        peername=("2001:db8::1", 65535),
        sockname=("", 0),
        build_id=2**63,
    )
    assert TransportRecording.from_bytes(recording.to_bytes()) == recording


def test_the_forensic_reader_recovers_a_torn_tail_and_says_it_was_torn() -> None:
    import io

    from wreath._recording_format import (
        SCHEMA_VERSION,
        MetadataImage,
        WFR1Writer,
        read_recording,
    )

    buffer = io.BytesIO()
    image = MetadataImage(SCHEMA_VERSION, *([()] * 11))
    writer = WFR1Writer(buffer, image)
    writer.write_captures([b"a slab of capture bytes"])
    writer.close()
    whole = buffer.getvalue()

    clean = read_recording(whole)
    assert clean.clean is True
    torn = read_recording(whole[: len(whole) - 4])
    assert torn.clean is False, "a torn recording must not read as a complete one"
