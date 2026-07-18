"""Pure reference codec for the Native Flight Recorder container format.

This is the readable schema/overflow oracle for NFR, not a performance path: the
future native spine writes cells; this module encodes/decodes the metadata image
container and validates the fixed cell records so tests have an independent,
byte-exact reference. It never records runtime telemetry.

The container is a small chunked binary format (a Stage-0 subset of the ``WFR1``
recording container described in the plan): a fixed header, a metadata chunk, and
an optional event chunk of fixed cells, each length-prefixed and checksummed so a
truncated or corrupted stream is rejected rather than guessed.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .._flight_schema import (
    BYTE_ORDER,
    CELL_SIZE,
    IMAGE_HASH_BYTES,
    METADATA_VERSION,
    SCHEMA_VERSION,
    CompletionCell,
    MetadataImage,
    NamedMeta,
    PlanMeta,
    RouteMeta,
    SchemaError,
)

#: Container magic. The trailing digit is the container (not schema) version.
MAGIC = b"WFR0"

#: Refuse to allocate for absurd declared sizes before the bytes are present.
MAX_CHUNK_BYTES = 64 * 1024 * 1024
MAX_ROWS = 5_000_000

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


# --- metadata image (de)serialization --------------------------------------
#
# Encoding mirrors MetadataImage.canonical_bytes exactly so a round trip is the
# identity and the container hash stays stable. Decoding is defensive: every
# length is bounds-checked before it is used to slice.


class _Reader:
    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def u32(self) -> int:
        if self._pos + 4 > len(self._data):
            raise SchemaError("metadata truncated reading a u32")
        (value,) = struct.unpack_from(BYTE_ORDER + "I", self._data, self._pos)
        self._pos += 4
        return value

    def text(self) -> str:
        length = self.u32()
        if length > MAX_CHUNK_BYTES:
            raise SchemaError("metadata string declares an implausible length")
        end = self._pos + length
        if end > len(self._data):
            raise SchemaError("metadata truncated reading a string")
        raw = self._data[self._pos : end]
        self._pos = end
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SchemaError("metadata string is not valid UTF-8") from exc

    def count(self) -> int:
        n = self.u32()
        if n > MAX_ROWS:
            raise SchemaError(f"metadata declares {n} rows, over the limit")
        return n

    def ids(self) -> tuple[int, ...]:
        return tuple(self.u32() for _ in range(self.count()))

    def at_end(self) -> bool:
        return self._pos == len(self._data)


def decode_metadata_image(data: bytes) -> MetadataImage:
    reader = _Reader(data)
    if data[:7] != b"WFRMETA":
        raise SchemaError("metadata image missing its marker")
    reader._pos = 7
    version = reader.u32()
    if version != METADATA_VERSION:
        raise SchemaError(f"unsupported metadata version {version}")

    routes: list[RouteMeta] = []
    for _ in range(reader.count()):
        route_id = reader.u32()
        method = reader.text()
        path = reader.text()
        operation_id = reader.text()
        plan_id = reader.u32()
        tags = tuple(reader.text() for _ in range(reader.count()))
        dependency_ids = reader.ids()
        middleware_ids = reader.ids()
        auth_policy_id = reader.u32()
        coverage = reader.text()
        routes.append(
            RouteMeta(
                route_id=route_id,
                method=method,
                path=path,
                operation_id=operation_id,
                plan_id=plan_id,
                tags=tags,
                dependency_ids=dependency_ids,
                middleware_ids=middleware_ids,
                auth_policy_id=auth_policy_id,
                coverage=coverage,
            )
        )

    plans: list[PlanMeta] = []
    for _ in range(reader.count()):
        plan_id = reader.u32()
        params = tuple(
            (reader.text(), reader.text(), reader.text())
            for _ in range(reader.count())
        )
        body_type = reader.text()
        returns_type = reader.text()
        serializer_id = reader.u32()
        validator_id = reader.u32()
        limit_ids = reader.ids()
        plans.append(
            PlanMeta(
                plan_id=plan_id,
                params=params,
                body_type=body_type,
                returns_type=returns_type,
                serializer_id=serializer_id,
                validator_id=validator_id,
                limit_ids=limit_ids,
            )
        )

    def _named() -> tuple[NamedMeta, ...]:
        return tuple(
            NamedMeta(entry_id=reader.u32(), name=reader.text())
            for _ in range(reader.count())
        )

    image = MetadataImage(
        version=version,
        routes=tuple(routes),
        plans=tuple(plans),
        dependencies=_named(),
        middleware=_named(),
        auth_policies=_named(),
        serializers=_named(),
        validators=_named(),
        limits=_named(),
        clients=_named(),
        databases=_named(),
        models=_named(),
        components=_named(),
    )
    if not reader.at_end():
        raise SchemaError("trailing bytes after the metadata image")
    return image


def decode_completion(cell: bytes) -> CompletionCell:
    """Convenience: decode one completion cell (schema-validated)."""
    return CompletionCell.decode(cell)


_HEXCHARS = frozenset(b"0123456789abcdef")


def parse_traceparent(data: bytes) -> tuple[int, int, int, bool] | None:
    """Strict W3C ``traceparent`` parse, the pure twin of the C parser.

    Returns ``(trace_hi, trace_lo, parent_span, sampled)`` or ``None`` for any
    malformed value. Lowercase hex only; rejects an ``ff`` version and all-zero
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

