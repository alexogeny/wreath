"""Reference codec for the Native Flight Recorder container format.

This is the readable schema/overflow oracle for NFR, not a performance path: the
native spine writes cells; this module encodes/decodes the metadata image
container and validates the fixed cell records so tests have an independent,
byte-exact reference to check the recorder against. It never records runtime
telemetry.

The container is a small chunked binary format (a Stage-0 subset of the `WFR1`
recording container described in the plan): a fixed header, a metadata chunk, and
an optional event chunk of fixed cells, each length-prefixed and checksummed so a
truncated or corrupted stream is rejected rather than guessed.
"""

from __future__ import annotations

import struct
import time
import zlib
from dataclasses import dataclass

from ._flight_schema import (
    BYTE_ORDER,
    CELL_SIZE,
    IMAGE_HASH_BYTES,
    MAX_CHUNK_BYTES,
    SCHEMA_VERSION,
    CompletionCell,
    MetadataImage,
    SchemaError,
    decode_metadata_image,
    siphash24,
)

_U64 = 0xFFFFFFFFFFFFFFFF

#: Container magic. The trailing digit is the container (not schema) version.
MAGIC = b"WFR0"


_HEADER = struct.Struct(BYTE_ORDER + "4sBBH")  # magic, container_ver, schema_ver, flags
_CHUNK = struct.Struct(BYTE_ORDER + "4sII")  # tag, byte_length, crc32

_CONTAINER_VERSION = 1
_FLAG_HAS_EVENTS = 1 << 0


@dataclass(frozen=True, slots=True)
class Recording:
    """A decoded container: its metadata image and any fixed event cells."""

    image: MetadataImage
    events: tuple[bytes, ...]  # raw 64-byte cells, schema-validated on decode


def encode_recording(image: MetadataImage, events: tuple[bytes, ...] = ()) -> bytes:
    """Serialize a metadata image (and optional fixed cells) into a container."""
    for cell in events:
        if len(cell) != CELL_SIZE:
            raise SchemaError(f"event cell must be {CELL_SIZE} bytes, got {len(cell)}")
    flags = _FLAG_HAS_EVENTS if events else 0
    out = bytearray()
    out += _HEADER.pack(MAGIC, _CONTAINER_VERSION, SCHEMA_VERSION, flags)
    out += image.image_hash_short()
    out += _chunk(b"META", image.canonical_bytes())
    if events:
        out += _chunk(b"EVNT", b"".join(events))
    return bytes(out)


def decode_recording(data: bytes) -> Recording:
    """Parse a container, rejecting truncation, corruption, and bad versions."""
    if len(data) < _HEADER.size + IMAGE_HASH_BYTES:
        raise SchemaError("recording is shorter than its header")
    magic, container_ver, schema_ver, flags = _HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise SchemaError(f"bad container magic {magic!r}")
    if container_ver != _CONTAINER_VERSION:
        raise SchemaError(f"unsupported container version {container_ver}")
    if schema_ver != SCHEMA_VERSION:
        raise SchemaError(f"unsupported schema version {schema_ver}")
    offset = _HEADER.size
    declared_hash = data[offset : offset + IMAGE_HASH_BYTES]
    offset += IMAGE_HASH_BYTES

    meta_bytes, offset = _read_chunk(data, offset, b"META")
    image = decode_metadata_image(meta_bytes)
    if image.image_hash_short() != declared_hash:
        raise SchemaError("metadata image hash does not match the recorded image")

    events: tuple[bytes, ...] = ()
    if flags & _FLAG_HAS_EVENTS:
        event_bytes, offset = _read_chunk(data, offset, b"EVNT")
        if len(event_bytes) % CELL_SIZE != 0:
            raise SchemaError("event chunk is not a whole number of cells")
        cells = tuple(
            bytes(event_bytes[i : i + CELL_SIZE])
            for i in range(0, len(event_bytes), CELL_SIZE)
        )
        for cell in cells:
            # Validate each fixed record against the schema (kind/version).
            if cell[0] != SCHEMA_VERSION:
                raise SchemaError("event cell has an unsupported schema version")
        events = cells
    if offset != len(data):
        # Refuse rather than return the first container and drop the rest. Two
        # recordings concatenated, or a file appended to after a short write,
        # both land here -- and decoding the first while silently discarding
        # what follows is the failure `read_recording` avoids by reporting a
        # torn tail as `clean=False` instead of hiding it.
        raise SchemaError(
            f"recording has {len(data) - offset} trailing byte(s) after its last "
            f"chunk; a container holds exactly one recording"
        )
    return Recording(image=image, events=events)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    if len(payload) > MAX_CHUNK_BYTES:
        raise SchemaError(f"chunk {tag!r} exceeds {MAX_CHUNK_BYTES} bytes")
    return _CHUNK.pack(tag, len(payload), zlib.crc32(payload) & 0xFFFFFFFF) + payload


