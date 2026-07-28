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
    LOG = 6  # one application log record, joined to its trace by request_id


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
    #: A request's buffered log scratch was exhausted before promotion.
    LOG_SCRATCH_FULL = 8
    #: A record's arguments did not fit the cell's inline area and were clipped.
    LOG_ARGS_TRUNCATED = 9
    #: A call site could not be interned; its records take the uninterned path.
    LOG_SITE_TABLE_FULL = 10
    #: The per-call-site limiter dropped a record (INFO and below only).
    LOG_SAMPLED = 11
    #: A record was emitted off the recorder's writer thread and took the slow
    #: path. Counted separately because it is a *shape* problem in the caller,
    #: not backpressure: the fix is to move the call, not to raise a budget.
    LOG_OFF_LOOP = 12


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


# --- log records ------------------------------------------------------------
#
# One application log record is one 64-byte ring cell, published by the same
# writer as a completion and joined to its trace by request_id -- so a record
# never carries a trace or span id of its own. Duplicating 24 bytes of
# correlation onto every record would buy nothing the projector cannot already
# reconstruct from the correlation carrier, and log records outnumber
# completions by one to two orders of magnitude.
#
# The static half of a log statement -- template, severity, module, line, field
# names, argument types, redaction dispositions -- is interned once into the
# metadata image and addressed by `site_id`. Only the dynamic arguments travel
# per record. That is the static/dynamic split NanoLog gets from a compile-time
# pass; Python has none, so the binding happens at import instead.
#
# =========================================================================
# DEFERRED BY DECISION -- this packing is Python, and that is not an oversight
# =========================================================================
#
# Every log record is currently packed *here*, in Python, then handed to the
# ring as bytes:
#
#     _logsite.pack_value()   PyObject*  -> LogArg      (per argument)
#     LogCell.encode()        LogArg...  -> 64 bytes    (this module)
#     Recorder.publish_log()  bytes      -> ring        (_flightmodule.c)
#
# The native emitter that replaces the first two steps -- `wreath_nfr_log`,
# packing straight into a ring slot from C -- is **not built**. The layout,
# the declared argument types, and the level check that precedes marshalling
# all exist to make that swap mechanical rather than a redesign, but nobody
# has done it. Until then:
#
#   * No performance claim may be made about logging. None is made in the
#     docs, the ADR, or these docstrings, and none should be added.
#   * `wreath_nfr_publish_cell` (flight.c) is the seam. A C emitter replaces
#     what happens *above* that call, not the call itself.
#
# Also deferred, each decided rather than forgotten -- see
# docs/plans/first-class-logging.md for the full list and ADR 0025 for why:
#
#   * **Benchmarks.** Six of them, none run. The load-bearing one is the cost
#     of a *disabled* `DEBUG(...)` call: failure-triggered logging assumes
#     verbose instrumentation is affordable, and that assumption is exactly
#     that number.
#   * **Crash forensics (stage 7).** The ring is anonymous memory, so a
#     segfault takes the last records with it. The framing is ready for a
#     file-backed ring -- every cell is versioned and every decode validates
#     lengths against the buffer -- so adding it is not a format break.
#   * **Off-loop emission.** The ring is single-writer. `wreath.jobs` and
#     thread-pool work have no fast path; `LossReason.LOG_OFF_LOOP` is
#     reserved for the counted slow path that is not built, because nothing
#     yet emits from off the loop.
#   * **`wreath.audit`.** Keeps its own `logging.getLogger` path. "Never
#     blocks the request path" and "never loses a record" are incompatible
#     promises; audit needs the second and must not inherit the first.
#
# =========================================================================


class Severity(IntEnum):
    """OpenTelemetry SeverityNumber, at the base of each band.

    The OTel data model gives each band four values (TRACE 1-4, DEBUG 5-8, INFO
    9-12, WARN 13-16, ERROR 17-20, FATAL 21-24). Wreath emits the band base; the
    extra granularity exists so a bridged record from another system can keep
    its own gradation without inventing a parallel scheme.
    """

    TRACE = 1
    DEBUG = 5
    INFO = 9
    WARN = 13
    ERROR = 17
    FATAL = 21


#: Band names indexed by `(severity - 1) // 4`.
_SEVERITY_BANDS: Final = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL")

#: stdlib level -> band base, descending so a level between bands rounds down.
_STDLIB_TO_SEVERITY: Final = (
    (50, Severity.FATAL),
    (40, Severity.ERROR),
    (30, Severity.WARN),
    (20, Severity.INFO),
    (10, Severity.DEBUG),
)

