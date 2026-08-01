"""Recording a durable job attempt: the policy, the container, the refusals.

A job attempt recording is identity + cause + boundaries + outcome. These tests
pin each of those, the deny-by-default arming in front of them, and the two
ways a container can lie about what it holds.
"""

from __future__ import annotations

import io

import pytest

from wreath._flight_schema import SCHEMA_VERSION, MetadataImage, SchemaError
from wreath._recording_format import WFR1Writer, read_recording
from wreath.recording import (
    AttemptOutcome,
    AttemptPolicy,
    AttemptRecord,
    AttemptTrigger,
    AttemptTriggerKind,
    BoundaryEvent,
    RecordingPolicyError,
    read_attempt_recording,
)


def _image() -> MetadataImage:
    return MetadataImage(SCHEMA_VERSION, *([()] * 11))


def _record(**overrides) -> AttemptRecord:
    fields = {
        "job_id": 4171,
        "queue": "work",
        "task": "send_password_reset",
        "attempt": 4,
        "max_attempts": 5,
        "tenant": "acme",
        "dedup_key": "work:reset:41",
        "fence": 7,
        "trace_context": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "boundaries": (
            BoundaryEvent(seam=0, target="main", coordinate=0),
            BoundaryEvent(seam=1, target="main", coordinate=2, error_type="PostgresError"),
        ),
        "outcome": AttemptOutcome.RAISED,
        "error_type": "PostgresError",
        "error_message": "server error after the round trip began",
        "argument_count": 2,
    }
    fields.update(overrides)
    return AttemptRecord(**fields)


def _written(*records: AttemptRecord, close: bool = True) -> bytes:
    buffer = io.BytesIO()
    writer = WFR1Writer(buffer, _image())
    for record in records:
        writer.write_attempt(record)
    if close:
        writer.close()
    return buffer.getvalue()


# --- arming ------------------------------------------------------------------


def test_a_policy_with_no_triggers_captures_nothing():
    """Deny by default, and the failing attempt is the tempting exception."""
    policy = AttemptPolicy()
    assert not policy.captures(
        task="send", outcome=AttemptOutcome.RAISED, attempt=1, max_attempts=5, job_id=1
    )
    assert not policy.captures(
        task="send", outcome=AttemptOutcome.COMPLETED, attempt=1, max_attempts=5, job_id=1
    )


def test_arming_on_failure_captures_every_outcome_that_is_not_completion():
    policy = AttemptPolicy(triggers=(AttemptTrigger(AttemptTriggerKind.FAILURE),))
    for outcome in (
        AttemptOutcome.RAISED,
        AttemptOutcome.DEADLINE_CANCELLED,
        AttemptOutcome.LEASE_EXPIRED,
    ):
        assert policy.captures(
            task="send", outcome=outcome, attempt=1, max_attempts=5, job_id=1
        ), outcome
    assert not policy.captures(
        task="send", outcome=AttemptOutcome.COMPLETED, attempt=1, max_attempts=5, job_id=1
    )


def test_a_deadline_cancellation_is_not_folded_into_raised():
    """Four outcomes, not three. `_run_handler` counts a deadline separately in
    `run_timeouts` precisely because nothing failed -- work was stopped -- so a
    policy that wants only genuine raises must be able to say so."""
    policy = AttemptPolicy(triggers=(AttemptTrigger(AttemptTriggerKind.RAISED),))
    assert policy.captures(
        task="send", outcome=AttemptOutcome.RAISED, attempt=1, max_attempts=5, job_id=1
    )
    assert not policy.captures(
        task="send",
        outcome=AttemptOutcome.DEADLINE_CANCELLED,
        attempt=1,
        max_attempts=5,
        job_id=1,
    )


def test_arming_on_final_failure_waits_for_the_attempt_that_exhausts_the_budget():
    policy = AttemptPolicy(triggers=(AttemptTrigger(AttemptTriggerKind.FINAL_FAILURE),))
    assert not policy.captures(
        task="send", outcome=AttemptOutcome.RAISED, attempt=4, max_attempts=5, job_id=1
    )
    assert policy.captures(
        task="send", outcome=AttemptOutcome.RAISED, attempt=5, max_attempts=5, job_id=1
    )
    assert not policy.captures(
        task="send", outcome=AttemptOutcome.COMPLETED, attempt=5, max_attempts=5, job_id=1
    )