def _read_chunk(data: bytes, offset: int, expected_tag: bytes) -> tuple[bytes, int]:
    if offset + _CHUNK.size > len(data):
        raise SchemaError("truncated chunk header")
    tag, length, crc = _CHUNK.unpack_from(data, offset)
    if tag != expected_tag:
        raise SchemaError(f"expected chunk {expected_tag!r}, found {tag!r}")
    if length > MAX_CHUNK_BYTES:
        raise SchemaError(f"chunk {tag!r} declares {length} bytes, over the limit")
    start = offset + _CHUNK.size
    end = start + length
    if end > len(data):
        raise SchemaError(f"chunk {tag!r} truncated: need {length} bytes")
    payload = data[start:end]
    if zlib.crc32(payload) & 0xFFFFFFFF != crc:
        raise SchemaError(f"chunk {tag!r} failed its CRC32 check")
    return payload, end


def decode_completion(cell: bytes) -> CompletionCell:
    """Convenience: decode one completion cell (schema-validated)."""
    return CompletionCell.decode(cell)


_HEXCHARS = frozenset(b"0123456789abcdef")


def parse_traceparent(data: bytes) -> tuple[int, int, int, bool] | None:
    """Strict W3C `traceparent` parse, independent of the C parser.

    Returns `(trace_hi, trace_lo, parent_span, sampled)` or `None` for any
    malformed value. Lowercase hex only; rejects an `ff` version and all-zero
    trace/parent ids; never raises on bad input.
    """
    data = bytes(data)
    if len(data) != 55 or data[2] != 0x2D or data[35] != 0x2D or data[52] != 0x2D:
        return None

    def hexval(segment: bytes) -> int | None:
        if not segment or any(b not in _HEXCHARS for b in segment):
            return None
        return int(segment, 16)

    version = hexval(data[0:2])
    if version is None or version == 0xFF:
        return None
    hi = hexval(data[3:19])
    lo = hexval(data[19:35])
    span = hexval(data[36:52])
    flags = hexval(data[53:55])
    if hi is None or lo is None or span is None or flags is None:
        return None
    if (hi == 0 and lo == 0) or span == 0:
        return None
    return (hi, lo, span, bool(flags & 1))


# --- pure recorder oracle ---------------------------------------------------
#
# A readable, bounded model of the native worker's *observable* behavior: the
# ring, counters, loss accounting, active table, and completion cells. It is not
# a performance path; the differential tests drive the same sequence through this
# and the native Recorder and assert identical drained cells and counters.

from ._flight_schema import (  # noqa: E402 - grouped with the oracle it serves
    _CAPTURE_FIELD_HEADER,
    _CAPTURE_SLAB_HEADER,
    CAPTURE_FIELD_ALIGN,
    CAPTURE_FIELD_HEADER_SIZE,
    CAPTURE_HASH_BYTES,
    CAPTURE_SLAB_HEADER_SIZE,
    FLAG_BODY_TRUNCATED,
    FLAG_DETAILED_ARMED,
    FLAG_ERROR_PROMOTED,
    FLAG_FORENSIC_ARMED,
    FLAG_SLOW_PROMOTED,
    HISTOGRAM_BUCKETS,
    PHASE_CELL_BUDGET,
    PHASE_RECORDS_PER_BATCH,
    CaptureDisposition,
    EventKind,
    LossReason,
    Mode,
    PhaseBatchCell,
    PhaseCoverage,
    PhaseKind,
    PhaseRecord,
    Protocol,
    TerminalStatus,
    _pad4,
    histogram_bucket,
)


