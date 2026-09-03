"""`WFR1` recording container + async recording sink (Stage 5, slice 5c).

The `WFR1` container is the on-disk forensic recording: a fixed header (magic,
versions, the application metadata hash, a recording UUID, clock calibration, and
a build id), a metadata chunk that gives the numeric ids meaning, and a stream of
checksummed chunks -- capture slabs (`CAPT`), optional completion cells
(`EVNT`), job attempts (`ATMP`) and workflow steps (`WFST`) -- terminated by a
footer (`FOOT`) on a clean close. **Record kinds are added, never containers**:
one decoder, one reader, one set of forensics tooling, and a footer that grows
by appending counts so a reader predating a kind still reads every count it
knows about. A reader rejects
an unsupported major version or a metadata-hash mismatch outright, but recovers
every complete, checksummed chunk from a file whose tail was torn off by an abrupt
termination (it reports whether the footer was present, i.e. whether the close was
clean).

The `RecordingSink` is the async, disk-facing consumer of capture slabs --
the sibling of the Stage-4 `ExportPipeline`. It owns one
background thread that drains the recorder's committed slabs (it is the *only*
consumer of the capture commit ring) and appends them to an owner-only `WFR1`
file. Following the plan's rule that a recording is never written from request
code and its failure never touches application work, a disk-full or write error
drops the recording output and counts it; the sink keeps draining so the bounded
slab pool never backs up, and the request path is untouched throughout.
"""

from __future__ import annotations

import os
import struct
import threading
import time
import uuid
import zlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from ._flight_schema import (
    CAPTURE_SLAB_HEADER_SIZE,
    CELL_SIZE,
    SCHEMA_VERSION,
    CaptureSlab,
    MetadataImage,
    SchemaError,
    decode_metadata_image,
)
from ._native import _core

__all__ = [
    "MAGIC",
    "WFR1Writer",
    "DecodedRecording",
    "read_recording",
    "RecordingSink",
    "AttemptOutcome",
    "BoundaryEvent",
    "AttemptRecord",
    "read_attempt_recording",
    "WorkflowStepOutcome",
    "WorkflowStepRecord",
    "read_step_recording",
]

#: Container magic. The trailing digit is the container (not schema) version.
MAGIC: Final = b"WFR1"
_CONTAINER_VERSION: Final = 1

#: Refuse to allocate for an absurd declared chunk size before bytes are present.
MAX_CHUNK_BYTES: Final = 256 * 1024 * 1024

# Fixed header (little-endian). Everything after the magic/versions is stable
# provenance a reader can trust before it reads a single chunk.
#   magic 4s | container_ver u8 | schema_ver u8 | flags u16
#   image_hash 16s | recording_uuid 16s
#   created_unix_nano u64 | clock_mono_ns u64 | clock_unix_ns u64
#   build_id u64 | reserved u64
_HEADER = struct.Struct("<4sBBH16s16sQQQQQ")
_CHUNK = struct.Struct("<4sII")  # tag, byte_length, crc32
_FOOTER = struct.Struct("<QQQ")  # chunk_count, capture_slabs, event_cells
#: Appended after `_FOOTER` rather than widening it. A reader that predates the
#: attempt record kind stops at `_FOOTER.size` and still gets every count it
#: knows about; widening the struct instead would have made an old footer fail
#: the length check and silently report zero slabs and zero cells.
_FOOTER_ATTEMPTS = struct.Struct("<Q")  # attempt records

#: Appended after `_FOOTER_ATTEMPTS` for the same reason that was appended after
#: `_FOOTER`: a reader that predates the workflow-step record kind stops where it
#: has always stopped and still gets every count it knows about.
_FOOTER_STEPS = struct.Struct("<Q")  # workflow-step records

_TAG_META: Final = b"META"
_TAG_CAPTURE: Final = b"CAPT"
_TAG_EVENT: Final = b"EVNT"
_TAG_ATTEMPT: Final = b"ATMP"
_TAG_STEP: Final = b"WFST"
_TAG_FOOTER: Final = b"FOOT"


def _build_id() -> int:
    """A stable-ish 64-bit identity of the producing build (wreath/python/platform).

    Recordings from an incompatible build should be recognizable; this is a
    coarse fingerprint, not a security control.
    """
    import platform
    import sys
    from importlib.metadata import PackageNotFoundError, version

    try:
        wreath_version = version("wreath")
    except PackageNotFoundError:
        wreath_version = "0"
    identity = f"{wreath_version}|{sys.version}|{platform.platform()}".encode()
    return zlib.crc32(identity) & 0xFFFFFFFF