def test_arming_by_task_name_captures_that_task_and_no_other():
    policy = AttemptPolicy(
        triggers=(AttemptTrigger(AttemptTriggerKind.TASK, task="import_herd"),)
    )
    assert policy.captures(
        task="import_herd",
        outcome=AttemptOutcome.COMPLETED,
        attempt=1,
        max_attempts=5,
        job_id=1,
    )
    assert not policy.captures(
        task="send_password_reset",
        outcome=AttemptOutcome.COMPLETED,
        attempt=1,
        max_attempts=5,
        job_id=1,
    )


def test_a_sampled_task_trigger_is_deterministic_in_the_job_id():
    """Sampling has to be reproducible from the row, not from an RNG: two
    workers reading the same job must agree on whether it is being recorded."""
    policy = AttemptPolicy(
        triggers=(AttemptTrigger(AttemptTriggerKind.TASK, task="import_herd", rate=0.5),)
    )

    def captured(job_id: int) -> bool:
        return policy.captures(
            task="import_herd",
            outcome=AttemptOutcome.COMPLETED,
            attempt=1,
            max_attempts=5,
            job_id=job_id,
        )

    decisions = [captured(job_id) for job_id in range(400)]
    assert decisions == [captured(job_id) for job_id in range(400)]
    # A rate of 0.5 that selects everything or nothing is a rate nobody applied.
    assert 0 < sum(decisions) < 400
    assert not AttemptPolicy(
        triggers=(AttemptTrigger(AttemptTriggerKind.TASK, task="import_herd", rate=0.0),)
    ).captures(
        task="import_herd",
        outcome=AttemptOutcome.COMPLETED,
        attempt=1,
        max_attempts=5,
        job_id=7,
    )


def test_an_unnamed_task_trigger_is_refused():
    """`TASK` with no task is 'record every attempt', which this subsystem does
    not have a spelling for."""
    with pytest.raises(RecordingPolicyError, match="names the task"):
        AttemptTrigger(AttemptTriggerKind.TASK)


def test_a_rate_outside_the_unit_interval_is_refused():
    with pytest.raises(RecordingPolicyError, match="rate"):
        AttemptTrigger(AttemptTriggerKind.FAILURE, rate=1.5)


def test_a_policy_bound_must_be_positive():
    with pytest.raises(RecordingPolicyError, match="max_boundaries"):
        AttemptPolicy(max_boundaries=0)
    with pytest.raises(RecordingPolicyError, match="out of range"):
        AttemptPolicy(max_boundaries=1 << 20)


def test_a_trigger_kind_may_be_spelled_as_its_string():
    """Policies arrive from configuration as often as from code, and a
    `"failure"` that stayed a `str` would compare unequal to every member and
    arm nothing at all."""
    trigger = AttemptTrigger("failure")
    assert trigger.kind is AttemptTriggerKind.FAILURE
    assert AttemptPolicy(triggers=(trigger,)).captures(
        task="send", outcome=AttemptOutcome.RAISED, attempt=1, max_attempts=5, job_id=1
    )
    with pytest.raises(ValueError, match="not a valid AttemptTriggerKind"):
        AttemptTrigger("whenever")


def test_an_outcome_may_be_spelled_as_its_string_too():
    policy = AttemptPolicy(triggers=(AttemptTrigger(AttemptTriggerKind.FAILURE),))
    assert policy.captures(
        task="send", outcome="raised", attempt=1, max_attempts=5, job_id=1
    )


# --- the trace ---------------------------------------------------------------


def test_a_trace_numbers_each_seam_and_target_separately():
    from wreath.recording import BoundaryTrace

    trace = BoundaryTrace(16)
    assert trace.note(1, "main") == 0
    assert trace.note(1, "main") == 1
    trace.note(1, "other")
    trace.note(6, "objects")
    assert [(e.seam, e.target, e.coordinate) for e in trace.events] == [
        (1, "main", 0), (1, "main", 1), (1, "other", 0), (6, "objects", 0),
    ]


def test_an_overflowing_trace_stops_recording_and_says_so():
    from wreath.recording import BoundaryTrace

    trace = BoundaryTrace(2)
    trace.note(1, "main")
    trace.note(1, "main")
    assert trace.note(1, "main") == -1
    assert trace.overflowed
    assert len(trace.events) == 2
    # Failing a crossing the trace refused to keep is a no-op rather than a
    # rewrite of somebody else's event: the index is not an index any more.
    trace.fail(-1, "PostgresError")
    assert [event.error_type for event in trace.events] == ["", ""]