from .._flight_schema import (  # noqa: E402 - grouped with the oracle it serves
    FLAG_DETAILED_ARMED,
    FLAG_ERROR_PROMOTED,
    FLAG_SLOW_PROMOTED,
    HISTOGRAM_BUCKETS,
    PHASE_CELL_BUDGET,
    PHASE_RECORDS_PER_BATCH,
    LossReason,
    Mode,
    PhaseBatchCell,
    PhaseCoverage,
    PhaseKind,
    PhaseRecord,
    Protocol,
    TerminalStatus,
    histogram_bucket,
)

_U64 = 0xFFFFFFFFFFFFFFFF


def _mix64(x: int) -> int:
    """Stateless splitmix64 finalizer; mirrors mix64() in flight.c bit-for-bit."""
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _U64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _U64
    return x ^ (x >> 31)


class _PureRequest:
    __slots__ = ("_worker", "request_id", "active_slot", "_ctx", "_finished")

    def __init__(self, worker: PureRecorder, request_id: int, slot: int, ctx: dict) -> None:
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


class PureRecorder:
    """Pure twin of ``wreath._native._flight.Recorder`` (observable behavior)."""

    def __init__(self, mode: int, worker_id: int = 0, ring_records: int = 16384,
                 active_requests: int = 2048, histogram_count: int = 1,
                 completion_summaries: bool = True,
                 detailed_sample_rate: float = 0.0, phase_slots: int = 256,
                 detailed_slow_us: int = 0) -> None:
        if ring_records and (ring_records & (ring_records - 1)):
            raise ValueError("ring_records must be a power of two")
        if not 0.0 <= detailed_sample_rate <= 1.0:
            raise ValueError("detailed_sample_rate must be in [0, 1]")
        self.mode = int(mode)
        self._worker_id = worker_id
        self._detailed_sample_threshold = int(detailed_sample_rate * 4294967296.0 + 0.5)
        self._slow_threshold_us = detailed_slow_us
        # Phase scratch pool: only present in a mode that arms phases.
        self._phase_capacity = phase_slots if self.mode >= Mode.DETAILED else 0
        self._phase_free = list(range(self._phase_capacity - 1, -1, -1))
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
        self._active: set[int] = set()

    # counters / snapshots ----------------------------------------------------
    @property
    def requests(self) -> int:
        return self._requests

    @property
    def completions(self) -> int:
        return self._completions

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def ring_occupancy(self) -> int:
        return len(self._ring)

    @property
    def ring_high_water(self) -> int:
        return self._high_water

    def loss(self, reason: int) -> int:
        return self._losses[int(reason)] if 0 <= int(reason) < len(self._losses) else 0

    def histogram(self) -> tuple[int, ...]:
        return tuple(self._histogram)

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
            self._active.add(slot)
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
            if self._phase_free:
                phase_slot = self._phase_free.pop()
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
        }

    def _release(self, ctx: dict) -> None:
        slot = ctx.get("slot", -1)
        if slot >= 0 and slot in self._active:
            self._active.discard(slot)
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
            self._phase_release(ctx)  # no completion to anchor phases to
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
        ).encode()
        published = self._publish(cell)
        self._commit_phases(ctx, published)

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


_PROTO = frozenset(int(p) for p in Protocol)
_TERM = frozenset(int(t) for t in TerminalStatus)
_PHASE_KIND_SET = frozenset(int(k) for k in PhaseKind)
_COVERAGE_SET = frozenset(int(c) for c in PhaseCoverage)