def _mix64(x: int) -> int:
    """Stateless splitmix64 finalizer; mirrors mix64() in flight.c bit-for-bit."""
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _U64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _U64
    return x ^ (x >> 31)




class _PureRequest:
    __slots__ = ("_worker", "request_id", "active_slot", "_ctx", "_finished")

    def __init__(self, worker: ReferenceRecorder, request_id: int, slot: int, ctx: dict) -> None:
        self._worker = worker
        self.request_id = request_id
        self.active_slot = slot
        self._ctx = ctx
        self._finished = False

    def route(self, route_id: int, plan_id: int) -> None:
        self._ctx["route_id"] = route_id
        self._ctx["plan_id"] = plan_id

    def phase(self, phase_id: int, dependency_id: int = 0, coverage: int = 0,
              start_offset_us: int = 0, duration_us: int = 0) -> None:
        self._worker._phase(self._ctx, phase_id, dependency_id, coverage,
                            start_offset_us, duration_us)

    @property
    def phase_count(self) -> int:
        return len(self._ctx.get("phases", ()))

    def capture(self, field_class: int, descriptor_id: int = 0,
                disposition: int = 0, data: bytes = b"", max_bytes: int = 0) -> None:
        self._worker._capture(self._ctx, field_class, descriptor_id, disposition,
                              data, max_bytes)

    @property
    def capture_slot(self) -> int:
        return self._ctx.get("capture_slot", -1)

    def finish(self, now_ns: int, status: int = 0, terminal: int = 0,
               error_class: int = 0, bytes_in: int = 0, bytes_out: int = 0) -> None:
        if self._finished:
            raise RuntimeError("request already finished")
        self._worker._end(self._ctx, now_ns, status, terminal, error_class,
                          bytes_in, bytes_out)
        self._finished = True

    def abandon(self) -> None:
        if not self._finished:
            self._worker._abandon(self._ctx)
            self._finished = True


