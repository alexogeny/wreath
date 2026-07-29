"""Reading a ring file back after the process that wrote it is gone.

The recorder can map its ring from a file instead of the heap (see the ring-file
section of `_flight_schema`), so a process that dies badly leaves its last cells
on disk. This module is the other end of that: given the file, reconstruct the
records that were live when it stopped.

Everything here is written for the case where something has already gone wrong,
which is the whole reason the file exists. So it **reports rather than raises**:

- a slot outside the live window is a lap the ring overwrote, and is skipped;
- a writer that outran the reader overwrote records that were never drained, and
  the count of them is reported rather than left to be inferred from a gap;
- a cell that will not decode is counted, because one torn slot must not cost
  the other 8,191.

The header is the exception. Magic, container version, schema version and cell
size are refusals, because every one of them is a way to read the following
bytes as something they are not -- and misreading a crash file is worse than
failing to read it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Final

from ._flight_schema import (
    CELL_SIZE,
    RING_FILE_HEADER_BYTES,
    CompletionCell,
    CorrelationCell,
    EventKind,
    LogCell,
    LossReason,
    PhaseBatchCell,
    RingFileHeader,
    SchemaError,
)

__all__ = [
    "DecodedRing",
    "RingRecord",
    "read_ring_file",
    "decode_cell",
    "in_flight_requests",
]

#: Kind byte -> the type that decodes it. CONTROL and CAPTURE carry no cell a
#: reader of the completion ring can use: capture slabs never travel this ring,
#: and a control event is bookkeeping rather than a record.
_DECODERS: Final[dict[int, Any]] = {
    int(EventKind.COMPLETION): CompletionCell,
    int(EventKind.CORRELATION): CorrelationCell,
    int(EventKind.PHASE): PhaseBatchCell,
    int(EventKind.LOG): LogCell,
}


def decode_cell(data: bytes) -> Any:
    """Decode one 64-byte cell by its kind byte, or None if nothing reads it.

    The projector routes by kind itself rather than calling this, because it
    hands each kind to a different assembler -- sharing would move the switch
    rather than remove it. This exists for readers that only want the record.

    Raises:
        SchemaError: If the kind is known but the bytes do not decode as it.
    """
    if len(data) < CELL_SIZE:
        raise SchemaError(f"a cell is {CELL_SIZE} bytes, got {len(data)}")
    decoder = _DECODERS.get(data[1])
    return None if decoder is None else decoder.decode(data)


@dataclass(frozen=True, slots=True)
class RingRecord:
    """One cell recovered from a ring file, with where it sat on the ring.

    `sequence` is the ring's own publish counter, not an index into the file:
    two records a lap apart occupy one slot, and the sequence is what says which
    came first.
    """

    sequence: int
    kind: int
    raw: bytes

    def decode(self) -> Any:
        """The typed record, or None for a kind no reader assembles."""
        return decode_cell(self.raw)


@dataclass(frozen=True, slots=True)
class DecodedRing:
    """What a ring file still held, and what it could not tell us.

    Attributes:
        header: Geometry, clock calibration, mirrored loss counters and
            provenance, all read from the file itself.
        records: Live cells in publish order, oldest first.
        undecodable: Slots in the live window whose bytes did not decode. A cell
            half-written at the instant the process died is the expected cause,
            and one of them must not cost the rest of the ring.
        drained: Records the reader had already consumed. They are not lost --
            they went to the projector, and the archival `EVNT` stream in the
            `WFR1` recording is where to look for them.
        cursors_inconsistent: The head/tail pair does not describe a state this
            writer can produce -- tail past head, or a window wider than the
            ring. The ring *refuses* when full rather than overwriting, so the
            window can never exceed its capacity legitimately; a file that says
            otherwise had its header torn mid-update, and the window has been
            clamped to what the geometry allows.
    """

    header: RingFileHeader
    records: tuple[RingRecord, ...]
    undecodable: int = 0
    drained: int = 0
    cursors_inconsistent: bool = False

    @property
    def live(self) -> int:
        """Records recovered from the file."""
        return len(self.records)

    @property
    def ring_full_drops(self) -> int:
        """Records the ring refused because it was full when they were made.

        The number that decides whether this file is the story or a sample of
        it. The ring drops rather than overwrites, so a non-zero count means the
        records nearest the crash may be the ones that are missing.
        """
        return self.header.loss(LossReason.RING_FULL)

    def of_kind(self, kind: EventKind) -> tuple[RingRecord, ...]:
        """Just the records of one kind, still in publish order."""
        return tuple(record for record in self.records if record.kind == int(kind))

    def in_flight(self) -> tuple[int, ...]:
        """Requests that were still running when the process stopped.

        A completion cell is written when a request *finishes*, so a request
        that logged and never completed is one that did not get to finish --
        which, in a crash file, is the request that took the process down.

        Read the gap for what it is. A request that emitted no record at all is
        invisible here (the recorder's active table is not in this file), and a
        request whose completion the ring refused while full looks identical to
        one that never completed -- which is why `ring_full_drops` is worth
        checking before believing this.
        """
        completed = {
            record.decode().request_id
            for record in self.of_kind(EventKind.COMPLETION)
        }
        logged: list[int] = []
        for record in self.of_kind(EventKind.LOG):
            request_id = record.decode().request_id
            if request_id and request_id not in completed and request_id not in logged:
                logged.append(request_id)
        return tuple(logged)

    def logs_for(self, request_id: int) -> tuple[RingRecord, ...]:
        """One request's log records, in the order the ring published them."""
        return tuple(
            record
            for record in self.of_kind(EventKind.LOG)
            if record.decode().request_id == request_id
        )

    def unix_nano(self, offset_ms: int) -> int:
        """Put a cell's monotonic offset on a wall clock.

        A cell carries a millisecond offset from the worker's clock epoch, and
        the epoch pair in the header is what turns that into a time an operator
        can line up against everything else that happened.
        """
        return self.header.epoch_unix_ns + offset_ms * 1_000_000


def in_flight_requests(ring: DecodedRing) -> tuple[int, ...]:
    """Requests that were still running when the process stopped. See
    `DecodedRing.in_flight`; this is the free function form, for a caller that
    has the ring and would rather not reach through it."""
    return ring.in_flight()


def read_ring_file(path: str | os.PathLike[str]) -> DecodedRing:
    """Recover the records a ring file still held.

    Args:
        path: The file the recorder mapped its ring from.

    Returns:
        The live records in publish order, with counts for what was lost.

    Raises:
        SchemaError: If the file is not a ring file this build can read, or is
            shorter than its own header says it is.
        OSError: If the file cannot be read at all.
    """
    with open(path, "rb") as handle:
        blob = handle.read()
    if len(blob) < RING_FILE_HEADER_BYTES:
        raise SchemaError(
            f"a ring file is at least {RING_FILE_HEADER_BYTES} bytes, got {len(blob)}"
        )
    header = RingFileHeader.decode(blob)
    cells = memoryview(blob)[RING_FILE_HEADER_BYTES:]
    expected = header.ring_records * CELL_SIZE
    if len(cells) < expected:
        raise SchemaError(
            f"ring file declares {header.ring_records} records "
            f"({expected} bytes of cells) but holds {len(cells)}"
        )

    # The two cursors are mirrored independently, and a crash can land between
    # them. Neither a tail past the head nor a window wider than the ring is a
    # state this writer produces -- it refuses when full rather than
    # overwriting -- so both mean a header torn mid-update. Clamp to what the
    # geometry allows and say so, rather than inventing a negative window or
    # reading a lap that was never written.
    head, tail = header.head, header.tail
    inconsistent = tail > head or head - tail > header.ring_records
    if tail > head:
        tail = head
    span = min(head - tail, header.ring_records)
    first = head - span

    records: list[RingRecord] = []
    undecodable = 0
    for sequence in range(first, head):
        slot = sequence % header.ring_records
        raw = bytes(cells[slot * CELL_SIZE : (slot + 1) * CELL_SIZE])
        try:
            decoded = decode_cell(raw)
        except SchemaError:
            undecodable += 1
            continue
        if decoded is None:
            # A kind no reader assembles (CONTROL), or a slot never written --
            # an INVALID kind byte of 0 in a ring that has not yet wrapped.
            # Neither is a defect, and neither is a record.
            continue
        records.append(RingRecord(sequence=sequence, kind=raw[1], raw=raw))

    return DecodedRing(
        header=header,
        records=tuple(records),
        undecodable=undecodable,
        drained=tail,
        cursors_inconsistent=inconsistent,
    )