#: Band base -> the stdlib level it round-trips to. TRACE has no stdlib name;
#: 5 is the conventional "below DEBUG" level.
_SEVERITY_TO_STDLIB: Final = {
    Severity.TRACE: 5,
    Severity.DEBUG: 10,
    Severity.INFO: 20,
    Severity.WARN: 30,
    Severity.ERROR: 40,
    Severity.FATAL: 50,
}


def severity_text(severity: int) -> str:
    """The band name for a severity number, derived rather than stored.

    Storing a severity *string* per record would cost more bytes than the whole
    fixed header; the name is a function of the number, so records carry the
    number and readers compute the name. Values outside 1..24 clamp to the
    nearest band rather than raising: a bridged record with a nonsense level is
    still worth reading.
    """
    band = (int(severity) - 1) // 4
    if band < 0:
        return _SEVERITY_BANDS[0]
    if band >= len(_SEVERITY_BANDS):
        return _SEVERITY_BANDS[-1]
    return _SEVERITY_BANDS[band]


def severity_from_stdlib(level: int) -> Severity:
    """Map a `logging` level onto a band base, rounding down between bands."""
    for threshold, severity in _STDLIB_TO_SEVERITY:
        if level >= threshold:
            return severity
    return Severity.TRACE


def severity_to_stdlib(severity: int) -> int:
    """Map a band base back onto its `logging` level."""
    band = (int(severity) - 1) // 4
    band = min(max(band, 0), len(_SEVERITY_BANDS) - 1)
    return _SEVERITY_TO_STDLIB[Severity(1 + band * 4)]


class LogArgType(IntEnum):
    """The type tag leading each packed argument.

    Arguments are self-describing even though the call site already declares
    their types, because the decoder must survive a stale metadata image and a
    torn record: one byte per argument buys a decode that validates instead of
    trusting. That property is what lets this layout become an on-disk format
    later without a second framing pass.
    """

    NONE = 0  # no payload
    BOOL = 1  # u8 0/1
    INT = 2  # i64
    FLOAT = 3  # f64 (IEEE 754 binary64)
    STR = 4  # u8 byte length, then that many UTF-8 bytes
    HASH = 5  # u64 keyed SipHash of a redacted value; never the bytes
    LENGTH = 6  # u32 original length of a value whose bytes were dropped


#: Payload width for the fixed-size argument types, tag byte excluded.
_LOG_ARG_FIXED_WIDTH: Final = {
    LogArgType.NONE: 0,
    LogArgType.BOOL: 1,
    LogArgType.INT: 8,
    LogArgType.FLOAT: 8,
    LogArgType.HASH: 8,
    LogArgType.LENGTH: 4,
}

_LOG_ARG_INT = struct.Struct(BYTE_ORDER + "q")
_LOG_ARG_FLOAT = struct.Struct(BYTE_ORDER + "d")
_LOG_ARG_HASH = struct.Struct(BYTE_ORDER + "Q")
_LOG_ARG_LENGTH = struct.Struct(BYTE_ORDER + "I")

#: Bytes of a log cell given over to packed arguments. Three 64-bit arguments
#: (9 bytes each with their tag) fit, which covers the common shape.
LOG_INLINE_ARG_BYTES: Final = 32

#: Arguments retained per record. A site declaring more is a design smell; the
#: excess is clipped and counted rather than spilled to a slab.
LOG_MAX_ARGS: Final = 8

#: Log-cell `flags` bits. Deliberately a separate namespace from the completion
#: cell's FLAG_*: these describe a record, not a request.
LOG_FLAG_PROMOTED: Final = 1 << 0  # buffered, then published by a promotion
LOG_FLAG_TRUNCATED: Final = 1 << 1  # an argument was clipped or dropped
LOG_FLAG_REDACTED: Final = 1 << 2  # an argument was hashed or reduced to a length
LOG_FLAG_OFF_LOOP: Final = 1 << 3  # emitted off the recorder's writer thread
#: The record carries application fields for its request's canonical log line
#: rather than a message of its own. Its arguments are the wide event's
#: attributes; a reader folds them into the completion rather than printing them
#: as a line.
LOG_FLAG_EVENT_FIELDS: Final = 1 << 4