class ReferenceRecorder:
    """A readable model of `wreath._native._flight.Recorder`'s observable behaviour."""

    def __init__(self, mode: int, worker_id: int = 0, ring_records: int = 16384,
                 active_requests: int = 2048, histogram_count: int = 1,
                 completion_summaries: bool = True,
                 detailed_sample_rate: float = 0.0, phase_slots: int = 256,
                 detailed_slow_us: int = 0, capture_slabs: int = 0,
                 slab_bytes: int = 65536,
                 capture_hash_key: tuple[int, int] | None = None) -> None:
        if ring_records and (ring_records & (ring_records - 1)):
            raise ValueError("ring_records must be a power of two")
        if not 0.0 <= detailed_sample_rate <= 1.0:
            raise ValueError("detailed_sample_rate must be in [0, 1]")
        self.mode = int(mode)
        self._worker_id = worker_id
        # Clock calibration captured at creation, mirroring the native worker: the
        # monotonic base the server's now_ns shares, paired with the wall clock.
        self._epoch_mono_ns = time.monotonic_ns()
        self._epoch_unix_ns = time.time_ns()
        self._detailed_sample_threshold = int(detailed_sample_rate * 4294967296.0 + 0.5)
        self._slow_threshold_us = detailed_slow_us
        # Phase scratch pool: only present in a mode that arms phases.
        self._phase_capacity = phase_slots if self.mode >= Mode.DETAILED else 0
        self._phase_free = list(range(self._phase_capacity - 1, -1, -1))
        self._phase_high_water = 0
        # Forensic capture-slab pool: only present in Forensic mode. Mirrors the
        # native free-stack + commit/return rings via three Python lists.
        forensic = self.mode >= Mode.FORENSIC and capture_slabs > 0
        self._capture_capacity = capture_slabs if forensic else 0
        self._slab_bytes = slab_bytes if forensic else 0
        self._capture_free = list(range(self._capture_capacity - 1, -1, -1))
        self._capture_committed: list[bytes] = []  # committed slabs, FIFO
        self._capture_returned: list[int] = []  # sink -> writer return queue
        self._capture_high_water = 0
        k = capture_hash_key or (0, 0)
        self._hash_k0, self._hash_k1 = k[0] & _U64, k[1] & _U64
        self._ring_records = ring_records
        self._completion_summaries = completion_summaries
        self._ring: list[bytes] = []
        self._high_water = 0
        self._requests = 0
        self._completions = 0
        self._losses = [0] * len(LossReason)
        self._histogram = [0] * HISTOGRAM_BUCKETS
        self._next_request_id = 1
        self._active_capacity = active_requests
        self._free = list(range(active_requests - 1, -1, -1))
        #: slot -> (request_id, start_ns, protocol, route_id) for the Inspector
        #: snapshot; route_id stays 0 until stage-4 projection, like native.
        self._active: dict[int, tuple[int, int, int, int]] = {}

    # counters / snapshots ----------------------------------------------------
    @property
    def requests(self) -> int:
        return self._requests

    @property
    def completions(self) -> int:
        return self._completions

    @property
    def clock_calibration(self) -> tuple[int, int]:
        """(epoch_mono_ns, epoch_unix_ns): maps a cell's end_offset_ms to Unix."""
        return (self._epoch_mono_ns, self._epoch_unix_ns)

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def ring_occupancy(self) -> int:
        return len(self._ring)

    @property
    def ring_high_water(self) -> int:
        return self._high_water

    @property
    def phase_capacity(self) -> int:
        return self._phase_capacity

    @property
    def phase_in_use(self) -> int:
        return self._phase_capacity - len(self._phase_free)

    @property
    def phase_high_water(self) -> int:
        return self._phase_high_water

    @property
    def capture_capacity(self) -> int:
        return self._capture_capacity

    @property
    def capture_slab_bytes(self) -> int:
        return self._slab_bytes

    @property
    def capture_in_use(self) -> int:
        return self._capture_capacity - len(self._capture_free)

    @property
    def capture_high_water(self) -> int:
        return self._capture_high_water

    @property
    def capture_committed(self) -> int:
        return len(self._capture_committed)

    def loss(self, reason: int) -> int:
        return self._losses[int(reason)] if 0 <= int(reason) < len(self._losses) else 0

    def histogram(self) -> tuple[int, ...]:
        return tuple(self._histogram)

    def active_snapshot(self) -> list[tuple[int, int, int, int]]:
        """In-flight requests as (request_id, start_ns, protocol, route_id)
        rows, in slot order like the native snapshot."""
        return [row for _slot, row in sorted(self._active.items())]

    # lifecycle ---------------------------------------------------------------
    def _start(self, connection_id: int, protocol: int, start_ns: int) -> dict:
        if self.mode == Mode.OFF:
            return {"mode": Mode.OFF}
        self._requests += 1
        request_id = self._next_request_id
        self._next_request_id += 1
        slot = -1
        if self._free:
            slot = self._free.pop()
            self._active[slot] = (request_id, start_ns, protocol, 0)
        else:
            self._losses[LossReason.ACTIVE_TABLE_FULL] += 1
        # Detailed-mode arming mirrors the native worker exactly (same finalizer,
        # same threshold), so drained cells compare byte-for-byte. Pulse never arms.
        # An armed request reserves a phase-scratch slot, or counts the loss.
        flags = 0
        phase_slot = -1
        if self.mode >= Mode.DETAILED and (
            _mix64(request_id) & 0xFFFFFFFF
        ) < self._detailed_sample_threshold:
            flags |= FLAG_DETAILED_ARMED
            if self.mode >= Mode.FORENSIC:
                flags |= FLAG_FORENSIC_ARMED  # capture is a nested subset of Detailed
            if self._phase_free:
                phase_slot = self._phase_free.pop()
                in_use = self._phase_capacity - len(self._phase_free)
                if in_use > self._phase_high_water:
                    self._phase_high_water = in_use
            else:
                self._losses[LossReason.PHASE_SCRATCH_FULL] += 1
        return {
            "mode": self.mode,
            "request_id": request_id,
            "connection_id": connection_id,
            "protocol": protocol,
            "start_ns": start_ns,
            "route_id": 0,
            "plan_id": 0,
            "slot": slot,
            "flags": flags,
            "phase_slot": phase_slot,
            "phases": [],
            "capture_slot": -1,  # a slab is reserved lazily on the first capture
            "capture_used": 0,
            "capture_fields": [],  # encoded field records, joined at commit
            "capture_flags": 0,
        }

    def _release(self, ctx: dict) -> None:
        slot = ctx.get("slot", -1)
        if slot >= 0 and slot in self._active:
            del self._active[slot]
            if len(self._free) < self._active_capacity:
                self._free.append(slot)
            ctx["slot"] = -1

    def _phase(self, ctx: dict, phase_id: int, dependency_id: int = 0,
               coverage: int = 0, start_offset_us: int = 0, duration_us: int = 0) -> None:
        if ctx.get("phase_slot", -1) < 0:
            return
        phases = ctx["phases"]
        if len(phases) >= PHASE_CELL_BUDGET:
            self._losses[LossReason.PHASE_SCRATCH_FULL] += 1
            return
        kind = PhaseKind(phase_id) if phase_id in _PHASE_KIND_SET else PhaseKind.UNKNOWN
        cov = PhaseCoverage(coverage) if coverage in _COVERAGE_SET else PhaseCoverage.UNKNOWN
        phases.append(PhaseRecord(
            phase_id=kind,
            duration_us=duration_us,
            start_offset_us=start_offset_us,
            dependency_id=dependency_id,
            coverage=cov,
            sequence=len(phases),
        ))

    def _phase_release(self, ctx: dict) -> None:
        slot = ctx.get("phase_slot", -1)
        if slot >= 0:
            if len(self._phase_free) < self._phase_capacity:
                self._phase_free.append(slot)
            ctx["phase_slot"] = -1

    # forensic capture ---------------------------------------------------------
    def _capture_reserve(self, ctx: dict) -> int:
        # Reclaim slabs the sink returned, keeping the free stack writer-owned.
        # `extend` rather than a `pop(0)` loop: popping the front of a list
        # shifts every remaining element, so draining n returns cost O(n^2) for
        # a result that does not depend on the order at all -- the entries are
        # placeholders and only their count is observable, as the note in
        # `drain_captures` explains.
        self._capture_free.extend(self._capture_returned)
        self._capture_returned.clear()
        if not self._capture_free:
            self._losses[LossReason.CAPTURE_POOL_FULL] += 1
            return -1
        slot = self._capture_free.pop()
        in_use = self._capture_capacity - len(self._capture_free)
        if in_use > self._capture_high_water:
            self._capture_high_water = in_use
        ctx["capture_used"] = CAPTURE_SLAB_HEADER_SIZE
        ctx["capture_fields"] = []
        ctx["capture_flags"] = 0
        return slot

    def _capture(self, ctx: dict, field_class: int, descriptor_id: int,
                 disposition: int, data: bytes, max_bytes: int = 0) -> None:
        # Deny-by-default: only a Forensic-armed request captures anything.
        if not (ctx.get("flags", 0) & FLAG_FORENSIC_ARMED) or self._capture_capacity == 0:
            return
        if ctx.get("capture_slot", -1) < 0:
            ctx["capture_slot"] = self._capture_reserve(ctx)
            if ctx["capture_slot"] < 0:
                return
        data = bytes(data)
        original_length = len(data)
        # Redact before retention, mirroring context_capture in flight.c.
        if disposition == CaptureDisposition.RAW:
            stored = min(original_length, 0xFFFF)
            if max_bytes and stored > max_bytes:
                stored = max_bytes  # policy byte cap; original_length preserved
            payload = data[:stored]
        elif disposition == CaptureDisposition.HASHED:
            digest = siphash24(data, self._hash_k0, self._hash_k1)
            payload = digest.to_bytes(CAPTURE_HASH_BYTES, "little")
            stored = CAPTURE_HASH_BYTES
        else:  # MASKED / LENGTH: no bytes retained
            stored = 0
            payload = b""

        used = ctx["capture_used"]
        if used + CAPTURE_FIELD_HEADER_SIZE > self._slab_bytes:
            self._losses[LossReason.CAPTURE_POOL_FULL] += 1
            return
        room = self._slab_bytes - used - CAPTURE_FIELD_HEADER_SIZE
        padded = _pad4(stored)
        if padded > room:
            if disposition == CaptureDisposition.RAW:
                stored = room & ~(CAPTURE_FIELD_ALIGN - 1)
                padded = stored
                payload = payload[:stored]
            else:
                self._losses[LossReason.CAPTURE_POOL_FULL] += 1
                return
        record = _CAPTURE_FIELD_HEADER.pack(
            field_class & 0xFFFF,
            descriptor_id & 0xFFFF,
            disposition & 0xFF,
            0,
            stored & 0xFFFF,
            original_length & 0xFFFFFFFF,
        ) + payload + b"\x00" * (padded - stored)
        ctx["capture_fields"].append(record)
        ctx["capture_used"] = used + CAPTURE_FIELD_HEADER_SIZE + padded
        if disposition == CaptureDisposition.RAW and stored < original_length:
            ctx["capture_flags"] |= FLAG_BODY_TRUNCATED & 0xFF
            ctx["flags"] = ctx.get("flags", 0) | FLAG_BODY_TRUNCATED
            self._losses[LossReason.BODY_TRUNCATED] += 1

    def _capture_release(self, ctx: dict) -> None:
        slot = ctx.get("capture_slot", -1)
        if slot >= 0 and len(self._capture_free) < self._capture_capacity:
            self._capture_free.append(slot)
        ctx["capture_slot"] = -1

    def _capture_finish(self, ctx: dict, published: bool) -> None:
        slot = ctx.get("capture_slot", -1)
        if slot < 0:
            return
        fields = ctx["capture_fields"]
        if published and fields:
            body = b"".join(fields)
            header = _CAPTURE_SLAB_HEADER.pack(
                ctx["request_id"] & _U64,
                (CAPTURE_SLAB_HEADER_SIZE + len(body)) & 0xFFFFFFFF,  # used_bytes
                len(fields) & 0xFFFF,  # field_count
                SCHEMA_VERSION,
                EventKind.CAPTURE,
                self._worker_id & 0xFF,
                ctx["capture_flags"] & 0xFF,
                0,
                0,
            )
            self._capture_committed.append(header + body)
            ctx["capture_slot"] = -1
        else:
            self._capture_release(ctx)

    def _publish(self, cell: bytes) -> bool:
        """Append a 64-byte cell to the ring, or count a RING_FULL loss."""
        if self._ring_records and len(self._ring) >= self._ring_records:
            self._losses[LossReason.RING_FULL] += 1
            return False
        self._ring.append(cell)
        self._high_water = max(self._high_water, len(self._ring))
        return True

    def _commit_phases(self, ctx: dict, published: bool) -> None:
        """Commit an armed request's phase batches (only behind a published
        completion), then return its scratch slot -- mirroring the native worker."""
        if ctx.get("phase_slot", -1) < 0:
            return
        phases = ctx["phases"]
        if published and phases:
            for start in range(0, len(phases), PHASE_RECORDS_PER_BATCH):
                batch = PhaseBatchCell(
                    request_id=ctx["request_id"],
                    records=tuple(phases[start:start + PHASE_RECORDS_PER_BATCH]),
                    worker_id=self._worker_id,
                )
                self._publish(batch.encode())
        self._phase_release(ctx)

    def _abandon(self, ctx: dict) -> None:
        if ctx.get("mode", Mode.OFF) == Mode.OFF:
            return
        self._release(ctx)
        self._phase_release(ctx)
        self._capture_finish(ctx, False)
        ctx["mode"] = Mode.OFF

    def _end(self, ctx: dict, now_ns: int, status: int, terminal: int,
             error_class: int, bytes_in: int, bytes_out: int) -> None:
        if ctx.get("mode", Mode.OFF) == Mode.OFF:
            return
        duration_us = max(now_ns - ctx["start_ns"], 0) // 1000
        # Detailed-mode promotion mirrors the native worker: flag a slow or failed
        # completion. Pulse leaves the flags clear (byte-identical to Stage 2).
        if self.mode >= Mode.DETAILED:
            if terminal in (TerminalStatus.ERROR, TerminalStatus.TIMEOUT):
                ctx["flags"] = ctx.get("flags", 0) | FLAG_ERROR_PROMOTED
            if self._slow_threshold_us and duration_us >= self._slow_threshold_us:
                ctx["flags"] = ctx.get("flags", 0) | FLAG_SLOW_PROMOTED
        self._completions += 1
        self._histogram[histogram_bucket(duration_us)] += 1
        self._release(ctx)
        if not self._completion_summaries:
            self._phase_release(ctx)  # no completion to anchor phases/slab to
            self._capture_finish(ctx, False)
            return
        cell = CompletionCell(
            request_id=ctx["request_id"],
            connection_id=ctx["connection_id"],
            route_id=ctx["route_id"],
            plan_id=ctx["plan_id"],
            duration_us=duration_us,
            status=status,
            bytes_in=bytes_in,
            bytes_out=bytes_out,
            protocol=Protocol(ctx["protocol"]) if ctx["protocol"] in _PROTO else Protocol.UNKNOWN,
            terminal=TerminalStatus(terminal) if terminal in _TERM else TerminalStatus.OK,
            error_class=error_class,
            worker_id=self._worker_id,
            flags=ctx.get("flags", 0),
            end_offset_ms=min(max(now_ns - self._epoch_mono_ns, 0) // 1_000_000, 0xFFFFFFFF),
        ).encode()
        published = self._publish(cell)
        self._commit_phases(ctx, published)
        self._capture_finish(ctx, published)

    # public API mirroring the native Recorder --------------------------------
    def begin(self, connection_id: int = 0, protocol: int = 0, start_ns: int = 0) -> _PureRequest:
        ctx = self._start(connection_id, protocol, start_ns)
        return _PureRequest(self, ctx.get("request_id", 0), ctx.get("slot", -1), ctx)

    def record(self, start_ns: int, end_ns: int, connection_id: int = 0, protocol: int = 0,
               route_id: int = 0, plan_id: int = 0, status: int = 0, terminal: int = 0,
               error_class: int = 0, bytes_in: int = 0, bytes_out: int = 0) -> None:
        ctx = self._start(connection_id, protocol, start_ns)
        ctx["route_id"] = route_id
        ctx["plan_id"] = plan_id
        self._end(ctx, end_ns, status, terminal, error_class, bytes_in, bytes_out)

    def drain(self, max_cells: int = 4096) -> bytes:
        if max_cells <= 0:
            return b""
        taken = self._ring[:max_cells]
        self._ring = self._ring[max_cells:]
        return b"".join(taken)

    def drain_captures(self, max_slabs: int = 256) -> list[bytes]:
        """Pop committed capture slabs (bytes), returning each to the pool.

        The sink/test side of `Recorder.drain_captures`."""
        if self._capture_capacity == 0 or max_slabs <= 0:
            return []
        taken = self._capture_committed[:max_slabs]
        self._capture_committed = self._capture_committed[max_slabs:]
        # The sink hands each slab back to the writer via the return queue; the
        # writer reclaims it on its next reserve. A slab header carries the
        # request id (stamped at reserve), never the slot index, so only the
        # *count* of returned slots is observable -- track that, not identities.
        self._capture_returned.extend(0 for _ in taken)
        return taken


_PROTO = frozenset(int(p) for p in Protocol)
_TERM = frozenset(int(t) for t in TerminalStatus)
_PHASE_KIND_SET = frozenset(int(k) for k in PhaseKind)
_COVERAGE_SET = frozenset(int(c) for c in PhaseCoverage)
