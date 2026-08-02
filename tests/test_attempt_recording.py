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


# --- `wreath flight read` reaches the record kind ----------------------------
#
# The decoder shipped with the record kind and the *dispatch* did not, so a
# `.wfr1` handed to `flight read` was answered with the ring reader's complaint
# about a `WFRR` magic: a true statement about the wrong thing, to somebody who
# had already had one crash that day. `flight read` now reads the magic first.


def _flight(*argv: str) -> int:
    from wreath import cli

    return cli.main(["flight", "read", *argv])


def _wfr1(tmp_path, *records: AttemptRecord, close: bool = True):
    path = tmp_path / "attempt.wfr1"
    path.write_bytes(_written(*records, close=close))
    return str(path)


def test_flight_read_decodes_an_attempt_recording(tmp_path, capsys) -> None:
    """Success: the identity, the outcome and the boundary trace, in the file
    the recorder actually wrote."""
    assert _flight(_wfr1(tmp_path, _record())) == 0
    out = capsys.readouterr().out
    assert "WFR1 container" in out
    assert "send_password_reset job 4171" in out
    assert "attempt 4 of 5 -> raised" in out
    assert "raised PostgresError" in out
    assert "fence 7" in out
    assert "boundary seam 1" in out
    assert "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01" in out


def test_flight_read_says_when_no_argument_was_allowed(tmp_path, capsys) -> None:
    """The count is forensic; the values are absent unless an operator named
    one, and a reader looking for them should learn that here rather than
    concluding the file was truncated."""
    assert _flight(_wfr1(tmp_path, _record())) == 0
    assert "2 argument(s), none allowed by name" in capsys.readouterr().out


def test_flight_read_prints_a_captured_argument(tmp_path, capsys) -> None:
    record = _record(arguments=(("user_id", '{"value":41}'),))
    assert _flight(_wfr1(tmp_path, record)) == 0
    assert 'arg user_id = {"value":41}' in capsys.readouterr().out