def test_a_recorder_uses_the_image_it_was_given():
    """A recording made by an application carries that application's metadata
    image, which is what makes its route and dependency ids mean anything. The
    empty stand-in is for a runner with no application, not the default answer."""
    import tempfile

    from wreath._flight_schema import NamedMeta
    from wreath.recording import AttemptRecorder

    # Deliberately *not* the empty image, or the stand-in and the given one
    # would hash the same and this would pass whichever was used.
    image = MetadataImage(
        SCHEMA_VERSION, (), (), (), (), (), (), (), (), (),
        (NamedMeta(entry_id=1, name="main"),), (), (),
    )
    assert image.image_hash_short() != _image().image_hash_short()

    with tempfile.TemporaryDirectory() as directory:
        recorder = AttemptRecorder(AttemptPolicy(), directory=directory, image=image)
        path = recorder.write(_record())
        assert path is not None
        with open(path, "rb") as handle:
            written = read_recording(handle.read())
        assert written.image.image_hash_short() == image.image_hash_short()
        assert recorder.written == 1


# --- the container -----------------------------------------------------------


def test_an_attempt_round_trips_through_the_wfr1_container():
    decoded = read_recording(_written(_record()))
    assert decoded.clean
    assert len(decoded.attempts) == 1
    assert decoded.attempts[0] == _record()


def test_the_attempt_is_a_record_kind_beside_the_others_not_a_second_format():
    """One container, one decoder. The footer counts it, so a reader that never
    heard of an attempt still knows the file holds one."""
    blob = _written(_record())
    assert blob[:4] == b"WFR1"
    decoded = read_recording(blob)
    assert decoded.footer_attempts == 1
    assert decoded.slabs == ()
    assert decoded.events == ()


def test_every_recorded_fact_survives_the_round_trip():
    decoded = read_attempt_recording(_written(_record()))
    original = _record()
    assert decoded.job_id == original.job_id == 4171
    assert decoded.queue == "work"
    assert decoded.task == "send_password_reset"
    assert decoded.attempt == 4
    assert decoded.max_attempts == 5
    assert decoded.tenant == "acme"
    assert decoded.dedup_key == "work:reset:41"
    assert decoded.fence == 7
    assert decoded.trace_context == original.trace_context
    assert decoded.outcome == "raised"
    assert decoded.error_type == "PostgresError"
    assert decoded.error_message == "server error after the round trip began"
    assert decoded.argument_count == 2
    assert decoded.boundaries == original.boundaries