def _chunk(tag: bytes, payload: bytes) -> bytes:
    if len(payload) > MAX_CHUNK_BYTES:
        raise SchemaError(f"chunk {tag!r} exceeds {MAX_CHUNK_BYTES} bytes")
    return _CHUNK.pack(tag, len(payload), zlib.crc32(payload) & 0xFFFFFFFF) + payload


# A job attempt is a *record kind inside `WFR1`*, not a second container: one
# decoder, one reader, one set of forensics tooling. What it holds is identity,
# cause, boundaries, and outcome.
# What it deliberately does not hold is the job's **arguments**. `args jsonb` is
# a positional array and `RedactionPolicy` is entirely name-keyed, so
# deny-by-default has no name to deny; only the *count* is recorded, so a reader
# can see that arguments existed without any of them reaching the disk.

_ATTEMPT_MAGIC: Final = b"ATT1"
_ATTEMPT_VERSION: Final = 1
#: This chunk holds only part of one attempt record. Refused, never joined:
#: an attempt assembled from pieces is a recording of less than it claims.
_ATTEMPT_FLAG_CONTINUED: Final = 0x01
#: magic 4s | version u8 | flags u8 | reserved u16 | total_bytes u32
_ATTEMPT_HEADER = struct.Struct("<4sBBHI")
#: job_id i64 | fence i64 | attempt u32 | max_attempts u32
#: | argument_count u32 | boundary_count u32
_ATTEMPT_FIXED = struct.Struct("<qqIIII")
_BOUNDARY_FIXED = struct.Struct("<Bi")  # seam u8 | coordinate i32

#: A recorded error message is clamped to what the queue itself keeps in
#: `last_error`, so a recording cannot hold more of a failure than the row does.
MAX_ERROR_MESSAGE = 2000


class AttemptOutcome(StrEnum):
    """How one execution of one task by one worker ended.

    Four, not three. `deadline_cancelled` is separate from `raised` because
    nothing failed -- `JobRunner._run` cancels the handler at
    `deadline_for(task)` and counts it in `run_timeouts` precisely because the
    cause is usually a slow dependency. Folding it into `raised` would make a
    recording report a defect where there was none.
    """

    COMPLETED = "completed"
    RAISED = "raised"
    DEADLINE_CANCELLED = "deadline_cancelled"
    LEASE_EXPIRED = "lease_expired"