@dataclass(frozen=True, slots=True)
class LogArg:
    """One packed log argument.

    Constructed through the named classmethods rather than positionally, so a
    call site cannot accidentally pass a hash where a length belongs.
    """

    type: LogArgType
    number: int = 0  # BOOL / INT / HASH / LENGTH payload
    fraction: float = 0.0  # FLOAT payload
    payload: bytes = b""  # STR payload, UTF-8

    @classmethod
    def none(cls) -> LogArg:
        return cls(LogArgType.NONE)

    @classmethod
    def boolean(cls, value: bool) -> LogArg:
        return cls(LogArgType.BOOL, number=1 if value else 0)

    @classmethod
    def integer(cls, value: int) -> LogArg:
        return cls(LogArgType.INT, number=value)

    @classmethod
    def real(cls, value: float) -> LogArg:
        return cls(LogArgType.FLOAT, fraction=value)

    @classmethod
    def text(cls, value: str) -> LogArg:
        return cls(LogArgType.STR, payload=value.encode("utf-8"))

    @classmethod
    def hashed(cls, fingerprint: int) -> LogArg:
        return cls(LogArgType.HASH, number=fingerprint)

    @classmethod
    def length(cls, original_length: int) -> LogArg:
        return cls(LogArgType.LENGTH, number=original_length)

    @property
    def text_value(self) -> str:
        """The decoded string of a STR argument, empty for every other type."""
        return self.payload.decode("utf-8")

    @property
    def redacted(self) -> bool:
        """True when the argument carries a fingerprint or a length, not a value."""
        return self.type in (LogArgType.HASH, LogArgType.LENGTH)


def _clip_utf8(raw: bytes, limit: int) -> bytes:
    """The longest prefix of `raw` that fits `limit` bytes and still decodes.

    Clipping mid-sequence would produce a record that raises on read, which is
    the one failure a log line must never have.
    """
    if len(raw) <= limit:
        return raw
    end = limit
    while end > 0 and (raw[end] & 0xC0) == 0x80:
        end -= 1
    return raw[:end]


def _encode_log_arg(arg: LogArg, budget: int) -> tuple[bytes, bool] | None:
    """Pack one argument into at most `budget` bytes.

    Returns the bytes and whether the value was clipped, or None when even a
    clipped form does not fit and the argument must be dropped.
    """
    if arg.type is LogArgType.STR:
        if budget < 3:  # tag + length + at least one byte
            return None
        clipped = _clip_utf8(arg.payload, min(budget - 2, 0xFF))
        return (
            bytes((LogArgType.STR, len(clipped))) + clipped,
            len(clipped) != len(arg.payload),
        )
    width = _LOG_ARG_FIXED_WIDTH[arg.type]
    if budget < 1 + width:
        return None
    if arg.type is LogArgType.NONE:
        return bytes((LogArgType.NONE,)), False
    if arg.type is LogArgType.BOOL:
        return bytes((LogArgType.BOOL, 1 if arg.number else 0)), False
    if arg.type is LogArgType.INT:
        return bytes((LogArgType.INT,)) + _LOG_ARG_INT.pack(arg.number), False
    if arg.type is LogArgType.FLOAT:
        return bytes((LogArgType.FLOAT,)) + _LOG_ARG_FLOAT.pack(arg.fraction), False
    if arg.type is LogArgType.HASH:
        return bytes((LogArgType.HASH,)) + _LOG_ARG_HASH.pack(arg.number), False
    return bytes((LogArgType.LENGTH,)) + _LOG_ARG_LENGTH.pack(arg.number), False


def _decode_log_args(blob: memoryview) -> tuple[LogArg, ...]:
    """Decode packed arguments, validating every length against the buffer."""
    args: list[LogArg] = []
    offset = 0
    end = len(blob)
    while offset < end:
        tag = blob[offset]
        offset += 1
        if tag not in _LOG_ARG_TYPES:
            raise SchemaError(f"unknown log argument type {tag}")
        kind = LogArgType(tag)
        if kind is LogArgType.STR:
            if offset >= end:
                raise SchemaError("log cell truncated reading an argument length")
            size = blob[offset]
            offset += 1
            if offset + size > end:
                raise SchemaError("log cell truncated reading an argument payload")
            args.append(LogArg(kind, payload=bytes(blob[offset : offset + size])))
            offset += size
            continue
        width = _LOG_ARG_FIXED_WIDTH[kind]
        if offset + width > end:
            raise SchemaError("log cell truncated reading an argument payload")
        chunk = blob[offset : offset + width]
        offset += width
        if kind is LogArgType.NONE:
            args.append(LogArg(kind))
        elif kind is LogArgType.BOOL:
            args.append(LogArg(kind, number=1 if chunk[0] else 0))
        elif kind is LogArgType.INT:
            args.append(LogArg(kind, number=_LOG_ARG_INT.unpack(chunk)[0]))
        elif kind is LogArgType.FLOAT:
            args.append(LogArg(kind, fraction=_LOG_ARG_FLOAT.unpack(chunk)[0]))
        elif kind is LogArgType.HASH:
            args.append(LogArg(kind, number=_LOG_ARG_HASH.unpack(chunk)[0]))
        else:
            args.append(LogArg(kind, number=_LOG_ARG_LENGTH.unpack(chunk)[0]))
    return tuple(args)


