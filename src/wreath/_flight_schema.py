"""Fixed schema for the Native Flight Recorder (NFR).

This module is the single source of truth for the NFR wire format: cell sizes,
field layouts, event kinds, modes, loss reasons, and the deterministic metadata
image. The pure reference codec in `wreath._pure.flight` and the future
`wreath._native._flight` C extension must agree with the constants here
byte-for-byte; a parity test enforces that once the extension exists.

Nothing here performs runtime telemetry. Stage 0 defines the schema and the
deterministic metadata image only -- there is no recorder, ring, or request-path
behavior. See `docs/plans/native-flight-recorder-stage-1.md` and
`docs/decisions/0021-native-flight-recorder-provisional-parameters.md`.

The provisional sizes below are acceptance decisions to be tuned by the Stage 3
benchmark matrix, not frozen guarantees (ADR 0021).
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Final


def _require_layout(actual: int, expected: int, what: str) -> None:
    """Refuse to import a struct whose packed size is not the wire size.

    These are wire-format invariants, not developer sanity checks: a struct that
    packs to the wrong size produces cells a reader will silently misparse.

    They are written as a raise rather than an `assert` deliberately. `python -O`
    strips `assert`, so under optimization the eight layout checks in this module
    would vanish and the module would import with a wrong layout and no
    complaint -- and `-O` is the one interpreter mode nothing in this repository
    tests. A check that disappears under a supported flag is a check with
    nothing to check.

    Args:
        actual: The packed size the `struct.Struct` reports.
        expected: The size the wire format requires.
        what: The name of the structure, for the message.

    Raises:
        RuntimeError: If the sizes differ. Raised at import, so a mislaid format
            string cannot reach a recorder.
    """
    if actual != expected:
        raise RuntimeError(
            f"{what} packs to {actual} bytes, but the wire format requires "
            f"{expected}; the format string and the layout comment above it "
            "have diverged"
        )


# --- versioning -------------------------------------------------------------

#: Wire schema version. Readers reject an unknown major version rather than
#: guessing. Bump only for an incompatible cell/container layout change.
SCHEMA_VERSION: Final = 1

#: Metadata image container version, independent of the cell schema.
METADATA_VERSION: Final = 1

#: Everything is little-endian, fixed, regardless of host byte order.
BYTE_ORDER: Final = "<"

# --- cell sizes (bytes) -----------------------------------------------------

#: A completion/event/correlation cell. Trace IDs that do not fit a completion
#: cell travel in a paired correlation cell, never a variable-length record.
CELL_SIZE: Final = 64

#: A detail (phase) record. Only an armed request writes these. They accumulate
#: in a per-request scratch block and are committed to the ring at completion,
#: packed into 64-byte phase-batch cells.
PHASE_CELL_SIZE: Final = 16

#: Per-request phase budget (records). Exhaustion drops the phase and increments a
#: loss counter; it never spills or allocates.
PHASE_CELL_BUDGET: Final = 12

#: Phase records packed per 64-byte ring cell: a 16-byte batch header plus three
#: 16-byte records. The budget is a whole multiple, so a full request commits
#: exactly PHASE_CELL_BUDGET / PHASE_RECORDS_PER_BATCH batch cells.
PHASE_RECORDS_PER_BATCH: Final = 3


class EventKind(IntEnum):
    """The `kind` byte of a 64-byte cell."""

    INVALID = 0
    COMPLETION = 1  # one per request in Pulse
    CORRELATION = 2  # paired trace/span carrier for a completion
    PHASE = 3  # a promoted detail phase (Detailed mode)
    CONTROL = 4  # bounded arm/disarm/export control event
    CAPTURE = 5  # a forensic capture slab (Forensic mode; off the completion ring)


class Mode(IntEnum):
    """Recorder mode. Off performs zero request-path work."""

    OFF = 0
    PULSE = 1
    DETAILED = 2
    FORENSIC = 3


class PhaseKind(IntEnum):
    """The instrumented segment a phase record measures. Only armed (sampled)
    Detailed requests emit these; the values are stable wire identifiers that the
    request-path markers (app, auth, serializers, PostgreSQL, HTTP client) target."""

    UNKNOWN = 0
    INGRESS = 1  # request head parsed, scope built
    ROUTE_MATCH = 2  # router match/classify
    BINDING = 3  # request binding + validation
    AUTH = 4  # authorization
    HANDLER = 5  # application handler body
    SERIALIZE = 6  # response serialization
    DB_POOL_WAIT = 7  # waiting for a PostgreSQL pool connection
    DB_QUERY = 8  # a PostgreSQL statement round trip
    ORM_HYDRATE = 9  # ORM row hydration
    HTTP_CLIENT = 10  # an outbound Wreath HTTP call
    MIDDLEWARE = 11  # a middleware hook
    RESPONSE_WRITE = 12  # response framing/egress
    DI_CONSTRUCT = 13  # building an app-scoped dependency (first use only)
    WS_FANOUT = 14  # broadcasting to a WebSocket room
    RESOLVER = 15  # one GraphQL field resolver


class PhaseCoverage(IntEnum):
    """Where a phase executed, mirroring the metadata-image coverage strings."""

    NATIVE = 0
    PYTHON = 1
    EXTERNAL = 2
    UNKNOWN = 3


class Protocol(IntEnum):
    """Transport a completion was served over."""

    UNKNOWN = 0
    HTTP1 = 1
    HTTP2 = 2
    HTTP3 = 3
    WEBSOCKET = 4


class TerminalStatus(IntEnum):
    """Terminal disposition of a request, independent of HTTP status."""

    OK = 0
    ERROR = 1  # handler/application raised
    CANCELLED = 2
    DISCONNECTED = 3
    TIMEOUT = 4
    PROTOCOL_ERROR = 5


class LossReason(IntEnum):
    """Every dropped telemetry item increments exactly one of these. There is no
    "telemetry about telemetry" event; only these bounded counters."""

    RING_FULL = 0
    ACTIVE_TABLE_FULL = 1
    PHASE_SCRATCH_FULL = 2
    CAPTURE_POOL_FULL = 3
    EXPORT_QUEUE_FULL = 4
    ENTROPY_EXHAUSTED = 5
    PROPAGATION_INVALID = 6
    BODY_TRUNCATED = 7


#: Completion-cell `flags` bits.
FLAG_SAMPLED: Final = 1 << 0  # W3C sampled
FLAG_DETAILED_ARMED: Final = 1 << 1
FLAG_FORENSIC_ARMED: Final = 1 << 2
FLAG_ERROR_PROMOTED: Final = 1 << 3
FLAG_SLOW_PROMOTED: Final = 1 << 4
FLAG_PROPAGATION_VALID: Final = 1 << 5
FLAG_BODY_TRUNCATED: Final = 1 << 6
FLAG_TELEMETRY_LOSS: Final = 1 << 7
FLAG_HAS_CORRELATION: Final = 1 << 8  # a correlation cell follows

# --- histograms -------------------------------------------------------------

#: base-2 log buckets over the microsecond duration, clamped to [0, 63].
HISTOGRAM_BUCKETS: Final = 64


def histogram_bucket(duration_us: int) -> int:
    """The log2 bucket index for a microsecond duration, clamped to a valid bin."""
    if duration_us <= 1:
        return 0
    return min(duration_us.bit_length() - 1, HISTOGRAM_BUCKETS - 1)


# --- completion cell codec --------------------------------------------------
#
# 64-byte little-endian layout (offsets in bytes):
#   0  u8   schema_version
#   1  u8   kind (EventKind)
#   2  u16  flags
#   4  u32  status          (HTTP status, 0 when not applicable)
#   8  u64  request_id       (worker-local)
#  16  u64  connection_id    (worker-local)
#  24  u32  route_id
#  28  u32  plan_id
#  32  u64  duration_us
#  40  u64  bytes_in
#  48  u64  bytes_out
#  56  u8   protocol (Protocol)
#  57  u8   terminal (TerminalStatus)
#  58  u8   error_class
#  59  u8   worker_id
#  60  u32  reserved (zero)
_COMPLETION = struct.Struct(BYTE_ORDER + "BBHIQQIIQQQBBBBI")
_require_layout(_COMPLETION.size, CELL_SIZE, "the completion cell")


@dataclass(frozen=True, slots=True)
class CompletionCell:
    """A decoded completion cell. The default Pulse record."""

    request_id: int
    connection_id: int
    route_id: int
    plan_id: int
    duration_us: int
    status: int
    bytes_in: int
    bytes_out: int
    protocol: Protocol = Protocol.UNKNOWN
    terminal: TerminalStatus = TerminalStatus.OK
    error_class: int = 0
    worker_id: int = 0
    flags: int = 0
    #: Monotonic end instant as milliseconds from the worker's clock epoch. The
    #: projector maps it to Unix time via the recorder's clock calibration.
    end_offset_ms: int = 0

    def encode(self) -> bytes:
        return _COMPLETION.pack(
            SCHEMA_VERSION,
            EventKind.COMPLETION,
            self.flags & 0xFFFF,
            self.status & 0xFFFFFFFF,
            self.request_id & 0xFFFFFFFFFFFFFFFF,
            self.connection_id & 0xFFFFFFFFFFFFFFFF,
            self.route_id & 0xFFFFFFFF,
            self.plan_id & 0xFFFFFFFF,
            self.duration_us & 0xFFFFFFFFFFFFFFFF,
            self.bytes_in & 0xFFFFFFFFFFFFFFFF,
            self.bytes_out & 0xFFFFFFFFFFFFFFFF,
            int(self.protocol) & 0xFF,
            int(self.terminal) & 0xFF,
            self.error_class & 0xFF,
            self.worker_id & 0xFF,
            self.end_offset_ms & 0xFFFFFFFF,
        )

    @classmethod
    def decode(cls, data: bytes) -> CompletionCell:
        if len(data) < CELL_SIZE:
            raise SchemaError(f"completion cell needs {CELL_SIZE} bytes, got {len(data)}")
        (
            version,
            kind,
            flags,
            status,
            request_id,
            connection_id,
            route_id,
            plan_id,
            duration_us,
            bytes_in,
            bytes_out,
            protocol,
            terminal,
            error_class,
            worker_id,
            end_offset_ms,
        ) = _COMPLETION.unpack(data[:CELL_SIZE])
        if version != SCHEMA_VERSION:
            raise SchemaError(f"unsupported schema version {version}")
        if kind != EventKind.COMPLETION:
            raise SchemaError(f"expected completion kind, got {kind}")
        return cls(
            request_id=request_id,
            connection_id=connection_id,
            route_id=route_id,
            plan_id=plan_id,
            duration_us=duration_us,
            status=status,
            bytes_in=bytes_in,
            bytes_out=bytes_out,
            protocol=Protocol(protocol) if protocol in _PROTOCOLS else Protocol.UNKNOWN,
            terminal=(
                TerminalStatus(terminal) if terminal in _TERMINALS else TerminalStatus.OK
            ),
            error_class=error_class,
            worker_id=worker_id,
            flags=flags,
            end_offset_ms=end_offset_ms,
        )


_PROTOCOLS = frozenset(int(p) for p in Protocol)
_TERMINALS = frozenset(int(t) for t in TerminalStatus)


# --- correlation cell codec -------------------------------------------------
#
# 64-byte little-endian layout:
#   0  u8   schema_version
#   1  u8   kind (EventKind.CORRELATION)
#   2  u16  flags
#   4  u32  reserved
#   8  u64  request_id
#  16  u64  trace_id_hi
#  24  u64  trace_id_lo
#  32  u64  parent_span_id
#  40  u64  span_id
#  48  16 bytes reserved
_CORRELATION = struct.Struct(BYTE_ORDER + "BBHIQQQQQ16x")
_require_layout(_CORRELATION.size, CELL_SIZE, "the correlation cell")


@dataclass(frozen=True, slots=True)
class CorrelationCell:
    """The trace/span carrier paired with a completion when propagation is present."""

    request_id: int
    trace_id: int  # 128-bit
    span_id: int
    parent_span_id: int = 0
    flags: int = 0

    def encode(self) -> bytes:
        return _CORRELATION.pack(
            SCHEMA_VERSION,
            EventKind.CORRELATION,
            self.flags & 0xFFFF,
            0,
            self.request_id & 0xFFFFFFFFFFFFFFFF,
            (self.trace_id >> 64) & 0xFFFFFFFFFFFFFFFF,
            self.trace_id & 0xFFFFFFFFFFFFFFFF,
            self.parent_span_id & 0xFFFFFFFFFFFFFFFF,
            self.span_id & 0xFFFFFFFFFFFFFFFF,
        )

    @classmethod
    def decode(cls, data: bytes) -> CorrelationCell:
        if len(data) < CELL_SIZE:
            raise SchemaError(f"correlation cell needs {CELL_SIZE} bytes, got {len(data)}")
        version, kind, flags, _r, request_id, hi, lo, parent, span = _CORRELATION.unpack(
            data[:CELL_SIZE]
        )
        if version != SCHEMA_VERSION:
            raise SchemaError(f"unsupported schema version {version}")
        if kind != EventKind.CORRELATION:
            raise SchemaError(f"expected correlation kind, got {kind}")
        return cls(
            request_id=request_id,
            trace_id=(hi << 64) | lo,
            span_id=span,
            parent_span_id=parent,
            flags=flags,
        )


# --- phase (detail) codec ---------------------------------------------------
#
# A phase record is 16 bytes. Records are committed to the ring inside 64-byte
# phase-batch cells: a 16-byte header (schema_version, kind=PHASE, count,
# worker_id, request_id) plus up to three records. The request_id makes each
# batch self-identifying, so phases attach to their completion by id rather than
# by ring position (robust to drops and reordering).
#
# 16-byte record layout:
#   0  u16  phase_id (PhaseKind)
#   2  u16  dependency_id  (metadata id, truncated; 0 = none)
#   4  u8   coverage (PhaseCoverage)
#   5  u8   sequence  (0-based order within the request)
#   6  u16  reserved (zero)
#   8  u32  start_offset_us  (from request start)
#  12  u32  duration_us
_PHASE_RECORD = struct.Struct(BYTE_ORDER + "HHBBHII")
_require_layout(_PHASE_RECORD.size, PHASE_CELL_SIZE, "the phase record")

# 16-byte batch header:
#   0  u8   schema_version
#   1  u8   kind (EventKind.PHASE)
#   2  u8   count  (1..PHASE_RECORDS_PER_BATCH)
#   3  u8   worker_id
#   4  u32  reserved (zero)
#   8  u64  request_id
_PHASE_BATCH_HEADER = struct.Struct(BYTE_ORDER + "BBBBIQ")
_require_layout(_PHASE_BATCH_HEADER.size, PHASE_CELL_SIZE, "the phase batch header")
_require_layout(
    _PHASE_BATCH_HEADER.size + PHASE_RECORDS_PER_BATCH * _PHASE_RECORD.size,
    CELL_SIZE,
    "the phase batch",
)


@dataclass(frozen=True, slots=True)
class PhaseRecord:
    """One measured phase within an armed request."""

    phase_id: PhaseKind
    duration_us: int
    start_offset_us: int = 0
    dependency_id: int = 0
    coverage: PhaseCoverage = PhaseCoverage.UNKNOWN
    sequence: int = 0

    def encode(self) -> bytes:
        return _PHASE_RECORD.pack(
            int(self.phase_id) & 0xFFFF,
            self.dependency_id & 0xFFFF,
            int(self.coverage) & 0xFF,
            self.sequence & 0xFF,
            0,
            self.start_offset_us & 0xFFFFFFFF,
            self.duration_us & 0xFFFFFFFF,
        )

    @classmethod
    def decode(cls, data: bytes) -> PhaseRecord:
        if len(data) < PHASE_CELL_SIZE:
            raise SchemaError(f"phase record needs {PHASE_CELL_SIZE} bytes, got {len(data)}")
        phase_id, dep, coverage, seq, _r, start_off, dur = _PHASE_RECORD.unpack(
            data[:PHASE_CELL_SIZE]
        )
        return cls(
            phase_id=PhaseKind(phase_id) if phase_id in _PHASE_KINDS else PhaseKind.UNKNOWN,
            duration_us=dur,
            start_offset_us=start_off,
            dependency_id=dep,
            coverage=(
                PhaseCoverage(coverage) if coverage in _COVERAGES else PhaseCoverage.UNKNOWN
            ),
            sequence=seq,
        )


@dataclass(frozen=True, slots=True)
class PhaseBatchCell:
    """A 64-byte ring cell carrying up to PHASE_RECORDS_PER_BATCH phase records
    for one request, tagged with its request_id."""

    request_id: int
    records: tuple[PhaseRecord, ...]
    worker_id: int = 0

    def encode(self) -> bytes:
        if not 1 <= len(self.records) <= PHASE_RECORDS_PER_BATCH:
            raise SchemaError(f"a phase batch holds 1..{PHASE_RECORDS_PER_BATCH} records")
        header = _PHASE_BATCH_HEADER.pack(
            SCHEMA_VERSION,
            EventKind.PHASE,
            len(self.records),
            self.worker_id & 0xFF,
            0,
            self.request_id & 0xFFFFFFFFFFFFFFFF,
        )
        body = b"".join(record.encode() for record in self.records)
        # Pad unused record slots with zero so every ring cell is exactly 64 bytes.
        body += b"\x00" * (CELL_SIZE - len(header) - len(body))
        return header + body

    @classmethod
    def decode(cls, data: bytes) -> PhaseBatchCell:
        if len(data) < CELL_SIZE:
            raise SchemaError(f"phase batch cell needs {CELL_SIZE} bytes, got {len(data)}")
        version, kind, count, worker_id, _r, request_id = _PHASE_BATCH_HEADER.unpack(
            data[:PHASE_CELL_SIZE]
        )
        if version != SCHEMA_VERSION:
            raise SchemaError(f"unsupported schema version {version}")
        if kind != EventKind.PHASE:
            raise SchemaError(f"expected phase kind, got {kind}")
        if not 1 <= count <= PHASE_RECORDS_PER_BATCH:
            raise SchemaError(f"phase batch count out of range: {count}")
        records = tuple(
            PhaseRecord.decode(
                data[(i + 1) * PHASE_CELL_SIZE : (i + 2) * PHASE_CELL_SIZE]
            )
            for i in range(count)
        )
        return cls(request_id=request_id, records=records, worker_id=worker_id)


_PHASE_KINDS = frozenset(int(k) for k in PhaseKind)
_COVERAGES = frozenset(int(c) for c in PhaseCoverage)


class SchemaError(ValueError):
    """A cell or metadata image failed to decode against this schema."""


# --- forensic capture (Stage 5) --------------------------------------------
#
# Forensic mode is the only mode that ever copies application bytes, and it does
# so under a deny-by-default policy: a field is captured only when a compiled
# rule produces it, and every byte is redacted (hashed/masked/length-only) or
# bounded (truncated) *before* it enters a slab. Captured fields for one armed
# request accumulate in a preallocated slab -- a self-identifying block tagged
# with the request id, laid out as a fixed header followed by typed field
# records -- which the off-path sink copies out and serializes. Slabs never
# travel the 64-byte completion ring; they have their own commit/return path.


class CaptureFieldClass(IntEnum):
    """The boundary a captured field came from. Stable wire identifiers targeted
    by the request-path capture seams (server, PostgreSQL, HTTP client)."""

    UNKNOWN = 0
    REQUEST_HEADER = 1
    RESPONSE_HEADER = 2
    REQUEST_BODY = 3
    RESPONSE_BODY = 4
    QUERY_PARAM = 5
    DB_PARAM = 6
    DB_ROW = 7
    OUTBOUND_REQUEST = 8
    OUTBOUND_RESPONSE = 9


class CaptureDisposition(IntEnum):
    """How a field was reduced before retention. The policy decides which; the
    native core enforces it as the bytes are written, so a disallowed field's
    raw bytes never exist in recorder memory."""

    RAW = 0  # policy-approved verbatim bytes, bounded by the slab (may truncate)
    HASHED = 1  # an 8-byte process-local keyed hash, never the bytes
    MASKED = 2  # a constant mask; only the original length is retained
    LENGTH = 3  # length only (bytes dropped)


#: Bytes of a keyed redaction hash (SipHash-2-4 output, truncated to 64 bits).
CAPTURE_HASH_BYTES: Final = 8

#: Record alignment inside a slab, so each field header stays naturally aligned.
CAPTURE_FIELD_ALIGN: Final = 4

# 24-byte capture-slab header (little-endian):
#   0  u64  request_id       (self-identifying, like a phase batch)
#   8  u32  used_bytes       (header + all field records, for the sink)
#  12  u16  field_count
#  14  u8   schema_version
#  15  u8   kind (EventKind.CAPTURE)
#  16  u8   worker_id
#  17  u8   flags            (FLAG_BODY_TRUNCATED when any field was truncated)
#  18  u16  reserved (zero)
#  20  u32  reserved2 (zero)
_CAPTURE_SLAB_HEADER = struct.Struct(BYTE_ORDER + "QIHBBBBHI")
CAPTURE_SLAB_HEADER_SIZE: Final = _CAPTURE_SLAB_HEADER.size
_require_layout(CAPTURE_SLAB_HEADER_SIZE, 24, "the capture slab header")

# 12-byte capture-field header (little-endian), followed by `stored_length`
# payload bytes padded up to CAPTURE_FIELD_ALIGN:
#   0  u16  field_class (CaptureFieldClass)
#   2  u16  descriptor_id  (compiled metadata id: header/column/client, 0 = none)
#   4  u8   disposition (CaptureDisposition)
#   5  u8   reserved (zero)
#   6  u16  stored_length  (payload bytes retained in the slab)
#   8  u32  original_length  (the field's true length before redaction/truncation)
_CAPTURE_FIELD_HEADER = struct.Struct(BYTE_ORDER + "HHBBHI")
CAPTURE_FIELD_HEADER_SIZE: Final = _CAPTURE_FIELD_HEADER.size
_require_layout(CAPTURE_FIELD_HEADER_SIZE, 12, "the capture field header")


def _pad4(n: int) -> int:
    """Round a byte count up to the slab record alignment."""
    return (n + CAPTURE_FIELD_ALIGN - 1) & ~(CAPTURE_FIELD_ALIGN - 1)


@dataclass(frozen=True, slots=True)
class CaptureField:
    """One captured field decoded from a slab."""

    field_class: CaptureFieldClass
    descriptor_id: int
    disposition: CaptureDisposition
    original_length: int
    payload: bytes = b""  # stored bytes (raw prefix, or the 8-byte keyed hash)

    @property
    def truncated(self) -> bool:
        """True when a RAW field was clipped to fit the slab."""
        return (
            self.disposition is CaptureDisposition.RAW
            and len(self.payload) < self.original_length
        )


@dataclass(frozen=True, slots=True)
class CaptureSlab:
    """A decoded capture slab: one armed request's retained forensic fields."""

    request_id: int
    fields: tuple[CaptureField, ...]
    worker_id: int = 0
    flags: int = 0

    @classmethod
    def decode(cls, data: bytes) -> CaptureSlab:
        if len(data) < CAPTURE_SLAB_HEADER_SIZE:
            raise SchemaError(
                f"capture slab needs {CAPTURE_SLAB_HEADER_SIZE} bytes, got {len(data)}"
            )
        (
            request_id,
            used_bytes,
            field_count,
            version,
            kind,
            worker_id,
            flags,
            _r0,
            _r1,
        ) = _CAPTURE_SLAB_HEADER.unpack_from(data, 0)
        if version != SCHEMA_VERSION:
            raise SchemaError(f"unsupported schema version {version}")
        if kind != EventKind.CAPTURE:
            raise SchemaError(f"expected capture kind, got {kind}")
        if used_bytes > len(data):
            raise SchemaError("capture slab used_bytes exceeds the buffer")
        fields: list[CaptureField] = []
        offset = CAPTURE_SLAB_HEADER_SIZE
        for _ in range(field_count):
            if offset + CAPTURE_FIELD_HEADER_SIZE > used_bytes:
                raise SchemaError("capture slab truncated reading a field header")
            fc, descriptor_id, disposition, _res, stored_length, original_length = (
                _CAPTURE_FIELD_HEADER.unpack_from(data, offset)
            )
            offset += CAPTURE_FIELD_HEADER_SIZE
            end = offset + stored_length
            if end > used_bytes:
                raise SchemaError("capture slab truncated reading a field payload")
            payload = bytes(data[offset:end])
            offset += _pad4(stored_length)
            fields.append(
                CaptureField(
                    field_class=(
                        CaptureFieldClass(fc)
                        if fc in _CAPTURE_CLASSES
                        else CaptureFieldClass.UNKNOWN
                    ),
                    descriptor_id=descriptor_id,
                    disposition=(
                        CaptureDisposition(disposition)
                        if disposition in _CAPTURE_DISPOSITIONS
                        else CaptureDisposition.LENGTH
                    ),
                    original_length=original_length,
                    payload=payload,
                )
            )
        return cls(
            request_id=request_id,
            fields=tuple(fields),
            worker_id=worker_id,
            flags=flags,
        )