def test_a_truncated_attempt_recording_is_refused_by_name():
    """Not recovered, not half-decoded. A recording whose tail was torn is the
    one thing a reader must not quietly report as complete."""
    blob = _written(_record())
    for cut in (len(blob) - 1, len(blob) // 2):
        with pytest.raises(SchemaError, match="truncated"):
            read_attempt_recording(blob[:cut])


def test_an_attempt_chunk_that_declares_more_than_it_holds_is_refused_by_name():
    blob = bytearray(_written(_record()))
    marker = blob.index(b"ATT1")
    # The record's own declared total, three fields into its header.
    declared = int.from_bytes(blob[marker + 8 : marker + 12], "little")
    blob[marker + 8 : marker + 12] = (declared + 16).to_bytes(4, "little")
    # Re-checksum the chunk so the failure is the record's, not the container's.
    _recrc(blob, marker)
    with pytest.raises(SchemaError, match="truncated"):
        read_attempt_recording(bytes(blob))


def test_a_chunked_attempt_recording_is_refused_by_name():
    """An attempt split across chunks would decode as the prefix that fits and
    say nothing about the rest, which is a recording of less than it claims."""
    blob = bytearray(_written(_record()))
    marker = blob.index(b"ATT1")
    blob[marker + 5] |= 0x01  # the continuation flag
    _recrc(blob, marker)
    with pytest.raises(SchemaError, match="chunked"):
        read_attempt_recording(bytes(blob))


def test_a_container_holding_two_attempts_is_refused_rather_than_guessed_at():
    with pytest.raises(SchemaError, match="2 attempt recordings"):
        read_attempt_recording(_written(_record(), _record(job_id=99)))


def test_a_container_holding_no_attempt_is_refused_by_name():
    buffer = io.BytesIO()
    WFR1Writer(buffer, _image()).close()
    with pytest.raises(SchemaError, match="no attempt recording"):
        read_attempt_recording(buffer.getvalue())


def test_an_attempt_recording_with_no_footer_is_refused():
    """No footer means the process died mid-write. Every boundary after the
    tear is missing and nothing in the bytes says how many."""
    with pytest.raises(SchemaError, match="truncated"):
        read_attempt_recording(_written(_record(), close=False))


def test_the_boundary_trace_keeps_its_order():
    record = _record(
        boundaries=tuple(
            BoundaryEvent(seam=1, target="main", coordinate=index) for index in range(9)
        )
    )
    decoded = read_attempt_recording(_written(record))
    assert [b.coordinate for b in decoded.boundaries] == list(range(9))


# --- what the record decoder refuses on its own ------------------------------
#
# Reached through `AttemptRecord.decode` rather than through a whole file: the
# container recovers a torn tail by design, so a chunk that never arrives is
# *dropped* there and these refusals are never consulted. They are what stops a
# chunk that did arrive from being read as something it is not.


def test_a_record_shorter_than_its_header_is_refused_by_name():
    with pytest.raises(SchemaError, match="shorter than its 12-byte header"):
        AttemptRecord.decode(b"ATT1\x01")


def test_a_payload_that_is_not_a_record_is_refused_by_name():
    payload = bytearray(_record().encode())
    payload[:4] = b"XXXX"
    with pytest.raises(SchemaError, match="bad record magic"):
        AttemptRecord.decode(bytes(payload))


def test_a_record_from_a_future_version_is_refused_rather_than_guessed_at():
    payload = bytearray(_record().encode())
    payload[4] = 2
    with pytest.raises(SchemaError, match="unsupported attempt record version 2"):
        AttemptRecord.decode(bytes(payload))


def test_a_record_torn_inside_a_text_field_is_refused_by_name():
    """The declared total still matches, because the tear is *inside* the body:
    a field says it is longer than what follows it."""
    from wreath._recording_format import _ATTEMPT_FIXED, _ATTEMPT_HEADER

    payload = bytearray(_record().encode())
    # The `queue` field's length prefix, immediately after the fixed block.
    offset = _ATTEMPT_HEADER.size + _ATTEMPT_FIXED.size
    payload[offset : offset + 4] = (1 << 20).to_bytes(4, "little")
    with pytest.raises(SchemaError, match="a field declares 1048576 bytes"):
        AttemptRecord.decode(bytes(payload))


def test_an_error_message_is_clamped_to_what_the_queue_row_itself_keeps():
    """`last_error` is `error[:2000]`, so a recording that held more of a
    failure than the row does would be describing something nobody can see in
    the queue."""
    from wreath._recording_format import MAX_ERROR_MESSAGE

    decoded = read_attempt_recording(_written(_record(error_message="x" * 9000)))
    assert len(decoded.error_message) == MAX_ERROR_MESSAGE == 2000


def test_a_footer_written_before_the_attempt_record_kind_still_reports_its_counts():
    """The attempt count was *appended* after the footer rather than widening
    it. Widening would have made an older footer fail the length check and
    silently report zero slabs and zero cells -- a recording that looks empty."""
    import struct

    blob = bytearray(_written(_record()))
    footer_payload = blob.rindex(b"FOOT") + 12
    legacy = bytes(blob[:footer_payload]) + bytes(blob[footer_payload:footer_payload + 24])
    legacy = bytearray(legacy)
    struct.pack_into("<4sII", legacy, footer_payload - 12, b"FOOT", 24,
                     __import__("zlib").crc32(bytes(legacy[footer_payload:])) & 0xFFFFFFFF)
    decoded = read_recording(bytes(legacy))
    assert decoded.clean
    assert decoded.footer_attempts == 0  # the old footer never carried one
    assert decoded.footer_capture_slabs == 0
    assert len(decoded.attempts) == 1  # ... and the record itself still decodes


def _recrc(blob: bytearray, marker: int) -> None:
    """Recompute the enclosing chunk's CRC after editing its payload."""
    import struct
    import zlib

    header = marker - 12  # `_CHUNK` is tag(4) + length(4) + crc(4)
    tag, length, _ = struct.unpack_from("<4sII", blob, header)
    payload = bytes(blob[marker : marker + length])
    struct.pack_into("<4sII", blob, header, tag, length, zlib.crc32(payload) & 0xFFFFFFFF)
