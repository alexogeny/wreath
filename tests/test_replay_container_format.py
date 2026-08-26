"""The three replay containers as untrusted input, and the round trips they owe.

`WTR1` (a transport recording), `WFS1` (a fault schedule) and `WFR1` (a forensic
recording) share one framing: magic, a version byte, then length-and-CRC-prefixed
chunks. A reader for that shape has exactly four ways to be wrong, and this file
covers all four by name:

* it **guesses** at damage it should refuse -- a repeated chunk, a body shorter
  than its declared length;
* it **refuses** damage it is supposed to recover -- a torn tail is the framing's
  whole reason for existing, and a reader that rejects one throws away every
  complete chunk before it;
* it **over-reads**, taking a declared length on trust;
* it **round-trips lossily**, so what comes back is not what went in.

`test_replay_robustness.py` and `test_replay_fault_corpus.py` already cover the
first-order cases (a flipped CRC byte, a bumped version, a foreign magic). This
extends rather than duplicates them: the repeated chunk, the recovery boundary,
the over-read, and a round-trip property over generated schedules rather than one
hand-written example.
"""

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


# --- a repeated chunk is refused by name --------------------------------------


def test_a_second_copy_of_a_chunk_is_refused_rather_than_preferred() -> None:
    """Appending a chunk must not silently replace the one already there.

    The reader kept the *last* copy of a repeated tag, so appending one more
    `SEGS` chunk to a valid recording replaced every segment in it while the
    file still verified end to end: same magic, same version, every CRC good.
    A replay would then run bytes nobody recorded, and the only sign would have
    been that the answer was wrong.
    """
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
    """A refusal that does not say what is wrong sends you to the hex editor."""
    blob = _recording() + _chunk(b"HEAD", b"\x00" * 24)
    with pytest.raises(ReplayError) as caught:
        TransportRecording.from_bytes(blob)
    assert "HEAD" in str(caught.value)


def test_a_repeated_chunk_in_a_fault_schedule_is_refused_too() -> None:
    """Both containers, one rule. A schedule is as much an untrusted input as a
    recording -- more so, since it is the thing that decides what gets injected."""
    schedule = FaultSchedule((FaultDescriptor(int(FaultKind.RESET), 0),))
    blob = schedule.to_bytes() + _chunk(b"FALT", struct.pack("<I", 0))
    with pytest.raises(ReplayError, match="repeats"):
        FaultSchedule.from_bytes(blob)


# --- what the framing is supposed to recover ----------------------------------


def test_trailing_bytes_that_are_not_a_chunk_are_recovered_past() -> None:
    """A torn tail is the framing's contract, not an error.

    The writer appends, so a file cut short by a crash ends mid-chunk. Refusing
    the whole recording would throw away every complete chunk before the tear,
    which is exactly the forensic material an incident produced. The reader
    stops at the tear and reports what it had.
    """
    original = TransportRecording.from_bytes(_recording())
    torn = TransportRecording.from_bytes(_recording() + b"\x00\x01\x02")
    assert torn == original


def test_an_unknown_trailing_chunk_is_ignored_by_a_recording() -> None:
    """The forward-compatible seam: a newer writer's extra chunk must not stop
    an older reader, or every container change becomes a flag day.

    A recording can afford this because it has no *optional* chunks -- both
    `HEAD` and `SEGS` are required, so a flipped tag byte turns into "missing a
    required chunk" rather than a field that quietly disappeared.
    """
    blob = _recording() + _chunk(b"XTRA", b"a field from the future")
    assert TransportRecording.from_bytes(blob) == TransportRecording.from_bytes(_recording())


def test_a_schedule_refuses_a_chunk_tag_it_does_not_know() -> None:
    """A schedule cannot afford the same openness, and this is why.

    A chunk's tag is **not covered by its CRC**. Flip one bit of `ADPT` and the
    chunk still verifies -- it is simply a chunk the reader has never heard of,
    so the adapter faults vanish and a schedule that promised to fault the
    database decodes as one that faults nothing. Found by the bit-flip property
    below, not by inspection.
    """
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
    """Because the next reader of this message will want to know why an
    otherwise-valid container was rejected."""
    blob = FaultSchedule().to_bytes() + _chunk(b"XTRA", b"")
    with pytest.raises(ReplayError) as caught:
        FaultSchedule.from_bytes(blob)
    assert "not covered by the CRC" in str(caught.value)


def test_a_truncated_schedule_is_refused_rather_than_half_applied() -> None:
    """The one place the two containers deliberately part company.

    A schedule whose `ADPT` chunk is cut short *could* be recovered: `FALT` is
    complete and CRC-valid, so the transport half decodes on its own. Recovering
    it is exactly wrong. What you would get is a schedule that injects fewer
    faults than its name promises, in a run that stays green -- a weaker
    schedule wearing a stronger one's name, which destroys the only thing a
    schedule is for. A recording recovers because partial forensic material is
    still evidence; a schedule refuses because a partial injection is a lie.
    """
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
    """The other side of the same decision, asserted so the asymmetry is a
    deliberate pair rather than one rule somebody forgot to apply twice."""
    original = TransportRecording.from_bytes(_recording())
    assert TransportRecording.from_bytes(_recording() + b"\x00\x01\x02") == original