_CAPTURE_CLASSES = frozenset(int(c) for c in CaptureFieldClass)
_CAPTURE_DISPOSITIONS = frozenset(int(d) for d in CaptureDisposition)


# --- deterministic metadata image ------------------------------------------
#
# Runtime records carry only numeric IDs. The metadata image is the canonical,
# versioned table that gives those IDs meaning. IDs are deterministic within an
# application image: descriptors are canonicalized and the image is hashed so the
# same application produces byte-identical metadata regardless of process,
# address-space layout, or registration order (where order is not semantic).

#: Reserved IDs. 0 always means "none/unknown" for every table.
ID_NONE: Final = 0

#: Truncated image-hash width carried on cells / in the container header.
IMAGE_HASH_BYTES: Final = 16


@dataclass(frozen=True, slots=True)
class RouteMeta:
    route_id: int
    method: str
    path: str
    operation_id: str
    plan_id: int
    tags: tuple[str, ...]
    dependency_ids: tuple[int, ...]
    middleware_ids: tuple[int, ...]
    auth_policy_id: int
    #: One of "native", "python", "mixed", "external", "unknown".
    coverage: str = "unknown"


@dataclass(frozen=True, slots=True)
class PlanMeta:
    """An immutable endpoint-plan descriptor: the compiled shape of a handler,
    beside its executable closures rather than replacing them."""

    plan_id: int
    #: (name, kind, type_name) for each bound parameter, canonically ordered.
    params: tuple[tuple[str, str, str], ...]
    body_type: str
    returns_type: str
    serializer_id: int
    validator_id: int
    limit_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class NamedMeta:
    """A generic (id, name) entry for dependency/middleware/policy/serializer/
    validator/limit/client/database/model tables."""

    entry_id: int
    name: str


