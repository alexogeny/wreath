"""Wreath flight-recorder replay (Stage 6/7).

Replay re-drives *Wreath-owned* behavior from a recording without claiming to
reproduce a real kernel, TLS stack, or arbitrary Python. Two guarantees, both
scoped to HTTP/1.1 in this first cut:

- **Transport replay** feeds recorded inbound byte segments, their virtual
  arrival schedule, and connection-lifecycle events (peer half-close / reset)
  into the *existing* native (or pure) HTTP/1 protocol driver over a fake
  transport, and reproduces the owned parser / framing / response-encoding
  behavior. Explicitly variable response fields (``Date``) are normalized before
  comparison. See :func:`replay_transport`.

- **Endpoint-plan replay** starts from a canonical semantic request and runs the
  owned routing, binding, validation, auth-requirement evaluation, and
  serialization. The handler may be invoked, skipped, or replaced with a recorded
  return/exception; a run that invokes arbitrary Python is labelled *best effort*,
  never deterministic. See :func:`replay_endpoint_plan`.

Both surfaces are replay/test-only: they run over fake transports and never touch
a real socket, file, or subprocess, and cannot broaden any capture policy.

A :class:`FaultSchedule` perturbs a *compatible* recording along owned seams
(short reads, truncation, mid-stream reset/half-close), keyed only to stable
owned coordinates, so owned failure handling can be exercised and asserted
deterministic.
"""

from __future__ import annotations

import asyncio
import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from ._replay_adapters import (
    AdapterFault,
    DatabaseDouble,
    FaultyHttpClient,
    ObjectStoreDouble,
    ReplayAdapters,
)
from ._replay_plan import (
    CanonicalRequest,
    PlanMode,
    PlanReplayResult,
    replay_endpoint_plan,
)
from .server import ServerConfig

__all__ = [
    "SegmentKind",
    "TransportSegment",
    "TransportRecording",
    "TransportReplayResult",
    "H2ReplayResult",
    "replay_transport_h2",
    "VirtualClock",
    "FaultKind",
    "FaultDescriptor",
    "FaultSchedule",
    "AdapterSeam",
    "AdapterFaultDescriptor",
    "fault_corpus",
    "open_recording",
    "replay_transport",
    "record_transport_segments",
    "CanonicalRequest",
    "PlanMode",
    "PlanReplayResult",
    "replay_endpoint_plan",
    "AdapterFault",
    "DatabaseDouble",
    "FaultyHttpClient",
    "ObjectStoreDouble",
    "ReplayAdapters",
    "ReplayError",
    "generate_test",
    "recorded_request",
]


class ReplayError(Exception):
    """A recording or fault schedule is malformed, or a replay could not run."""


# --- checksummed container framing (shape shared with _recording_format) -----

MAX_CHUNK_BYTES = 256 * 1024 * 1024
_CHUNK = struct.Struct("<4sII")  # tag, byte_length, crc32
_MAGIC_TRANSPORT = b"WTR1"
_MAGIC_FAULTS = b"WFS1"
_CONTAINER_VERSION = 1


def _chunk(tag: bytes, payload: bytes) -> bytes:
    if len(payload) > MAX_CHUNK_BYTES:
        raise ReplayError(f"chunk {tag!r} exceeds {MAX_CHUNK_BYTES} bytes")
    return _CHUNK.pack(tag, len(payload), zlib.crc32(payload) & 0xFFFFFFFF) + payload


def _read_chunks(data: bytes, offset: int) -> list[tuple[bytes, bytes]]:
    """Read every complete, CRC-valid chunk from ``offset``. A torn or corrupt
    tail stops iteration (recoverable), matching the WFR1 reader's contract."""
    chunks: list[tuple[bytes, bytes]] = []
    view = memoryview(data)
    while offset + _CHUNK.size <= len(view):
        tag, length, crc = _CHUNK.unpack_from(view, offset)
        start = offset + _CHUNK.size
        end = start + length
        if length > MAX_CHUNK_BYTES or end > len(view):
            break  # truncated tail
        payload = bytes(view[start:end])
        if zlib.crc32(payload) & 0xFFFFFFFF != crc:
            break  # corrupt chunk; stop, do not hide it
        chunks.append((tag, payload))
        offset = end
    return chunks


def _build_id() -> int:
    """A coarse 64-bit fingerprint of the producing build (not a security control)."""
    import platform
    import sys
    from importlib.metadata import PackageNotFoundError, version

    try:
        wreath_version = version("wreath")
    except PackageNotFoundError:
        wreath_version = "0"
    identity = f"{wreath_version}|{sys.version}|{platform.platform()}".encode()
    return zlib.crc32(identity) & 0xFFFFFFFF


def _encode_addr(addr: tuple[str, int]) -> bytes:
    host = addr[0].encode("utf-8")
    return struct.pack("<HH", len(host), int(addr[1])) + host


def _decode_addr(payload: bytes, offset: int) -> tuple[tuple[str, int], int]:
    host_len, port = struct.unpack_from("<HH", payload, offset)
    offset += 4
    host = payload[offset:offset + host_len].decode("utf-8")
    return (host, port), offset + host_len


# --- transport recording model ----------------------------------------------


class SegmentKind(IntEnum):
    """What a recorded transport segment represents on the inbound half."""

    DATA = 0  # bytes the peer sent
    EOF = 1  # peer half-closed the read side (connection_lost with no error)
    RESET = 2  # peer aborted (connection_lost with an error)


@dataclass(frozen=True, slots=True)
class TransportSegment:
    """One recorded inbound event with its virtual-clock arrival offset.

    ``offset_us`` is microseconds from the connection's virtual start; it drives
    the replay schedule and is the coordinate a virtual-clock fault trigger keys
    to. ``data`` is empty for the EOF/RESET lifecycle kinds.
    """

    offset_us: int
    kind: int = int(SegmentKind.DATA)
    data: bytes = b""