_LOG_ARG_TYPES = frozenset(int(t) for t in LogArgType)

# 64-byte little-endian log cell:
#   0  u8   schema_version
#   1  u8   kind (EventKind.LOG)
#   2  u16  flags (LOG_FLAG_*)
#   4  u32  site_id           (interned call site; 0 = uninterned)
#   8  u64  request_id        (0 when the record is not request-scoped)
#  16  u32  offset_ms         (monotonic, from the worker clock epoch --
#                              the same basis as CompletionCell.end_offset_ms)
#  20  u32  dropped_siblings  (records the limiter dropped for this site since
#                              the last one it let through)
#  24  u8   severity          (OTel SeverityNumber)
#  25  u8   worker_id
#  26  u8   arg_count
#  27  u8   arg_bytes         (packed argument bytes, <= LOG_INLINE_ARG_BYTES)
#  28  u32  reserved (zero)
#  32  32B  packed arguments
_LOG = struct.Struct(BYTE_ORDER + "BBHIQIIBBBBI32s")
_require_layout(_LOG.size, CELL_SIZE, "the log cell")


@dataclass(frozen=True, slots=True)
class LogCell:
    """A decoded log record.

    `args` carries only the dynamic half of the statement. Names, template,
    module and line live in the metadata image under `site_id`; the record is
    meaningless without it, and deliberately so.
    """

    request_id: int
    site_id: int
    severity: Severity | int
    offset_ms: int = 0
    worker_id: int = 0
    args: tuple[LogArg, ...] = ()
    flags: int = 0
    dropped_siblings: int = 0

    def encode(self) -> bytes:
        packed = bytearray()
        flags = self.flags
        count = 0
        for index, arg in enumerate(self.args):
            if index >= LOG_MAX_ARGS:
                flags |= LOG_FLAG_TRUNCATED
                break
            result = _encode_log_arg(arg, LOG_INLINE_ARG_BYTES - len(packed))
            if result is None:
                flags |= LOG_FLAG_TRUNCATED
                break
            chunk, clipped = result
            packed += chunk
            count += 1
            if clipped:
                flags |= LOG_FLAG_TRUNCATED
            if arg.redacted:
                flags |= LOG_FLAG_REDACTED
        return _LOG.pack(
            SCHEMA_VERSION,
            EventKind.LOG,
            flags & 0xFFFF,
            self.site_id & 0xFFFFFFFF,
            self.request_id & 0xFFFFFFFFFFFFFFFF,
            self.offset_ms & 0xFFFFFFFF,
            self.dropped_siblings & 0xFFFFFFFF,
            int(self.severity) & 0xFF,
            self.worker_id & 0xFF,
            count & 0xFF,
            len(packed) & 0xFF,
            0,
            bytes(packed).ljust(LOG_INLINE_ARG_BYTES, b"\0"),
        )

    @classmethod
    def decode(cls, data: bytes) -> LogCell:
        if len(data) < CELL_SIZE:
            raise SchemaError(f"log cell needs {CELL_SIZE} bytes, got {len(data)}")
        (
            version,
            kind,
            flags,
            site_id,
            request_id,
            offset_ms,
            dropped_siblings,
            severity,
            worker_id,
            arg_count,
            arg_bytes,
            _reserved,
            blob,
        ) = _LOG.unpack(data[:CELL_SIZE])
        if version != SCHEMA_VERSION:
            raise SchemaError(f"unsupported schema version {version}")
        if kind != EventKind.LOG:
            raise SchemaError(f"expected log kind, got {kind}")
        if arg_bytes > LOG_INLINE_ARG_BYTES:
            raise SchemaError(
                f"log cell declares {arg_bytes} argument bytes, but a cell holds "
                f"at most {LOG_INLINE_ARG_BYTES}"
            )
        args = _decode_log_args(memoryview(blob)[:arg_bytes])
        if len(args) != arg_count:
            raise SchemaError(
                f"log cell declares {arg_count} arguments but its payload holds "
                f"{len(args)}"
            )
        return cls(
            request_id=request_id,
            site_id=site_id,
            severity=Severity(severity) if severity in _SEVERITIES else severity,
            offset_ms=offset_ms,
            worker_id=worker_id,
            args=args,
            flags=flags,
            dropped_siblings=dropped_siblings,
        )


_SEVERITIES = frozenset(int(s) for s in Severity)


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