@dataclass(frozen=True, slots=True)
class MetadataImage:
    """The canonical numeric metadata for one application image."""

    version: int
    routes: tuple[RouteMeta, ...]
    plans: tuple[PlanMeta, ...]
    dependencies: tuple[NamedMeta, ...]
    middleware: tuple[NamedMeta, ...]
    auth_policies: tuple[NamedMeta, ...]
    serializers: tuple[NamedMeta, ...]
    validators: tuple[NamedMeta, ...]
    limits: tuple[NamedMeta, ...]
    clients: tuple[NamedMeta, ...]
    databases: tuple[NamedMeta, ...]
    models: tuple[NamedMeta, ...]
    components: tuple[NamedMeta, ...] = ()

    def canonical_bytes(self) -> bytes:
        """A deterministic byte serialization for hashing and equality.

        Only stable descriptor content enters here: no addresses, no `repr()`,
        no `PYTHONHASHSEED`-dependent hashing. Rows are emitted in ascending
        ID order so registration order that is not semantic cannot change it.
        """
        parts: list[bytes] = [
            b"WFRMETA",
            _u32(self.version),
        ]

        def _named(rows: tuple[NamedMeta, ...]) -> None:
            parts.append(_u32(len(rows)))
            for row in sorted(rows, key=lambda r: r.entry_id):
                parts.append(_u32(row.entry_id))
                parts.append(_text(row.name))

        parts.append(_u32(len(self.routes)))
        for r in sorted(self.routes, key=lambda r: r.route_id):
            parts.append(_u32(r.route_id))
            parts.append(_text(r.method))
            parts.append(_text(r.path))
            parts.append(_text(r.operation_id))
            parts.append(_u32(r.plan_id))
            parts.append(_u32(len(r.tags)))
            for tag in r.tags:
                parts.append(_text(tag))
            parts.append(_ids(r.dependency_ids))
            parts.append(_ids(r.middleware_ids))
            parts.append(_u32(r.auth_policy_id))
            parts.append(_text(r.coverage))

        parts.append(_u32(len(self.plans)))
        for p in sorted(self.plans, key=lambda p: p.plan_id):
            parts.append(_u32(p.plan_id))
            parts.append(_u32(len(p.params)))
            for name, kind, type_name in p.params:
                parts.append(_text(name))
                parts.append(_text(kind))
                parts.append(_text(type_name))
            parts.append(_text(p.body_type))
            parts.append(_text(p.returns_type))
            parts.append(_u32(p.serializer_id))
            parts.append(_u32(p.validator_id))
            parts.append(_ids(p.limit_ids))

        for rows in (
            self.dependencies,
            self.middleware,
            self.auth_policies,
            self.serializers,
            self.validators,
            self.limits,
            self.clients,
            self.databases,
            self.models,
            self.components,
        ):
            _named(rows)
        return b"".join(parts)

    def image_hash(self) -> bytes:
        """The full 32-byte BLAKE2b digest of the canonical form."""
        return hashlib.blake2b(self.canonical_bytes(), digest_size=32).digest()

    def image_hash_short(self) -> bytes:
        """The truncated hash carried on cells / in the container header."""
        return self.image_hash()[:IMAGE_HASH_BYTES]


def _u32(value: int) -> bytes:
    if value < 0 or value > 0xFFFFFFFF:
        raise SchemaError(f"u32 out of range: {value}")
    return struct.pack(BYTE_ORDER + "I", value)


def _text(value: str) -> bytes:
    raw = value.encode("utf-8")
    return _u32(len(raw)) + raw


def _ids(ids: tuple[int, ...]) -> bytes:
    # IDs are sorted so a non-semantic ordering cannot change the image.
    ordered = sorted(ids)
    return _u32(len(ordered)) + b"".join(_u32(i) for i in ordered)