@dataclass(frozen=True, slots=True)
class TransportRecording:
    """Recorded inbound transport events for one HTTP/1 connection.

    Deliberately minimal and self-describing: the segments plus the peer/socket
    addresses the scope needs and a build fingerprint for compatibility checks.
    Serializes to a checksummed ``WTR1`` container that recovers a torn tail.
    """

    segments: tuple[TransportSegment, ...]
    peername: tuple[str, int] = ("127.0.0.1", 54321)
    sockname: tuple[str, int] = ("127.0.0.1", 8000)
    build_id: int = 0

    def to_bytes(self) -> bytes:
        head = struct.pack("<QQ", self.build_id & 0xFFFFFFFFFFFFFFFF, 0)
        head += _encode_addr(self.peername) + _encode_addr(self.sockname)
        segs = bytearray(struct.pack("<I", len(self.segments)))
        for seg in self.segments:
            segs += struct.pack("<QBI", seg.offset_us, seg.kind, len(seg.data))
            segs += seg.data
        return (
            _MAGIC_TRANSPORT
            + bytes((_CONTAINER_VERSION,))
            + _chunk(b"HEAD", head)
            + _chunk(b"SEGS", bytes(segs))
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> TransportRecording:
        if data[:4] != _MAGIC_TRANSPORT:
            raise ReplayError("not a WTR1 transport recording")
        if len(data) < 5 or data[4] != _CONTAINER_VERSION:
            raise ReplayError("unsupported WTR1 container version")
        chunks = dict(_read_chunks(data, 5))
        if b"HEAD" not in chunks or b"SEGS" not in chunks:
            raise ReplayError("transport recording is missing a required chunk")
        head = chunks[b"HEAD"]
        build_id, _reserved = struct.unpack_from("<QQ", head, 0)
        peername, off = _decode_addr(head, 16)
        sockname, _ = _decode_addr(head, off)
        segs = chunks[b"SEGS"]
        (count,) = struct.unpack_from("<I", segs, 0)
        offset = 4
        parsed: list[TransportSegment] = []
        for _ in range(count):
            arrival, kind, length = struct.unpack_from("<QBI", segs, offset)
            offset += struct.calcsize("<QBI")
            payload = segs[offset:offset + length]
            offset += length
            parsed.append(TransportSegment(arrival, kind, bytes(payload)))
        return cls(tuple(parsed), peername, sockname, build_id)


def record_transport_segments(
    chunks: list[bytes],
    *,
    peername: tuple[str, int] = ("127.0.0.1", 54321),
    sockname: tuple[str, int] = ("127.0.0.1", 8000),
    close: int | None = int(SegmentKind.EOF),
    interval_us: int = 1000,
) -> TransportRecording:
    """Build a recording from a list of inbound byte chunks.

    A convenience for producing a recording from bytes a connection received:
    each chunk becomes a DATA segment spaced ``interval_us`` apart, optionally
    followed by an EOF/RESET lifecycle segment.
    """
    segments: list[TransportSegment] = []
    at = 0
    for chunk in chunks:
        segments.append(TransportSegment(at, int(SegmentKind.DATA), bytes(chunk)))
        at += interval_us
    if close is not None:
        segments.append(TransportSegment(at, int(close), b""))
    return TransportRecording(tuple(segments), peername, sockname, _build_id())


# --- virtual clock -----------------------------------------------------------


class VirtualClock:
    """A monotonic virtual clock in microseconds, advanced explicitly by replay.

    Replay never sleeps on wall time; it advances this clock to each segment's
    offset. Fault triggers keyed to a virtual-clock instant read it here.
    """

    __slots__ = ("_now_us",)

    def __init__(self) -> None:
        self._now_us = 0

    @property
    def now_us(self) -> int:
        return self._now_us

    def advance_to(self, offset_us: int) -> None:
        if offset_us > self._now_us:
            self._now_us = offset_us


# --- fake transport ----------------------------------------------------------


class _ReplayTransport(asyncio.Transport):
    """A fake asyncio transport that captures everything the protocol writes.

    Mirrors the test harness's FakeTransport but lives in the shipped module so
    replay never depends on test code. Reads are driven externally by feeding the
    protocol; writes are accumulated for comparison.
    """

    __slots__ = ("buffer", "write_count", "closed", "aborted", "_extra", "_paused")

    def __init__(self, peername: tuple[str, int], sockname: tuple[str, int]) -> None:
        super().__init__()
        self.buffer = bytearray()
        self.write_count = 0
        self.closed = False
        self.aborted = False
        self._paused = False
        self._extra = {"sockname": sockname, "peername": peername}

    def write(self, data: Any) -> None:
        if not self.closed:
            self.buffer += bytes(data)
            self.write_count += 1

    def writelines(self, list_of_data: Any) -> None:
        if not self.closed:
            for chunk in list_of_data:
                self.buffer += bytes(chunk)
            self.write_count += 1

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.aborted = True
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        self._paused = True

    def resume_reading(self) -> None:
        self._paused = False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._extra.get(name, default)


#: A cap on event-loop pumps while draining a replay, so a pathological or hung
#: app can never make replay loop forever. Generous: real handlers settle in a
#: handful of pumps, and a request that needs more than this is itself a finding.
_MAX_PUMPS = 4096


async def _pump_after_feed(transport: _ReplayTransport) -> None:
    """Let the protocol act on the bytes just fed, cheaply.

    One pump lets scheduled callbacks run. If the protocol paused reading (its
    backpressure signal), give the app task room to drain and resume before more
    bytes arrive — a real transport would not deliver into a paused protocol.
    This keeps a byte-fragmented recording linear instead of O(pumps x segments):
    the heavy draining happens once, at the end.
    """
    await asyncio.sleep(0)
    pumps = 0
    while transport._paused and pumps < _MAX_PUMPS:
        await asyncio.sleep(0)
        pumps += 1


#: Consecutive quiet pumps that count as "the connection is staying open" — long
#: enough to sit through the lull between two pipelined responses (each response
#: is only a handful of awaits apart), so draining never stops mid-pipeline.
_QUIET_PLATEAU = 256


async def _drain(transport: _ReplayTransport) -> None:
    """Pump the loop until the connection closes, or a long quiet plateau.

    The reliable done-signal is the driver closing the transport: replay delivers
    a read-EOF after the last segment, and a keep-alive driver closes on EOF while
    a ``Connection: close`` response closes on its own. Draining to *close* — not
    to a short quiescence — is what lets pipelined and keep-alive replays reproduce
    *every* response, however long the lull between them. The quiet plateau is only
    a fallback for a driver that leaves the connection open; ``_MAX_PUMPS`` bounds
    a stuck app so replay can never hang.
    """
    stable = 0
    last = -1
    pumps = 0
    while pumps < _MAX_PUMPS:
        await asyncio.sleep(0)
        pumps += 1
        if transport.closed:
            for _ in range(4):  # flush any final queued writes
                await asyncio.sleep(0)
            return
        current = len(transport.buffer)
        if current == last and not transport._paused:
            stable += 1
            if stable >= _QUIET_PLATEAU:
                return
        else:
            stable = 0
            last = current


def _feed(protocol: Any, data: bytes) -> None:
    """Feed bytes through the BufferedProtocol zero-copy path when available
    (the native server's production ingress), else ``data_received``."""
    if not isinstance(protocol, asyncio.BufferedProtocol):
        protocol.data_received(data)
        return
    view = memoryview(data)
    while True:
        target = memoryview(protocol.get_buffer(len(view) or -1))
        n = min(len(target), len(view))
        target[:n] = view[:n]
        protocol.buffer_updated(n)
        target.release()
        view = view[n:]
        if not len(view):
            return


# --- transport replay --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransportReplayResult:
    """The owned, comparable outcome of a transport replay.

    ``response`` is the raw bytes the protocol wrote; ``normalized`` has variable
    fields (``Date``) replaced so two builds can be compared. ``terminal`` is the
    connection disposition the owned driver chose.
    """

    response: bytes
    normalized: bytes
    terminal: str  # "closed" | "aborted" | "open"
    write_count: int
    segments_fed: int

    def matches(self, other: TransportReplayResult) -> bool:
        """Whether two replays are equivalent up to normalized response bytes."""
        return self.normalized == other.normalized and self.terminal == other.terminal


@dataclass(frozen=True, slots=True)
class H2ReplayResult:
    """The owned outcome of an HTTP/2 transport replay, decoded per stream.

    HTTP/2 responses are HPACK-encoded and multiplexed, so raw bytes are not a
    stable comparison key; ``streams`` holds each stream's decoded status,
    headers, and body. ``raw`` is kept for byte-level inspection.
    """

    streams: dict[int, Any]  # stream_id -> H2StreamResponse
    raw: bytes
    terminal: str
    write_count: int
    segments_fed: int
    #: The GOAWAY error code the server sent for a connection-level protocol
    #: error, or None. GOAWAY (with close) is the owned response to a fatal
    #: framing/HPACK/protocol violation.
    goaway: int | None = None

    def _canonical(self) -> tuple[Any, ...]:
        # Drop the variable ``date`` header so two builds compare equal.
        streams: dict[int, tuple[Any, ...]] = {}
        for sid, stream in self.streams.items():
            headers = tuple((k, v) for k, v in stream.headers if k != b"date")
            streams[sid] = (stream.status, headers, stream.body, stream.reset, stream.ended)
        return (streams, self.terminal, self.goaway)

    def matches(self, other: H2ReplayResult) -> bool:
        """Whether two replays produced the same owned outcome (per-stream
        responses, terminal, and GOAWAY), ignoring the variable ``date``."""
        return self._canonical() == other._canonical()


def _normalize_response(data: bytes) -> bytes:
    """Replace variable HTTP/1 response fields so builds compare equal.

    Only ``Date`` varies for an owned HTTP/1 response (connection ids are an
    HTTP/2/3 concern). The header value is replaced with a constant token; the
    header's presence and position are preserved.
    """
    out = bytearray()
    lines = data.split(b"\r\n")
    for index, line in enumerate(lines):
        if line[:5].lower() == b"date:":
            out += b"date: <normalized>"
        else:
            out += line
        if index != len(lines) - 1:
            out += b"\r\n"
    return bytes(out)


async def _drive_connection(
    app: Any,
    recording: TransportRecording,
    protocol_cls: type,
    config: ServerConfig | None,
    faults: FaultSchedule | None,
) -> tuple[bytes, str, int, int]:
    """Feed a recording into one protocol over a fake transport and return the
    raw response bytes, terminal disposition, write count, and segments fed.

    Protocol-agnostic: HTTP/1 and HTTP/2 both drive here; only the *reading* of
    the response bytes differs, and that is the caller's job. A fault schedule
    perturbs the inbound byte stream before it reaches the parser.
    """
    loop = asyncio.get_running_loop()
    transport = _ReplayTransport(recording.peername, recording.sockname)
    registry: set[Any] = set()
    protocol = protocol_cls(app, config or ServerConfig(), loop, registry)
    protocol.connection_made(transport)

    clock = VirtualClock()
    plan = _TransportFaultPlan(faults) if faults is not None else None
    segments_fed = 0
    lost = False
    data_index = -1
    for segment in recording.segments:
        clock.advance_to(segment.offset_us)
        if segment.kind == int(SegmentKind.DATA):
            data_index += 1
            data = segment.data
            forced_close: int | None = None
            if plan is not None:
                data, forced_close = plan.rewrite(data_index, clock, data)
            if data:
                _feed(protocol, data)
                segments_fed += 1
                await _pump_after_feed(transport)
            if forced_close == _FIRE_TIMEOUT:
                # A virtual-clock timeout: let the fed bytes settle, then fire the
                # driver's owned timeout enforcement (close / abort / 408). The
                # connection stays in the loop so the owned handling is observed.
                await _drain(transport)
                _fire_timeout(protocol)
                await _drain(transport)
            elif forced_close is not None:
                # Let the bytes already fed finish processing before the close, so
                # a complete request that arrived before the close still answers
                # and an incomplete one is what the close interrupts.
                await _drain(transport)
                _deliver_close(protocol, forced_close)
                lost = True
                break
        else:
            # A recorded lifecycle event (peer half-close / reset): drain the
            # bytes received before it, then deliver it.
            await _drain(transport)
            _deliver_close(protocol, segment.kind)
            lost = True
            break
    # Drain first: let every buffered request finish and its response flush. Only
    # then, if the recording carried no lifecycle event and the driver has not
    # already closed (e.g. a keep-alive connection with no `Connection: close`),
    # deliver a clean read-EOF to model the peer closing its write side, and drain
    # the final bookkeeping. Delivering the EOF *before* draining would tear the
    # connection down mid-request and drop responses — the ordering matters.
    await _drain(transport)
    if not lost and not transport.closed:
        _deliver_close(protocol, int(SegmentKind.EOF))
        await _drain(transport)

    terminal = "aborted" if transport.aborted else ("closed" if transport.closed else "open")
    return bytes(transport.buffer), terminal, transport.write_count, segments_fed


async def replay_transport(
    app: Any,
    recording: TransportRecording,
    *,
    config: ServerConfig | None = None,
    protocol_cls: type | None = None,
    faults: FaultSchedule | None = None,
) -> TransportReplayResult:
    """Re-drive the owned HTTP/1 protocol from a transport recording.

    Feeds each recorded segment into the protocol over a fake transport,
    advancing a virtual clock to each segment's offset and pumping the loop so
    the app task can run. Returns the owned response bytes and terminal
    disposition. An optional :class:`FaultSchedule` perturbs the inbound stream
    along transport seams before it reaches the parser. For HTTP/2 use
    :func:`replay_transport_h2`, which decodes the frames it wrote back.
    """
    if protocol_cls is None:
        protocol_cls = _default_protocol_cls()
    response, terminal, write_count, segments_fed = await _drive_connection(
        app, recording, protocol_cls, config, faults
    )
    return TransportReplayResult(
        response=response,
        normalized=_normalize_response(response),
        terminal=terminal,
        write_count=write_count,
        segments_fed=segments_fed,
    )


async def replay_transport_h2(
    app: Any,
    recording: TransportRecording,
    *,
    config: ServerConfig | None = None,
    faults: FaultSchedule | None = None,
) -> H2ReplayResult:
    """Re-drive the owned HTTP/2 protocol from a transport recording.

    The recording is raw HTTP/2 wire bytes (client preface, SETTINGS, HEADERS/
    DATA frames); byte-level faults apply exactly as for HTTP/1. The frames the
    server wrote back are decoded into per-stream owned responses so two builds
    can be compared without depending on HPACK byte layout or the ``date`` value.
    """
    protocol_cls = _default_h2_protocol_cls()
    if protocol_cls is None:
        raise ReplayError("the native HTTP/2 protocol is not built")
    h2_config = config or ServerConfig(protocols=("h2",))
    response, terminal, write_count, segments_fed = await _drive_connection(
        app, recording, protocol_cls, h2_config, faults
    )
    from ._h2_codec import decode_response, goaway_error

    return H2ReplayResult(
        streams=decode_response(response),
        raw=response,
        terminal=terminal,
        write_count=write_count,
        segments_fed=segments_fed,
        goaway=goaway_error(response),
    )


def _default_h2_protocol_cls() -> type | None:
    import importlib

    try:
        native = importlib.import_module("wreath._native._server")
        return getattr(native, "Http2Protocol", None)
    except ImportError:
        return None


def _fire_timeout(protocol: Any) -> None:
    """Fire the driver's owned request/keep-alive timeout enforcement. Both the
    native and pure HTTP/1 drivers expose ``_replay_fire_timeout`` for exactly
    this; a driver without it (e.g. HTTP/2) is a no-op.

    The `callable` guard is what tolerates a driver that does not implement it.
    A driver that implements it and then *raises* is a fault, and replay exists
    to reproduce a request faithfully -- swallowing it would let a replayed run
    diverge from production without saying so, which is the one thing this
    module refuses to do elsewhere (a chunked or truncated recording is
    rejected by name rather than guessed at)."""
    fire = getattr(protocol, "_replay_fire_timeout", None)
    if callable(fire):
        fire()


def _deliver_close(protocol: Any, kind: int) -> None:
    """Deliver a peer half-close (EOF) or reset to the protocol, tolerating
    drivers that implement only a subset of the lifecycle callbacks.

    The `callable` guards are what provide that tolerance. A callback that
    exists and raises is a driver fault, and it propagates: see
    `_fire_timeout` for why replay does not swallow one."""
    exc: Exception | None = (
        ConnectionResetError("replayed peer reset") if kind == int(SegmentKind.RESET) else None
    )
    eof = getattr(protocol, "eof_received", None)
    if kind == int(SegmentKind.EOF) and callable(eof):
        eof()
    lost = getattr(protocol, "connection_lost", None)
    if callable(lost):
        lost(exc)


def _default_protocol_cls() -> type:
    """The native HTTP/1 protocol when built, else the pure twin."""
    import importlib

    try:
        native = importlib.import_module("wreath._native._server")
        return native.Http1Protocol
    except ImportError:
        from ._pure.server import Http1Protocol

        return Http1Protocol


# --- fault injection ---------------------------------------------------------


#: A `_TransportFaultPlan.rewrite` action sentinel (distinct from any SegmentKind)
#: meaning "fire the driver's owned timeout after feeding this segment".
_FIRE_TIMEOUT = 100


class FaultKind(IntEnum):
    """The owned seam perturbation a fault descriptor applies."""

    SHORT_READ = 0  # deliver only the first ``value`` bytes of the segment
    TRUNCATE = 1  # drop this segment's bytes past ``value`` and every later one
    RESET = 2  # inject a peer reset after this segment
    HALF_CLOSE = 3  # inject a peer half-close (EOF) after this segment
    CLOCK_JUMP = 4  # advance the virtual clock by ``value`` us before this segment
    DUPLICATE = 5  # feed this segment's bytes twice (peer retransmission)
    TIMEOUT = 6  # fire the driver's owned request/keep-alive timeout after this segment


@dataclass(frozen=True, slots=True)
class FaultDescriptor:
    """One perturbation keyed to a stable owned coordinate.

    ``segment_index`` is the trigger: the Nth recorded DATA segment. ``value``
    parameterizes SHORT_READ/TRUNCATE (a byte offset within the segment). No
    trigger references wall-clock time or an address, so a schedule is bit-for-bit
    reproducible.
    """

    kind: int
    segment_index: int
    value: int = 0


class AdapterSeam(IntEnum):
    """The boundary an adapter fault perturbs, and its coordinate's meaning."""

    DB_ACQUIRE = 0  # connection acquire (coordinate unused)
    DB_QUERY = 1  # the Nth query on a leased connection (coordinate = index)
    DB_RELEASE = 2  # returning the connection to the pool (coordinate unused)
    HTTP_REQUEST = 3  # the Nth outbound request (coordinate = index)
    DB_LISTEN = 4  # LISTEN, or the notification stream, on a held connection
    DB_TRANSACTION = 5  # the Nth transaction scope on a leased connection
    OBJECT_STORE = 6  # the Nth operation on a named object store (coordinate = index)


@dataclass(frozen=True, slots=True)
class AdapterFaultDescriptor:
    """One boundary-adapter fault, keyed to a stable owned coordinate: the named
    database/client and (for query/request seams) the Nth operation. ``kind`` is
    a :class:`wreath.replay.AdapterFault` value."""

    seam: int
    target: str
    kind: str
    coordinate: int = 0


def _encode_str(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def _decode_str(data: bytes, offset: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<H", data, offset)
    offset += 2
    return data[offset:offset + length].decode("utf-8"), offset + length


@dataclass(frozen=True, slots=True)
class FaultSchedule:
    """An ordered, checksummed set of faults — a first-class replay input that
    round-trips through its own ``WFS1`` container. Transport faults perturb the
    inbound byte stream; adapter faults perturb the PostgreSQL/HTTP boundaries.
    Both are keyed only to stable owned coordinates, so a schedule is bit-for-bit
    reproducible across runs and builds."""

    faults: tuple[FaultDescriptor, ...] = ()
    adapter_faults: tuple[AdapterFaultDescriptor, ...] = ()

    def to_bytes(self) -> bytes:
        body = bytearray(struct.pack("<I", len(self.faults)))
        for fault in self.faults:
            body += struct.pack("<BiI", fault.kind, fault.segment_index, fault.value)
        out = _MAGIC_FAULTS + bytes((_CONTAINER_VERSION,)) + _chunk(b"FALT", bytes(body))
        if self.adapter_faults:
            adapt = bytearray(struct.pack("<I", len(self.adapter_faults)))
            for fault in self.adapter_faults:
                adapt += struct.pack("<Bi", fault.seam, fault.coordinate)
                adapt += _encode_str(fault.target) + _encode_str(fault.kind)
            out += _chunk(b"ADPT", bytes(adapt))
        return out

    @classmethod
    def from_bytes(cls, data: bytes) -> FaultSchedule:
        if data[:4] != _MAGIC_FAULTS:
            raise ReplayError("not a WFS1 fault schedule")
        if len(data) < 5 or data[4] != _CONTAINER_VERSION:
            raise ReplayError("unsupported WFS1 container version")
        chunks = dict(_read_chunks(data, 5))
        if b"FALT" not in chunks:
            raise ReplayError("fault schedule is missing its FALT chunk")
        body = chunks[b"FALT"]
        (count,) = struct.unpack_from("<I", body, 0)
        offset = 4
        faults: list[FaultDescriptor] = []
        for _ in range(count):
            kind, index, value = struct.unpack_from("<BiI", body, offset)
            offset += struct.calcsize("<BiI")
            faults.append(FaultDescriptor(kind, index, value))
        adapter_faults: list[AdapterFaultDescriptor] = []
        adapt = chunks.get(b"ADPT")
        if adapt is not None:
            (acount,) = struct.unpack_from("<I", adapt, 0)
            offset = 4
            for _ in range(acount):
                seam, coordinate = struct.unpack_from("<Bi", adapt, offset)
                offset += struct.calcsize("<Bi")
                target, offset = _decode_str(adapt, offset)
                kind, offset = _decode_str(adapt, offset)
                adapter_faults.append(AdapterFaultDescriptor(seam, target, kind, coordinate))
        return cls(tuple(faults), tuple(adapter_faults))


class _TransportFaultPlan:
    """Applies a fault schedule to the inbound DATA segments during replay.

    Keyed to the DATA-segment index (the trigger). ``rewrite`` returns the bytes
    to actually feed and, when a lifecycle fault fires, the close kind to deliver
    after them. TRUNCATE latches so every later segment is suppressed too.
    """

    __slots__ = ("_by_index", "_truncated_from")

    def __init__(self, schedule: FaultSchedule) -> None:
        self._by_index: dict[int, FaultDescriptor] = {}
        self._truncated_from: int | None = None
        for fault in schedule.faults:
            # Last descriptor for an index wins; a schedule is explicit anyway.
            self._by_index[fault.segment_index] = fault

    def rewrite(self, index: int, clock: VirtualClock, data: bytes) -> tuple[bytes, int | None]:
        if self._truncated_from is not None and index >= self._truncated_from:
            return b"", None
        fault = self._by_index.get(index)
        if fault is None:
            return data, None
        kind = fault.kind
        if kind == int(FaultKind.SHORT_READ):
            return data[: fault.value], None
        if kind == int(FaultKind.TRUNCATE):
            self._truncated_from = index + 1
            return data[: fault.value], None
        if kind == int(FaultKind.RESET):
            return data, int(SegmentKind.RESET)
        if kind == int(FaultKind.HALF_CLOSE):
            return data, int(SegmentKind.EOF)
        if kind == int(FaultKind.CLOCK_JUMP):
            # A scheduling perturbation: jump the virtual clock before this
            # segment (keyed to an owned coordinate, so it stays reproducible).
            clock.advance_to(clock.now_us + fault.value)
            return data, None
        if kind == int(FaultKind.DUPLICATE):
            # Model a peer retransmission: feed the same bytes twice.
            return data + data, None
        if kind == int(FaultKind.TIMEOUT):
            # Fire the driver's owned request/keep-alive timeout after this
            # segment -- a real virtual-clock timeout, not an adapter outcome.
            return data, _FIRE_TIMEOUT
        return data, None


# --- curated fault corpus ----------------------------------------------------


def fault_corpus() -> dict[str, FaultSchedule]:
    """A curated set of named fault schedules, one region of the §7 taxonomy per
    entry. The corpus is a first-class artifact: a test drives every schedule and
    asserts a deterministic owned outcome, and the same set seeds the sanitizer /
    fuzz gates -- so the fault-injection library and the ASan/UBSan gate are the
    same corpus. Transport entries are keyed to segment indices (apply them to a
    recording split into a few segments); adapter entries carry adapter faults.

    **The regions, and why each is one.** An entry earns its place by naming a
    failure the owned code has to answer differently from its neighbours. A
    schedule whose outcome is "something reasonable happens" is not a region and
    does not belong; a cross product nobody can reason about is worse than a
    short list that maps to named incidents.

    ``transport-*`` / ``schedule-*``
        The inbound byte stream and the virtual clock: short reads, truncation,
        peer reset, half-close, retransmission, and the driver's own timeout.

    ``adapter-pool_*`` / ``adapter-server_error`` / ``adapter-connection_drop``
    ``adapter-lost_commit`` / ``adapter-release_error``
        The PostgreSQL pool: acquire, the Nth statement, and the release that
        has to happen anyway. ``lost_commit`` is the ambiguous one -- the write
        may or may not be durable, which is the only pool fault where retrying
        is not obviously safe.

    ``adapter-connect_error`` / ``adapter-read_timeout``
        The outbound HTTP client, beneath its own timeout and phase handling.

    ``adapter-listen_refused`` / ``adapter-notify_stream_end``
    ``adapter-notify_stream_error``
        The LISTEN/NOTIFY doorbell. ``STREAM_END`` and ``STREAM_ERROR`` are
        deliberately separate regions: ``Connection.notifications()`` *returns*
        when the connection closes rather than raising, so a supervisor written
        around ``except`` sees nothing at all. Modelling only the raising case
        would have re-blessed the bug where ephemeral fan-out stopped for the
        life of a process with no signal.

    ``adapter-begin_error`` / ``adapter-statement_timeout``
    ``adapter-commit_error``
        The transaction scope, at its three distinct moments: no work ran, work
        ran and rolled back, work ran and its durability is unknown. A caller's
        recovery turns on which, so collapsing them makes "did my write happen?"
        unanswerable.

    ``adapter-claim_lost``
        An ``INSERT ... ON CONFLICT ... RETURNING`` claim that succeeds and
        returns *no row*. Not an error, which is the entire hazard: the caller
        sees a successful statement with an empty result and carries on.

    ``adapter-object_*``
        Object storage: unreachable, a write torn part-way (the key exists and
        the bytes are wrong), and a read shorter than ``stat`` promised -- the
        last without raising, so a caller trusting the length is truncated
        silently.

    ``adapter-*-then-*``
        Compounds, where two faults *together* are the failure and either alone
        is handled: a doorbell that drops and then cannot re-``LISTEN``, a lost
        claim followed by an unknown commit, a torn write followed by a read.
    """
    from ._replay_adapters import AdapterFault

    corpus: dict[str, FaultSchedule] = {}
    # Transport seam: each kind at the head and at a mid-stream segment.
    byte_kinds = {int(FaultKind.SHORT_READ), int(FaultKind.TRUNCATE), int(FaultKind.CLOCK_JUMP)}
    for kind in FaultKind:
        for index in (0, 1):
            value = 8 if int(kind) in byte_kinds else 0
            corpus[f"transport-{kind.name.lower()}-seg{index}"] = FaultSchedule(
                (FaultDescriptor(int(kind), index, value),)
            )
    # Scheduling seam: a large clock jump plus a mid-stream reset together.
    corpus["schedule-jump-then-reset"] = FaultSchedule(
        (
            FaultDescriptor(int(FaultKind.CLOCK_JUMP), 0, 5_000_000),
            FaultDescriptor(int(FaultKind.RESET), 1),
        )
    )
    # Adapter seam: one schedule per modeled boundary failure. The `adapter-`
    # prefix is load-bearing -- the corpus test derives its parametrisation from
    # it, and the sanitizer gate re-runs the same names.
    db_acquire = {AdapterFault.POOL_TIMEOUT, AdapterFault.POOL_EXHAUSTED}
    db_release = {AdapterFault.RELEASE_ERROR}
    http = {AdapterFault.CONNECT_ERROR, AdapterFault.READ_TIMEOUT}
    listen = {
        AdapterFault.LISTEN_REFUSED,
        AdapterFault.NOTIFY_STREAM_END,
        AdapterFault.NOTIFY_STREAM_ERROR,
    }
    transaction = {
        AdapterFault.BEGIN_ERROR,
        AdapterFault.COMMIT_ERROR,
        AdapterFault.STATEMENT_TIMEOUT,
    }
    objects = {
        AdapterFault.OBJECT_UNREACHABLE,
        AdapterFault.OBJECT_WRITE_TORN,
        AdapterFault.OBJECT_READ_SHORT,
    }
    for fault in AdapterFault:
        if fault in db_acquire:
            seam, target, coord = AdapterSeam.DB_ACQUIRE, "main", 0
        elif fault in db_release:
            seam, target, coord = AdapterSeam.DB_RELEASE, "main", 0
        elif fault in http:
            seam, target, coord = AdapterSeam.HTTP_REQUEST, "api", 0
        elif fault in listen:
            seam, target, coord = AdapterSeam.DB_LISTEN, "main", 0
        elif fault in transaction:
            seam, target, coord = AdapterSeam.DB_TRANSACTION, "main", 0
        elif fault in objects:
            seam, target, coord = AdapterSeam.OBJECT_STORE, "objects", 0
        else:
            seam, target, coord = AdapterSeam.DB_QUERY, "main", 0
        corpus[f"adapter-{fault.value}"] = FaultSchedule(
            adapter_faults=(AdapterFaultDescriptor(int(seam), target, fault.value, coord),)
        )
    # Compound schedules: the regions where two faults *together* are the real
    # failure, and either alone is handled. One entry per compound, because a
    # cross product nobody can reason about is worse than a short list that maps
    # to named incidents.
    #
    # A doorbell that ends its stream and then cannot re-LISTEN: the shape of a
    # database that went away and came back refusing. A supervisor must keep
    # retrying rather than treating the failed reopen as terminal.
    corpus["adapter-doorbell-drop-then-refused-reopen"] = FaultSchedule(
        adapter_faults=(
            AdapterFaultDescriptor(
                int(AdapterSeam.DB_LISTEN), "main", AdapterFault.NOTIFY_STREAM_END.value, 0
            ),
            AdapterFaultDescriptor(
                int(AdapterSeam.DB_LISTEN), "main", AdapterFault.LISTEN_REFUSED.value, 1
            ),
        )
    )
    # A claim that comes back empty and a commit whose outcome is unknown: the
    # two halves of "did my write happen?", which is the question every
    # idempotency and job-dedup path exists to answer.
    corpus["adapter-claim-lost-then-commit-unknown"] = FaultSchedule(
        adapter_faults=(
            AdapterFaultDescriptor(
                int(AdapterSeam.DB_QUERY), "main", AdapterFault.CLAIM_LOST.value, 0
            ),
            AdapterFaultDescriptor(
                int(AdapterSeam.DB_TRANSACTION), "main", AdapterFault.COMMIT_ERROR.value, 0
            ),
        )
    )
    # A torn write followed by a read: the archive/upload failure where the
    # object exists, `exists()` says yes, and the bytes are wrong.
    corpus["adapter-object-torn-write-then-read"] = FaultSchedule(
        adapter_faults=(
            AdapterFaultDescriptor(
                int(AdapterSeam.OBJECT_STORE), "objects", AdapterFault.OBJECT_WRITE_TORN.value, 0
            ),
        )
    )
    return corpus


# --- open a recording from disk ----------------------------------------------


def open_recording(path: str) -> TransportRecording:
    """Read a recording file. Currently the ``WTR1`` transport recording; the
    reader dispatches on the container magic so future kinds slot in here."""
    with open(path, "rb") as handle:
        data = handle.read()
    magic = data[:4]
    if magic == _MAGIC_TRANSPORT:
        return TransportRecording.from_bytes(data)
    raise ReplayError(f"unrecognized recording container {magic!r}")


# --- recording -> regression test ---------------------------------------------
#
# An incident produces a recording, and a recording is only useful while someone
# is looking at it. Turning one into a test is twenty minutes of transcribing
# headers by hand, which is why it usually does not happen and the same bug comes
# back. Wreath recorded the request, owns the pipeline that served it, and ships
# the client that can drive it again, so the transcription is a function.

#: Test sources are generated, not written. Say so at the top of the file.
_GENERATED_HEADER = "# Generated by `wreath replay to-test`. Re-generate rather than edit."


def recorded_request(recording: TransportRecording) -> CanonicalRequest:
    """The first HTTP/1.1 request in ``recording``, as a canonical request.

    Reads the DATA segments in arrival order and parses one request out of them,
    so a request the peer split across several reads is reassembled exactly as
    the server saw it.

    Deliberately narrow: request line, headers, and a ``Content-Length`` body.
    Anything else -- a chunked body, a truncated tail -- raises rather than
    guessing, because a generated test that asserts a mis-decoded body is worse
    than no test at all.
    """
    raw = b"".join(
        segment.data
        for segment in recording.segments
        if segment.kind == int(SegmentKind.DATA)
    )
    if not raw.strip():
        raise ReplayError("the recording contains no request bytes")
    head, separator, rest = raw.partition(b"\r\n\r\n")
    if not separator:
        raise ReplayError("the recorded request is incomplete: no header terminator")
    lines = head.split(b"\r\n")
    request_line = lines[0].split(b" ")
    if len(request_line) < 3:
        raise ReplayError(f"the recorded request line is malformed: {lines[0]!r}")
    method = request_line[0].decode("latin-1")
    target = request_line[1].decode("latin-1")

    headers: list[tuple[bytes, bytes]] = []
    for line in lines[1:]:
        name, colon, value = line.partition(b":")
        if not colon:
            raise ReplayError(f"the recorded request has a malformed header: {line!r}")
        headers.append((name.strip().lower(), value.strip()))

    lookup = dict(headers)
    if b"chunked" in lookup.get(b"transfer-encoding", b"").lower():
        raise ReplayError(
            "the recorded request uses chunked transfer-encoding, which this "
            "generator does not decode; replay it with `wreath replay transport`"
        )
    declared = int(lookup.get(b"content-length", b"0") or 0)
    if len(rest) < declared:
        raise ReplayError(
            f"the recorded request is incomplete: {len(rest)} body bytes of "
            f"{declared} declared"
        )
    if rest[declared:].strip():
        # A keep-alive connection carrying a second request concatenates into
        # this buffer, and taking `rest[:declared]` would drop it without a
        # word -- generating a regression test that silently covers only the
        # first of two requests. Refused for the same reason a chunked body is:
        # a generated test that quietly tests less than the recording holds is
        # worse than no test at all.
        raise ReplayError(
            f"the recording holds {len(rest) - declared} bytes past the first "
            "request's body, so the connection carried more than one request; "
            "this generator emits a test for a single request. Re-record one "
            "request, or replay the whole connection with `wreath replay "
            "transport`"
        )

    path, _, query = target.partition("?")
    return CanonicalRequest(
        method=method,
        path=path,
        headers=tuple(headers),
        query_string=query.encode("latin-1"),
        body=rest[:declared],
        client=recording.peername,
        server=recording.sockname,
    )


def _test_name(request: CanonicalRequest) -> str:
    slug = "".join(
        character if character.isalnum() else "_" for character in request.path
    ).strip("_")
    return f"test_{request.method.lower()}_{slug}".rstrip("_").replace("__", "_")


async def generate_test(
    app: Any,
    recording: TransportRecording,
    *,
    target: str,
    name: str | None = None,
    origin: str | None = None,
) -> str:
    """A runnable pytest module that re-drives ``recording`` against ``app``.

    ``target`` is how the generated file should import the application --
    ``"myapp:app"``, or ``"myapp"`` for a module-level ``app``. ``origin`` is
    the recording's filename, written into the docstring so a reader six months
    later knows where the case came from.

    The request is replayed **now**, through :class:`~wreath.testing.TestClient`,
    and what comes back becomes the assertion. So this is a *characterisation*
    test: it pins what this request does today. Generated against the broken
    build it encodes the bug (watch it fail, then fix, then update); generated
    after the fix it locks the fix in. The tool cannot tell which you meant.
    """
    from .testing import TestClient

    request = recorded_request(recording)
    async with TestClient(app) as client:
        response = await client.request(
            request.method,
            request.path + (f"?{request.query_string.decode('latin-1')}"
                            if request.query_string else ""),
            headers={
                name_.decode("latin-1"): value.decode("latin-1")
                for name_, value in request.headers
                # `host` and framing headers are the test client's business;
                # carrying them over would assert on transport, not behaviour.
                if name_ not in (b"host", b"content-length", b"connection")
            },
            content=request.body,
        )

    module, _, attribute = target.partition(":")
    import_line = (
        f"from {module} import {attribute}" if attribute else f"import {module}"
    )
    application = attribute or f"{module}.app"
    headers = {
        name_.decode("latin-1"): value.decode("latin-1")
        for name_, value in request.headers
        if name_ not in (b"host", b"content-length", b"connection")
    }
    where = f" from {origin}" if origin else ""
    path_literal = request.path + (
        f"?{request.query_string.decode('latin-1')}" if request.query_string else ""
    )
    lines = [
        _GENERATED_HEADER,
        '"""A recorded request, replayed.',
        "",
        f"Captured{where} and generated by `wreath replay to-test`. It asserts",
        "what this request did when it was generated -- update the expectation",
        "deliberately, the way you would any other regression test.",
        '"""',
        "",
        "import pytest",
        "",
        "from wreath.testing import TestClient",
        "",
        import_line,
        "",
        "",
        "@pytest.mark.asyncio",
        f"async def {name or _test_name(request)}() -> None:",
        f"    async with TestClient({application}) as client:",
        "        response = await client.request(",
        f"            {request.method!r},",
        f"            {path_literal!r},",
        f"            headers={headers!r},",
    ]
    if request.body:
        lines.append(f"            content={request.body!r},")
    lines += [
        "        )",
        "",
        f"    assert response.status == {response.status}",
        f"    assert response.body == {response.body!r}",
        "",
    ]
    return "\n".join(lines)