@dataclass(frozen=True, slots=True)
class BoundaryEvent:
    """One boundary crossing during an attempt, at an owned coordinate.

    The coordinate space is the *same* one a `wreath.replay.FaultSchedule` is
    keyed to -- the named target and the Nth operation at that seam -- which is
    what lets a recorded failure be replayed as an injected fault rather than
    re-derived from a payload nobody may keep.

    Attributes:
        seam: A `wreath.replay.AdapterSeam` value, held as an int for the same
            reason `AdapterFaultDescriptor.seam` is: the container must not
            import the replay module to decode a number.
        target: The named database, HTTP client, or object store.
        coordinate: The Nth operation at this seam on this target.
        error_type: The exception class name when the call raised, or `""`
            when it returned. The *message* is not kept: a driver error quotes
            the statement, and a statement quotes its parameters.
    """

    seam: int
    target: str
    coordinate: int
    error_type: str = ""


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One execution of one task by one worker, and the boundaries it crossed.

    Identity is `job_id`/`queue`/`task`/`attempt`/`tenant`/`dedup_key` **and
    `fence`**. The fence is not decoration: after a lease expiry two workers can
    both believe they own a job, and a recording that cannot say which one it
    was is a recording of an ambiguity.

    `trace_context` is the `traceparent` of the request that enqueued the job,
    which the queue already stores -- so an attempt recording joins to the
    request that produced its arguments at no extra cost.

    `argument_count` is how many arguments the job carried. The arguments
    themselves are never recorded; see the module comment above.
    """

    job_id: int
    queue: str
    task: str
    attempt: int
    max_attempts: int
    tenant: str
    dedup_key: str
    fence: int
    trace_context: str
    boundaries: tuple[BoundaryEvent, ...]
    outcome: str
    error_type: str = ""
    error_message: str = ""
    argument_count: int = 0
    #: `(parameter_name, json_text)` for the arguments an operator allowed by
    #: name, where the JSON is exactly one of `{"value": ...}` or
    #: `{"withheld": "<reason>"}`. Empty unless `AttemptPolicy` names one, which
    #: is the default; see `wreath.recording.AttemptPolicy`.
    arguments: tuple[tuple[str, str], ...] = ()

    def encode(self) -> bytes:
        return _core.attempt_encode(
            self.job_id,
            self.fence,
            self.attempt,
            self.max_attempts,
            self.argument_count,
            self.boundaries,
            (
                self.queue,
                self.task,
                self.tenant,
                self.dedup_key,
                self.trace_context,
                str(self.outcome),
                self.error_type,
                self.error_message[:MAX_ERROR_MESSAGE],
            ),
            self.arguments,
        )

    @classmethod
    def decode(cls, payload: bytes) -> AttemptRecord:
        """Parse one attempt record, refusing anything partial *by name*.

        A recording that decodes to a prefix of itself is the failure this
        refuses: every boundary after the tear is missing and nothing in the
        bytes says how many there were.
        """
        return _core.attempt_decode(payload, SchemaError, BoundaryEvent, cls)


# A **second record kind inside `WFR1`, beside `ATMP`** -- one container, one
# decoder, one set of forensics tooling. It is a distinct kind rather than an
# `AttemptRecord` with different field names because the unit genuinely differs
# in all three of the things a recording is for:
# * **identity** is `(workflow instance, step)`, not `(job, attempt)`. There is
#   no fence, because nothing claims a step under a lease; there is a
#   *position*, because a saga is ordered and "step 4" is meaningless without it.
# * **cause** is the step before it, not the request that enqueued it. A saga
#   step's inputs are the previous step's outputs, so `after` is the join a
#   reader actually follows.
# * **compensations have already run, or have not.** That is the fact a
#   mid-saga failure is impossible to reconstruct without, and the reason the
#   roadmap called this the highest-value unbuilt record kind: the money left
#   the account, the courier was not booked, and whether the refund ran is the
#   whole question.
# What it does *not* hold, on the same argument the attempt record gives: the
# step's arguments and its return value. A step's result is threaded through
# `StepContext.results` and is arbitrary application data; a recording that kept
# it would be a copy of the saga's payload on disk with no name-keyed policy
# able to deny any of it.

_STEP_MAGIC: Final = b"WFS1"
_STEP_VERSION: Final = 1
#: This chunk holds only part of one step record. Refused, never joined.
_STEP_FLAG_CONTINUED: Final = 0x01
#: magic 4s | version u8 | flags u8 | reserved u16 | total_bytes u32
_STEP_HEADER = struct.Struct("<4sBBHI")
#: position i32 | boundary_count u32 | compensation_count u32 | completed_before u32
_STEP_FIXED = struct.Struct("<iIII")


class WorkflowStepOutcome(StrEnum):
    """How one execution of one step of one workflow instance ended."""

    COMPLETED = "completed"
    RAISED = "raised"
    COMPENSATION_FAILED = "compensation_failed"


#: How one earlier step's undo went, as recorded on the failing step's record.
#: `none` is a step that declared no compensation at all, which is a different
#: fact from an undo that was never reached.
COMPENSATION_RAN: Final = "ran"
COMPENSATION_FAILED: Final = "failed"
COMPENSATION_NONE: Final = "none"


@dataclass(frozen=True, slots=True)
class WorkflowStepRecord:
    """One step of one workflow instance, and what the saga had already undone.

    Attributes:
        instance: the workflow instance key -- the durable identity `resume`
            takes, so a recording joins to the row it describes.
        workflow: the workflow's name.
        step: this step's name, which is what the store keys completion on.
        position: this step's index in declaration order. Declaration order *is*
            execution order and the undo chain is its reverse, so the number is
            what makes "mid-way" a place rather than an adjective.
        after: the step that ran before this one, or `""` for the first. The
            cause, in the sense the request's `traceparent` is a job attempt's.
        completed_before: how many steps were recorded complete when this one
            started. On a resumed instance that is work this process never did.
        compensations: `(step, outcome)` newest-first, where outcome is
            `ran`, `failed` or `none`. Empty on a step that completed.
    """

    instance: str
    workflow: str
    step: str
    position: int
    after: str
    tenant: str
    trace_context: str
    boundaries: tuple[BoundaryEvent, ...]
    outcome: str
    error_type: str = ""
    error_message: str = ""
    completed_before: int = 0
    compensations: tuple[tuple[str, str], ...] = ()

    def encode(self) -> bytes:
        return _core.step_encode(
            self.position,
            self.boundaries,
            self.completed_before,
            self.compensations,
            self.instance,
            self.workflow,
            self.step,
            self.after,
            self.tenant,
            self.trace_context,
            str(self.outcome),
            self.error_type,
            self.error_message[:MAX_ERROR_MESSAGE],
        )

    @classmethod
    def decode(cls, payload: bytes) -> WorkflowStepRecord:
        """Parse one step record, refusing anything partial by name.

        Stricter than a capture slab and for the same reason `AttemptRecord` is:
        a torn step record does not leave a smaller step, it leaves one whose
        compensation list is missing entries nobody can enumerate -- and *those*
        are the entries the record exists for.
        """
        return _core.step_decode(payload, SchemaError, BoundaryEvent, cls)


def read_step_recording(data: bytes) -> WorkflowStepRecord:
    """The one workflow-step recording in a `WFR1` file, or a refusal saying why not.

    `read_attempt_recording`'s twin, in the same order for the same reason: the
    tear is diagnosed first, because a cut landing inside the `WFST` chunk
    leaves a file with no step in it and "this file holds no step recording"
    would send a reader looking for another file rather than at the truncation.
    """
    return _read_one_recording(
        data,
        field="steps",
        truncated=(
            "workflow-step recording is truncated: the file has no footer, so the "
            "process died while writing it and the compensations it had not "
            "written yet cannot be counted"
        ),
        missing="this recording holds no workflow-step recording",
        multiple=(
            "this recording holds {count} workflow-step recordings; one file is "
            "one step, because step 3 of an instance is a different execution from "
            "step 4 and a reader can only characterise one"
        ),
    )


def _text(value: str) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) > 0xFFFFFFFF:  # pragma: no cover - a 4 GiB field is not reachable
        raise SchemaError("attempt recording field is too long to encode")
    return struct.pack("<I", len(raw)) + raw


def _read_text(payload: bytes, offset: int) -> tuple[str, int]:
    if offset + 4 > len(payload):
        raise SchemaError("attempt recording is truncated inside a text field")
    (length,) = struct.unpack_from("<I", payload, offset)
    offset += 4
    end = offset + length
    if end > len(payload):
        raise SchemaError(
            f"attempt recording is truncated: a field declares {length} bytes and "
            f"only {len(payload) - offset} remain"
        )
    return payload[offset:end].decode("utf-8"), end


def read_attempt_recording(data: bytes) -> AttemptRecord:
    """The one attempt recording in a `WFR1` file, or a refusal naming why not.

    Stricter than `read_recording` on purpose. A capture slab stream recovers a
    torn tail because every complete slab before the tear is forensic material
    worth keeping; an attempt is *one* record, so a tear does not leave a
    smaller attempt, it leaves an attempt that is missing the part nobody can
    enumerate. A file with no footer, more than one attempt, or none at all is
    refused by name rather than reported as the thing it nearly is.
    """
    return _read_one_recording(
        data,
        field="attempts",
        truncated=(
            "attempt recording is truncated: the file has no footer, so the process "
            "died while writing it and the boundaries it had not written yet cannot "
            "be counted"
        ),
        missing=(
            "this recording holds no attempt recording; `wreath replay to-test` "
            "reads a job attempt from an `ATMP` record and this file has none"
        ),
        multiple=(
            "this recording holds {count} attempt recordings; one file is one "
            "attempt, because attempt 4 of a job is a different execution from "
            "attempt 3 and a test can only characterise one"
        ),
    )


def _read_one_recording(
    data: bytes,
    *,
    field: str,
    truncated: str,
    missing: str,
    multiple: str,
) -> Any:
    """Read one strict record after the shared footer/cardinality checks."""
    decoded = read_recording(data)
    # First by design: a tear inside the record also makes its collection empty,
    # and "none" would diagnose the wrong file rather than the partial write.
    if not decoded.clean:
        raise SchemaError(truncated)
    records = getattr(decoded, field)
    if not records:
        raise SchemaError(missing)
    if len(records) > 1:
        raise SchemaError(multiple.format(count=len(records)))
    return records[0]


class WFR1Writer:
    """Streams a `WFR1` recording to a binary file object.

    Writes the header and metadata chunk on construction, appends capture/event
    chunks as they arrive, and writes the footer on `close`. It performs no
    buffering policy of its own beyond the file object's; the caller (the sink)
    decides how often to flush and owns the file's permissions and lifetime.
    """

    __slots__ = (
        "_file",
        "_chunk_count",
        "_capture_slabs",
        "_event_cells",
        "_attempts",
        "_steps",
        "_closed",
    )

    def __init__(self, file: Any, image: MetadataImage) -> None:
        self._file = file
        self._chunk_count = 0
        self._capture_slabs = 0
        self._event_cells = 0
        self._attempts = 0
        self._steps = 0
        self._closed = False
        now_unix = time.time_ns()
        header = _HEADER.pack(
            MAGIC,
            _CONTAINER_VERSION,
            SCHEMA_VERSION,
            0,
            image.image_hash_short(),
            uuid.uuid4().bytes,
            now_unix,
            time.monotonic_ns(),
            now_unix,
            _build_id() & 0xFFFFFFFFFFFFFFFF,
            0,
        )
        self._file.write(header)
        self._write_chunk(_TAG_META, image.canonical_bytes())

    def _write_chunk(self, tag: bytes, payload: bytes) -> None:
        self._file.write(_chunk(tag, payload))
        self._chunk_count += 1

    def write_captures(self, slabs: list[bytes]) -> int:
        """Append one `CAPT` chunk holding the given slabs (already serialized by
        the native capture core, self-delimited by each slab header's used_bytes).
        Returns the number of slabs written."""
        if not slabs:
            return 0
        self._write_chunk(_TAG_CAPTURE, b"".join(slabs))
        self._capture_slabs += len(slabs)
        return len(slabs)

    def write_events(self, cells: bytes) -> int:
        """Append one `EVNT` chunk of fixed 64-byte completion cells."""
        if not cells:
            return 0
        if len(cells) % CELL_SIZE != 0:
            raise SchemaError("event bytes are not a whole number of cells")
        self._write_chunk(_TAG_EVENT, cells)
        self._event_cells += len(cells) // CELL_SIZE
        return len(cells) // CELL_SIZE

    def write_attempt(self, record: AttemptRecord) -> None:
        """Append one `ATMP` chunk holding a whole job-attempt record."""
        self._write_chunk(_TAG_ATTEMPT, record.encode())
        self._attempts += 1

    def write_step(self, record: WorkflowStepRecord) -> None:
        """Append one `WFST` chunk holding a whole workflow-step record."""
        self._write_chunk(_TAG_STEP, record.encode())
        self._steps += 1

    def close(self) -> None:
        """Write the footer (proving a clean close) and flush. Idempotent."""
        if self._closed:
            return
        self._closed = True
        footer = (
            _FOOTER.pack(self._chunk_count + 1, self._capture_slabs, self._event_cells)
            + _FOOTER_ATTEMPTS.pack(self._attempts)
            + _FOOTER_STEPS.pack(self._steps)
        )
        self._file.write(_chunk(_TAG_FOOTER, footer))
        flush = getattr(self._file, "flush", None)
        if callable(flush):
            flush()


@dataclass(frozen=True, slots=True)
class DecodedRecording:
    """A parsed `WFR1` recording. `clean` is True only when a valid footer was
    reached; a torn-off tail yields every complete chunk read so far with
    `clean=False`."""

    image: MetadataImage
    slabs: tuple[CaptureSlab, ...]
    events: tuple[bytes, ...]  # raw 64-byte cells, schema-version checked
    recording_uuid: bytes
    build_id: int
    created_unix_nano: int
    clock_mono_ns: int
    clock_unix_ns: int
    clean: bool
    footer_capture_slabs: int = 0
    footer_event_cells: int = 0
    #: Job-attempt records, in the order they were written.
    attempts: tuple[AttemptRecord, ...] = ()
    footer_attempts: int = 0
    #: Workflow-step records, in the order they were written.
    steps: tuple[WorkflowStepRecord, ...] = ()
    footer_steps: int = 0


def read_recording(data: bytes) -> DecodedRecording:
    """Parse a `WFR1` recording, recovering complete chunks from a torn tail.

    Rejects an unknown container/schema major version and a metadata-hash mismatch
    (a reader must not guess at an incompatible image). A chunk whose header or
    payload is truncated, or whose CRC fails, ends parsing: everything before it is
    returned with `clean=False`.
    """
    if len(data) < _HEADER.size:
        raise SchemaError("recording is shorter than its header")
    (
        magic,
        container_ver,
        schema_ver,
        _flags,
        image_hash,
        recording_uuid,
        created_unix,
        clock_mono,
        clock_unix,
        build_id,
        _reserved,
    ) = _HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise SchemaError(f"bad container magic {magic!r}")
    if container_ver != _CONTAINER_VERSION:
        raise SchemaError(f"unsupported container version {container_ver}")
    if schema_ver != SCHEMA_VERSION:
        raise SchemaError(f"unsupported schema version {schema_ver}")

    offset = _HEADER.size
    image: MetadataImage | None = None
    slabs: list[CaptureSlab] = []
    events: list[bytes] = []
    attempts: list[AttemptRecord] = []
    steps: list[WorkflowStepRecord] = []
    clean = False
    footer_captures = 0
    footer_events = 0
    footer_attempts = 0
    footer_steps = 0

    while offset + _CHUNK.size <= len(data):
        tag, length, crc = _CHUNK.unpack_from(data, offset)
        start = offset + _CHUNK.size
        end = start + length
        if length > MAX_CHUNK_BYTES or end > len(data):
            break  # truncated tail: recover what came before
        payload = data[start:end]
        if zlib.crc32(payload) & 0xFFFFFFFF != crc:
            break  # torn / corrupt chunk: stop, keep the clean prefix
        offset = end
        if tag == _TAG_META:
            image = decode_metadata_image(payload)
            if image.image_hash_short() != image_hash:
                raise SchemaError("recording metadata hash does not match its header")
        elif tag == _TAG_CAPTURE:
            _split_slabs(payload, slabs)
        elif tag == _TAG_EVENT:
            decoded_events = _core.recording_event_cells(
                payload, CELL_SIZE, SCHEMA_VERSION, SchemaError
            )
            if decoded_events is None:
                break
            events.extend(decoded_events)
        elif tag == _TAG_ATTEMPT:
            # Refused rather than skipped: an `ATMP` chunk this reader cannot
            # decode is the whole recording, not one slab out of many.
            attempts.append(AttemptRecord.decode(payload))
        elif tag == _TAG_STEP:
            # Refused rather than skipped, exactly as `ATMP` is: a `WFST` chunk
            # this reader cannot decode is the whole recording.
            steps.append(WorkflowStepRecord.decode(payload))
        elif tag == _TAG_FOOTER:
            if len(payload) >= _FOOTER.size:
                _count, footer_captures, footer_events = _FOOTER.unpack_from(payload, 0)
            if len(payload) >= _FOOTER.size + _FOOTER_ATTEMPTS.size:
                (footer_attempts,) = _FOOTER_ATTEMPTS.unpack_from(payload, _FOOTER.size)
            if len(payload) >= _FOOTER.size + _FOOTER_ATTEMPTS.size + _FOOTER_STEPS.size:
                (footer_steps,) = _FOOTER_STEPS.unpack_from(
                    payload, _FOOTER.size + _FOOTER_ATTEMPTS.size
                )
            clean = True
            break
        # Unknown tags are skipped (forward compatibility within a major version).

    if image is None:
        raise SchemaError("recording is missing its metadata chunk")
    return DecodedRecording(
        image=image,
        slabs=tuple(slabs),
        events=tuple(events),
        recording_uuid=bytes(recording_uuid),
        build_id=build_id,
        created_unix_nano=created_unix,
        clock_mono_ns=clock_mono,
        clock_unix_ns=clock_unix,
        clean=clean,
        footer_capture_slabs=footer_captures,
        footer_event_cells=footer_events,
        attempts=tuple(attempts),
        footer_attempts=footer_attempts,
        steps=tuple(steps),
        footer_steps=footer_steps,
    )


def _split_slabs(payload: bytes, out: list[CaptureSlab]) -> None:
    """Walk a CAPT chunk, splitting self-delimited slabs by their used_bytes."""
    offset = 0
    n = len(payload)
    while offset + CAPTURE_SLAB_HEADER_SIZE <= n:
        used = int.from_bytes(payload[offset + 8 : offset + 12], "little")
        if used < CAPTURE_SLAB_HEADER_SIZE or offset + used > n:
            break  # a torn slab inside an otherwise-valid chunk: stop here
        out.append(CaptureSlab.decode(payload[offset : offset + used]))
        offset += used


class RecordingSink:
    """Drains committed capture slabs to a `WFR1` file on a background thread.

    It is the sole consumer of the recorder's capture commit ring. A disk-full or
    write error is caught, counted, and degrades the sink to *drain-and-drop* so
    the bounded slab pool keeps flowing; the request path never sees it.
    """

    __slots__ = (
        "_recorder",
        "_image",
        "_path",
        "_interval",
        "_max_slabs",
        "_lock",
        "_thread",
        "_stop",
        "_writer",
        "_fh",
        "_written",
        "_dropped",
        "_write_errors",
        "_degraded",
    )

    def __init__(
        self,
        recorder: Any,
        image: MetadataImage,
        path: str,
        *,
        interval: float = 0.1,
        max_slabs_per_drain: int = 256,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._recorder = recorder
        self._image = image
        self._path = path
        self._interval = interval
        self._max_slabs = max_slabs_per_drain
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._writer: WFR1Writer | None = None
        self._fh: Any = None
        self._written = 0
        self._dropped = 0
        self._write_errors = 0
        self._degraded = False

    @property
    def path(self) -> str:
        return self._path

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # Open owner-only (0600), truncating. Failure to open leaves the sink
        # degraded from the start -- it still drains and drops, never raising.
        try:
            fd = os.open(
                self._path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            self._fh = os.fdopen(fd, "wb")
            self._writer = WFR1Writer(self._fh, self._image)
        except OSError:
            self._degraded = True
            self._close_file()
        self._stop.clear()
        thread = threading.Thread(target=self._run, name="wreath-flight-recording", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            self._thread = None
        self._drain_once()  # final flush of anything committed after the last tick
        if self._writer is not None and not self._degraded:
            try:
                self._writer.close()
            except OSError:
                self._note_error()
        self._close_file()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._drain_once()
            self._stop.wait(self._interval)

    def _drain_once(self) -> None:
        # Always drain -- even when degraded -- so slabs return to the pool and the
        # bounded capture pool never backs up into request-path loss.
        slabs = self._recorder.drain_captures(self._max_slabs)
        if not slabs:
            return
        if self._degraded or self._writer is None:
            with self._lock:
                self._dropped += len(slabs)
            return
        try:
            self._writer.write_captures(slabs)
            self._fh.flush()
        except OSError:
            self._note_error()
            with self._lock:
                self._dropped += len(slabs)
            self._degraded = True  # stop writing; keep draining/dropping
            self._close_file()
        else:
            with self._lock:
                self._written += len(slabs)

    def archive_cells(self, cells: bytes) -> None:
        """Append drained ring cells to the recording as an `EVNT` chunk.

        The archival half of crash forensics. The ring file holds what was still
        in flight when a process died; this holds everything before it, because
        a ring keeps `ring_records` cells and then refuses. Wired to the
        projector's drain, which is where cells leave the ring.

        Called from the projector thread rather than this sink's own, so it
        takes the same lock the stats do and follows the same rule as
        `_drain_once`: a write error degrades to drop-and-count and never
        propagates. The cells have already happened; failing to file them must
        not stall the drain that feeds trace assembly and every exporter.
        """
        if not cells:
            return
        with self._lock:
            if self._degraded or self._writer is None:
                self._dropped += len(cells) // CELL_SIZE
                return
            try:
                self._writer.write_events(cells)
                self._fh.flush()
            except OSError, ValueError:
                # ValueError: a partial cell, which means the drain handed over
                # something that is not a whole number of cells -- a bug worth
                # degrading on rather than writing a chunk no reader can split.
                self._write_errors += 1
                self._dropped += len(cells) // CELL_SIZE
                self._degraded = True
                self._close_file()
            else:
                self._written += len(cells) // CELL_SIZE

    def _note_error(self) -> None:
        with self._lock:
            self._write_errors += 1

    def _close_file(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    @property
    def stats(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "written": self._written,
                "dropped": self._dropped,
                "write_errors": self._write_errors,
                "degraded": self._degraded,
            }