def test_flight_read_emits_versioned_json_for_a_recording(tmp_path, capsys) -> None:
    import json

    assert _flight(_wfr1(tmp_path, _record()), "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["container"] == "WFR1"
    assert payload["clean"] is True
    assert payload["attempts"][0]["task"] == "send_password_reset"
    assert payload["attempts"][0]["boundaries"][1]["error_type"] == "PostgresError"


def test_flight_read_refuses_a_truncated_recording_by_name(tmp_path, capsys) -> None:
    """Malformed: no footer means the process died mid-write, and what is
    missing cannot be counted. Reported, and the exit code says so."""
    path = tmp_path / "torn.wfr1"
    path.write_bytes(_written(_record(), close=False))
    assert _flight(str(path)) == 0
    out = capsys.readouterr().out
    assert "no footer" in out
    assert out.index("no footer") < out.index("send_password_reset"), (
        "a torn file read as a complete one loses the records nearest the "
        "failure, so the tear is stated before the contents"
    )


def test_flight_read_propagates_a_decoder_refusal_as_a_message(tmp_path, capsys) -> None:
    """Error propagation: a `SchemaError` from the decoder is a usage failure
    with exit 2, not a traceback -- the same contract the ring path has."""
    path = tmp_path / "corrupt.wfr1"
    blob = bytearray(_written(_record()))
    blob[8:12] = b"\xff\xff\xff\xff"  # an unreadable schema version
    path.write_bytes(bytes(blob))
    assert _flight(str(path)) == 2
    assert capsys.readouterr().err, "the refusal must name something"


def test_flight_read_refuses_a_transport_recording_naming_the_command(
    tmp_path, capsys
) -> None:
    """Unsupported, and the useful kind of unsupported.

    A `WTR1` is a recorder file, just not this command's. Left to the ring
    reader it answered "not a wreath ring file" -- true, and it sends the
    reader looking for a different *file* rather than for a different command.
    """
    path = tmp_path / "connection.wtr1"
    path.write_bytes(b"WTR1" + b"\x00" * 512)
    assert _flight(str(path)) == 2
    err = capsys.readouterr().err
    assert "WTR1" in err
    assert "wreath replay transport" in err


def test_flight_read_refuses_a_file_that_is_no_container_at_all(
    tmp_path, capsys
) -> None:
    """Neither a ring, a recording, nor a transport recording. The ring reader
    owns this message, because "what even is this file" is its question."""
    path = tmp_path / "elsewhere.bin"
    path.write_bytes(b"ZZZZ" + b"\x00" * 512)
    assert _flight(str(path)) == 2
    assert "ring file" in capsys.readouterr().err.lower()


def test_flight_read_still_reads_a_ring_file(tmp_path) -> None:
    """The dispatch must not have moved the ring path. Its own suite is
    `tests/test_flight_ring_file.py`; this is the pin that the magic check
    sits in front of it rather than instead of it."""
    from wreath._cli import _flight_magic

    path = tmp_path / "attempt.wfr1"
    path.write_bytes(_written(_record()))
    assert _flight_magic(str(path)) == b"WFR1"

    ring = tmp_path / "ring.wfrr"
    ring.write_bytes(b"WFRR" + b"\x00" * 16)
    assert _flight_magic(str(ring)) == b"WFRR"


# --- capturing an argument, safely -------------------------------------------
#
# `args jsonb` is positional and `RedactionPolicy` is name-keyed, so the names
# come from the *handler's signature* -- the one place in the recording process
# that has them. Everything below is about the four rules that make that safe,
# and every one of them fails closed.

import json as _json  # noqa: E402 - the section it belongs to starts here


def _policy(*names: str, **limits) -> AttemptPolicy:
    from wreath.recording import RedactionPolicy

    bounds = {"max_fields": 32, "max_depth": 4, "max_body_bytes": 4096}
    bounds.update(limits)
    return AttemptPolicy(
        argument_allowlist=frozenset(names), redaction=RedactionPolicy(**bounds)
    )


def send_password_reset(user_id, token):
    """The example the whole design note is written around."""


def test_no_allowlist_captures_nothing() -> None:
    """The default, and it is the behaviour that shipped before this existed."""
    assert AttemptPolicy().capture_arguments(
        task="send_password_reset",
        handler=send_password_reset,
        args=(41, "reset-token"),
        kwargs={},
    ) == ()


def test_one_parameter_is_captured_and_its_neighbour_is_not() -> None:
    """The whole point: `user_id` yes, `token` never."""
    captured = _policy("send_password_reset.user_id").capture_arguments(
        task="send_password_reset",
        handler=send_password_reset,
        args=(41, "reset-token-nobody-may-keep"),
        kwargs={},
    )
    assert captured == (("user_id", '{"value":41}'),)
    assert "reset-token-nobody-may-keep" not in repr(captured)


def test_an_allowlist_for_another_task_captures_nothing() -> None:
    """The key is `task.parameter`, so `other.user_id` does not reach this one."""
    assert _policy("other_task.user_id").capture_arguments(
        task="send_password_reset",
        handler=send_password_reset,
        args=(41, "t"),
        kwargs={},
    ) == ()


def test_a_task_this_process_cannot_resolve_captures_nothing() -> None:
    """Rule 1. The dead-letter path already meets a row whose handler is gone;
    falling back to position would record whatever happened to be first."""
    assert _policy("send_password_reset.user_id").capture_arguments(
        task="send_password_reset", handler=None, args=(41, "t"), kwargs={}
    ) == ()


def test_a_call_that_does_not_bind_captures_nothing() -> None:
    """Rule 1 again, in its other shape: a row enqueued by a release whose
    handler took a different arity."""
    assert _policy("send_password_reset.user_id").capture_arguments(
        task="send_password_reset",
        handler=send_password_reset,
        args=(41, "t", "extra"),
        kwargs={},
    ) == ()


def test_a_value_that_lands_in_varargs_is_never_named() -> None:
    """Rule 2. `*rest` has no declared parameter to allow, so no spelling of
    the allowlist reaches it."""

    def fan_out(first, *rest, **options):
        pass

    captured = _policy(
        "fan_out.first", "fan_out.rest", "fan_out.options", "fan_out.secret"
    ).capture_arguments(
        task="fan_out",
        handler=fan_out,
        args=(1, "in-varargs"),
        kwargs={"secret": "in-kwargs"},
    )
    assert [name for name, _ in captured] == ["first"]
    assert "in-varargs" not in repr(captured)
    assert "in-kwargs" not in repr(captured)


def test_a_defaulted_parameter_the_call_omitted_is_absent() -> None:
    """Not withheld -- absent. The job did not carry it, and recording a
    default the caller never sent would be a recording of this process."""

    def report(period, verbose=True):
        pass

    captured = _policy("report.period", "report.verbose").capture_arguments(
        task="report", handler=report, args=("day",), kwargs={}
    )
    assert [name for name, _ in captured] == ["period"]


def test_nested_structure_is_captured_whole_under_the_bounds() -> None:
    """Rule 3: the parameter is the unit of consent, and it is the whole
    argument. There is no per-field key space, and the docstring says so."""

    def ingest(payload):
        pass

    captured = _policy("ingest.payload").capture_arguments(
        task="ingest",
        handler=ingest,
        args=({"a": [1, 2, {"b": None}], "c": True},),
        kwargs={},
    )
    assert _json.loads(captured[0][1]) == {
        "value": {"a": [1, 2, {"b": None}], "c": True}
    }


def test_an_unsupported_type_is_withheld_with_its_reason() -> None:
    """Rule 4, and the reason is recorded rather than the value dropped: an
    operator who allowed a parameter and finds nothing cannot otherwise tell an
    absence from a refusal."""

    def ingest(payload):
        pass

    captured = _policy("ingest.payload").capture_arguments(
        task="ingest", handler=ingest, args=(object(),), kwargs={}
    )
    assert _json.loads(captured[0][1]) == {"withheld": "unsupported type object"}


def test_bytes_are_not_a_string(tmp_path) -> None:
    """`bytes` has a `repr`, which is exactly why it must not be captured: a
    forensic file is not the place to discover what was in a blob."""

    def ingest(payload):
        pass

    captured = _policy("ingest.payload").capture_arguments(
        task="ingest", handler=ingest, args=(b"\x00secret",), kwargs={}
    )
    assert _json.loads(captured[0][1]) == {"withheld": "unsupported type bytes"}
    assert "secret" not in captured[0][1]


def test_a_cycle_is_withheld_rather_than_recursed() -> None:
    def ingest(payload):
        pass

    loop: list = [1]
    loop.append(loop)
    captured = _policy("ingest.payload").capture_arguments(
        task="ingest", handler=ingest, args=(loop,), kwargs={}
    )
    assert _json.loads(captured[0][1]) == {"withheld": "contains a cycle"}


def test_the_same_list_twice_is_not_a_cycle() -> None:
    """The cycle check is on the path, not on everything seen -- otherwise a
    value that appears twice side by side is refused for no reason."""

    def ingest(payload):
        pass

    shared = [1, 2]
    captured = _policy("ingest.payload").capture_arguments(
        task="ingest", handler=ingest, args=([shared, shared],), kwargs={}
    )
    assert _json.loads(captured[0][1]) == {"value": [[1, 2], [1, 2]]}


def test_depth_past_the_limit_withholds_the_whole_argument() -> None:
    """The *whole* argument. A truncated structure is the shape this subsystem
    refuses everywhere else: a reader cannot tell three from the first three of
    nine."""

    def ingest(payload):
        pass

    deep = {"a": {"b": {"c": {"d": 1}}}}
    captured = _policy("ingest.payload", max_depth=2).capture_arguments(
        task="ingest", handler=ingest, args=(deep,), kwargs={}
    )
    assert _json.loads(captured[0][1]) == {"withheld": "nested deeper than the 2-level limit"}


def test_more_fields_than_the_budget_withholds_the_whole_argument() -> None:
    def ingest(payload):
        pass

    captured = _policy("ingest.payload", max_fields=4).capture_arguments(
        task="ingest", handler=ingest, args=(list(range(50)),), kwargs={}
    )
    assert "max_fields" in _json.loads(captured[0][1])["withheld"]


def test_a_value_over_the_byte_budget_is_withheld() -> None:
    def ingest(payload):
        pass

    captured = _policy(
        "ingest.payload", max_body_bytes=16, max_fields=4096
    ).capture_arguments(
        task="ingest", handler=ingest, args=("x" * 4000,), kwargs={}
    )
    assert "argument budget" in _json.loads(captured[0][1])["withheld"]
    assert "xxxx" not in captured[0][1], "a refusal must not carry the value"


def test_a_non_string_mapping_key_is_withheld() -> None:
    def ingest(payload):
        pass

    captured = _policy("ingest.payload").capture_arguments(
        task="ingest", handler=ingest, args=({1: "a"},), kwargs={}
    )
    assert "int" in _json.loads(captured[0][1])["withheld"]


def test_a_non_finite_number_is_withheld_rather_than_raising() -> None:
    """`json.dumps(allow_nan=False)` would raise from inside the serialiser,
    past the path that records a reason -- and a recorder that can take a
    worker down with it is worse than no recorder."""

    def ingest(payload):
        pass

    captured = _policy("ingest.payload").capture_arguments(
        task="ingest", handler=ingest, args=(float("nan"),), kwargs={}
    )
    assert "non-finite" in _json.loads(captured[0][1])["withheld"]


def test_the_capture_is_immutable_against_a_handler_that_mutates_after() -> None:
    """Normalised into new containers and serialised immediately, so a handler
    that keeps its argument and edits it cannot change what was recorded."""

    def ingest(payload):
        pass

    payload = {"a": [1]}
    captured = _policy("ingest.payload").capture_arguments(
        task="ingest", handler=ingest, args=(payload,), kwargs={}
    )
    payload["a"].append(999)
    payload["b"] = "added later"
    assert _json.loads(captured[0][1]) == {"value": {"a": [1]}}


def test_an_allowlist_without_bounds_is_refused_where_it_is_written() -> None:
    """A policy that names an argument but sets no limits would capture a value
    of unknown shape into a file nobody can open."""
    with pytest.raises(RecordingPolicyError, match="needs redaction limits"):
        AttemptPolicy(argument_allowlist=frozenset({"t.p"}))


def test_a_malformed_allowlist_key_is_refused() -> None:
    with pytest.raises(RecordingPolicyError, match="must be 'task.parameter'"):
        AttemptPolicy(argument_allowlist=frozenset({"no_dot"}))


def test_a_dotted_task_name_still_names_one_parameter() -> None:
    """Split on the last dot, so `billing.reconcile.month` is the `month`
    parameter of `billing.reconcile` and not a path into something."""

    def reconcile(month):
        pass

    captured = _policy("billing.reconcile.month").capture_arguments(
        task="billing.reconcile", handler=reconcile, args=("2026-07",), kwargs={}
    )
    assert captured == (("month", '{"value":"2026-07"}'),)


def test_a_captured_argument_round_trips_through_the_container() -> None:
    """The record kind carries it, and a recording written before this existed
    still decodes -- the section is appended, not folded into the header."""
    record = _record(arguments=(("user_id", '{"value":41}'),))
    decoded = read_attempt_recording(_written(record))
    assert decoded.arguments == (("user_id", '{"value":41}'),)
    assert read_attempt_recording(_written(_record())).arguments == ()


def test_a_framework_supplied_parameter_is_never_capturable() -> None:
    """`JobRunner` calls `handler(ctx, *job.args)`, so `ctx` occupies the first
    parameter and is this process's object rather than anything the queue row
    carried. It is aligned past, and an allowlist entry naming it records
    nothing -- otherwise an operator could point the recorder at a live
    database handle."""

    def send(ctx, address, token):
        pass

    captured = _policy("send.ctx", "send.address").capture_arguments(
        task="send",
        handler=send,
        args=("alex@example.com", "reset-token"),
        kwargs={},
        framework_parameters=1,
    )
    assert captured == (("address", '{"value":"alex@example.com"}'),)


def test_a_cycle_through_a_mapping_is_withheld_too() -> None:
    """The list case has its own test above; a dict closes the loop just as
    easily, and the two are separate branches."""

    def ingest(payload):
        pass

    loop: dict = {"a": 1}
    loop["self"] = loop
    captured = _policy("ingest.payload").capture_arguments(
        task="ingest", handler=ingest, args=(loop,), kwargs={}
    )
    assert _json.loads(captured[0][1]) == {"withheld": "contains a cycle"}


def test_an_infinite_number_is_withheld_like_a_nan() -> None:
    """`allow_nan=False` refuses both, and both must be caught before the
    serialiser sees them -- a raise from inside `json.dumps` escapes past the
    path that records a reason."""

    def ingest(payload):
        pass

    for value in (float("inf"), float("-inf")):
        captured = _policy("ingest.payload").capture_arguments(
            task="ingest", handler=ingest, args=(value,), kwargs={}
        )
        assert "non-finite" in _json.loads(captured[0][1])["withheld"], value


@pytest.mark.parametrize("missing", ["max_fields", "max_depth", "max_body_bytes"])
def test_each_bound_is_required_on_its_own(missing: str) -> None:
    """One refusal, three conditions -- so three tests.

    A single test that leaves all three unset passes whichever clause fired,
    which is the shape `AGENTS.md` names: a refusal test that proves only that
    *something* refused. Each of these sets the other two and leaves one at
    zero.
    """
    from wreath.recording import RedactionPolicy

    bounds = {"max_fields": 32, "max_depth": 4, "max_body_bytes": 4096}
    bounds[missing] = 0
    with pytest.raises(RecordingPolicyError, match="needs redaction limits"):
        AttemptPolicy(
            argument_allowlist=frozenset({"t.p"}), redaction=RedactionPolicy(**bounds)
        )