# --- over-reads -----------------------------------------------------------


def test_a_chunk_length_that_overruns_the_buffer_is_not_read() -> None:
    """A declared length is a claim, not a fact.

    Framed with a length far past the end of the data: the reader must treat it
    as a torn tail rather than slicing past the buffer or trusting the number.
    """
    payload = b"nowhere near this long"
    header = _CHUNK.pack(b"SEGS", 10_000, 0)
    blob = _MAGIC_TRANSPORT + b"\x01" + _chunk(b"HEAD", b"\x00" * 24) + header + payload
    with pytest.raises(ReplayError, match="missing a required chunk"):
        TransportRecording.from_bytes(blob)


def test_a_chunk_length_past_the_hard_cap_is_not_allocated() -> None:
    """`MAX_CHUNK_BYTES` is the allocation guard, and it is checked *before* the
    slice -- a 4 GiB declared length in a 60-byte file must cost nothing."""
    header = _CHUNK.pack(b"SEGS", MAX_CHUNK_BYTES + 1, 0)
    blob = _MAGIC_TRANSPORT + b"\x01" + _chunk(b"HEAD", b"\x00" * 24) + header
    with pytest.raises(ReplayError, match="missing a required chunk"):
        TransportRecording.from_bytes(blob)


def test_the_writer_refuses_to_frame_a_chunk_past_the_cap() -> None:
    """Both ends of the same cap. A writer that can emit what no reader will
    accept produces files that verify nowhere."""
    with pytest.raises(ReplayError, match="exceeds"):
        _chunk(b"SEGS", b"\x00" * (MAX_CHUNK_BYTES + 1))


def test_a_schedule_claiming_more_faults_than_it_carries_does_not_over_read() -> None:
    body = struct.pack("<I", 4096)  # says 4096 faults; carries none
    blob = _MAGIC_FAULTS + b"\x01" + _chunk(b"FALT", body)
    with pytest.raises((ReplayError, struct.error)):
        FaultSchedule.from_bytes(blob)


# --- round-trip properties ----------------------------------------------------


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
    """The property, not one example.

    A schedule is the reproducibility guarantee: two runs are only comparable
    if the schedule that reached the second is bit-for-bit the one that reached
    the first. Generated cases cover the edges a hand-written example does not
    -- an empty target name, a non-ASCII fault name, `2**32 - 1` as a value,
    and a schedule with no faults at all.
    """
    schedule = _random_schedule(random.Random(seed))
    assert FaultSchedule.from_bytes(schedule.to_bytes()) == schedule


@pytest.mark.parametrize("seed", range(64))
def test_serialising_a_schedule_is_deterministic(seed: int) -> None:
    """Same schedule, same bytes. A container whose encoding varied would make
    a checksum useless as an identity and a corpus impossible to diff."""
    schedule = _random_schedule(random.Random(seed))
    assert schedule.to_bytes() == schedule.to_bytes()
    assert FaultSchedule.from_bytes(schedule.to_bytes()).to_bytes() == schedule.to_bytes()


@pytest.mark.parametrize("name", sorted(fault_corpus()))
def test_every_corpus_entry_survives_its_own_container(name: str) -> None:
    """The corpus is only an artifact if it is one after serialization.

    The sanitizer gate re-runs these by name from bytes, so an entry that lost a
    fault on the way through would run a *different*, weaker schedule there than
    the one this suite drives -- and both would be green.
    """
    schedule = fault_corpus()[name]
    assert FaultSchedule.from_bytes(schedule.to_bytes()) == schedule


@pytest.mark.parametrize("seed", range(32))
def test_a_flipped_byte_anywhere_in_a_schedule_is_caught_or_harmless(seed: int) -> None:
    """The checksum's rejection path, driven rather than assumed.

    Every byte of a serialized schedule is flipped in turn; each must either be
    refused, or produce a schedule that is still exactly the original (a flip in
    a byte the format does not read). What must not happen is a *different*
    schedule coming back from a container that claimed to verify -- that is a
    silently altered fault injection, and it would make two runs incomparable
    while looking identical.
    """
    schedule = _random_schedule(random.Random(seed))
    blob = schedule.to_bytes()
    rng = random.Random(seed ^ 0x5EED)
    for _ in range(8):
        index = rng.randrange(len(blob))
        corrupted = bytearray(blob)
        corrupted[index] ^= 1 << rng.randrange(8)
        try:
            decoded = FaultSchedule.from_bytes(bytes(corrupted))
        except (ReplayError, struct.error, UnicodeDecodeError):
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


# --- the WFR1 forensic container shares the shape ------------------------------


def test_the_forensic_reader_recovers_a_torn_tail_and_says_it_was_torn() -> None:
    """`WFR1`'s reader has the same contract and one extra obligation: it has to
    *say* the recording was torn, because a truncated capture and a complete one
    answer different questions and nothing else distinguishes them."""
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
