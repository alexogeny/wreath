"""Wreath flight-recorder replay (Stage 6/7).

Replay re-drives *Wreath-owned* behavior from a recording without claiming to
reproduce a real kernel, TLS stack, or arbitrary Python. Two guarantees, both
scoped to HTTP/1.1 in this first cut:

- **Transport replay** feeds recorded inbound byte segments, their virtual
  arrival schedule, and connection-lifecycle events (peer half-close / reset)
  into the HTTP/1 protocol driver over a fake transport, and reproduces the
  owned parser / framing / response-encoding
  behavior. Explicitly variable response fields (`Date`) are normalized before
  comparison. See `replay_transport`.

- **Endpoint-plan replay** starts from a canonical semantic request and runs the
  owned routing, binding, validation, auth-requirement evaluation, and
  serialization. The handler may be invoked, skipped, or replaced with a recorded
  return/exception; a run that invokes arbitrary Python is labelled *best effort*,
  never deterministic. See `replay_endpoint_plan`.

Both surfaces are replay/test-only: they run over fake transports and never touch
a real socket, file, or subprocess, and cannot broaden any capture policy.

A `FaultSchedule` perturbs a *compatible* recording along owned seams
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
from typing import Any, cast

from ._native import _server
from ._recording_format import AttemptRecord, _build_id
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
    "RingReproduction",
    "reproduce_from_ring",
    "AttemptRecord",
    "KIND_ATTEMPT",
    "KIND_TRANSPORT",
    "recording_kind",
    "open_attempt_recording",
    "AttemptReplayError",
    "AttemptReplayResult",
    "attempt_adapters",
    "attempt_fault_schedule",
    "replay_attempt",
    "generate_attempt_test",
]


class ReplayError(Exception):
    """A recording or fault schedule is malformed, or a replay could not run."""


# --- checksummed container framing (shape shared with _recording_format) -----

MAX_CHUNK_BYTES = 256 * 1024 * 1024
_CHUNK = struct.Struct("<4sII")  # tag, byte_length, crc32
_MAGIC_TRANSPORT = b"WTR1"
_MAGIC_FAULTS = b"WFS1"
#: The flight recorder's container, which carries the job-attempt record kind.
#: Spelled here rather than imported so `open_recording` can dispatch on four
#: bytes without pulling the recording format in for a `WTR1` file.
_MAGIC_RECORDING = b"WFR1"
#: Every chunk tag a `WFS1` schedule may carry. See `_chunk_map`'s
#: `known`: a tag is not checksummed, so an open vocabulary would let one
#: flipped bit drop the optional `ADPT` chunk without a word.
_FAULT_CHUNKS = frozenset({b"FALT", b"ADPT"})
_CONTAINER_VERSION = 1


def _chunk(tag: bytes, payload: bytes) -> bytes:
    if len(payload) > MAX_CHUNK_BYTES:
        raise ReplayError(f"chunk {tag!r} exceeds {MAX_CHUNK_BYTES} bytes")
    return _CHUNK.pack(tag, len(payload), zlib.crc32(payload) & 0xFFFFFFFF) + payload


def _read_chunks(data: bytes, offset: int) -> list[tuple[bytes, bytes]]:
    """Read every complete, CRC-valid chunk from `offset`. A torn or corrupt
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


def _chunk_map(
    data: bytes,
    offset: int,
    *,
    container: str,
    recover_tail: bool,
    known: frozenset[bytes] | None = None,
) -> dict[bytes, bytes]:
    """Every complete chunk by tag, refusing a tag that appears twice.

    A **repeat** of a tag already read is refused by name. It is not a new
    field, it is a second copy of one, and `dict()` silently kept the last.
    Appending one more `SEGS` chunk to a valid recording would then replace
    every segment in it while the file still verified -- same magic, same
    version, every CRC good -- so a replay would run bytes nobody recorded and
    say nothing. Refused for the same reason `recorded_request` refuses a
    chunked body: a container that quietly holds something other than what it
    appears to is worse than one that will not open.

    `recover_tail` is where the two containers deliberately differ, and the
    difference is what each one is *for*:

    - A **recording** recovers. The writer appends, so a capture cut short by a
      crash ends mid-chunk, and every complete chunk before the tear is the
      forensic material the incident produced. Throwing it away to punish the
      tear helps nobody.
    - A **fault schedule** refuses. Its whole promise is that two runs got the
      same injection, and a torn `ADPT` chunk would drop every adapter fault
      while the transport half still decoded -- a schedule that injects less
      than it says it does, in a run that stays green. A weaker schedule
      running under the name of a stronger one is the failure this refusal
      exists to prevent, and it is not a tear anybody can act on: schedules are
      generated, not salvaged.

    `known`, when given, is the complete set of tags the container may hold;
    anything else is refused by name. **The CRC covers the payload, not the
    tag**, so a single flipped bit in a tag turns a chunk the reader needs into
    one it has never heard of -- and an *optional* chunk that goes unrecognised
    simply vanishes while every checksum still verifies. Measured: flipping one
    bit of the `P` in `ADPT` left a chunk whose CRC still checked out, and a
    schedule carrying an adapter fault decoded cleanly as one carrying none --
    the same injection, silently weaker. A recording has no optional
    chunks, so it stays open to a future writer's extra ones; a schedule has
    `ADPT`, so it names its vocabulary and the version byte carries forward
    compatibility instead.
    """
    seen: dict[bytes, bytes] = {}
    consumed = offset
    for tag, payload in _read_chunks(data, offset):
        if known is not None and tag not in known:
            raise ReplayError(
                f"{container} container holds an unrecognised "
                f"{tag.decode('latin-1', 'replace')!r} chunk. A chunk tag is "
                "not covered by the CRC, so one flipped bit turns a chunk this "
                "reader needs into one it silently ignores; the vocabulary is "
                "fixed and the version byte is how it grows"
            )
        if tag in seen:
            raise ReplayError(
                f"{container} container repeats the {tag.decode('latin-1')!r} "
                "chunk; a second copy of a chunk would silently replace the "
                "first, so the container is refused rather than guessed at"
            )
        seen[tag] = payload
        consumed += _CHUNK.size + len(payload)
    if not recover_tail and consumed != len(data):
        raise ReplayError(
            f"{container} container has {len(data) - consumed} trailing bytes "
            "that are not a complete chunk, so part of it was lost or altered; "
            "a fault schedule is refused rather than recovered, because a "
            "schedule missing half its faults injects less than it claims and "
            "the run that used it still passes"
        )
    return seen


def _encode_addr(addr: tuple[str, int]) -> bytes:
    host = addr[0].encode("utf-8")
    return struct.pack("<HH", len(host), int(addr[1])) + host


def _decode_addr(payload: bytes, offset: int) -> tuple[tuple[str, int], int]:
    host_len, port = struct.unpack_from("<HH", payload, offset)
    offset += 4
    host = payload[offset : offset + host_len].decode("utf-8")
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

    `offset_us` is microseconds from the connection's virtual start; it drives
    the replay schedule and is the coordinate a virtual-clock fault trigger keys
    to. `data` is empty for the EOF/RESET lifecycle kinds.
    """

    offset_us: int
    kind: int = int(SegmentKind.DATA)
    data: bytes = b""


@dataclass(frozen=True, slots=True)
class TransportRecording:
    """Recorded inbound transport events for one HTTP/1 connection.

    Deliberately minimal and self-describing: the segments plus the peer/socket
    addresses the scope needs and a build fingerprint for compatibility checks.
    Serializes to a checksummed `WTR1` container that recovers a torn tail.
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
        chunks = _chunk_map(data, 5, container="WTR1", recover_tail=True)
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
            payload = segs[offset : offset + length]
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
    each chunk becomes a DATA segment spaced `interval_us` apart, optionally
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
    a `Connection: close` response closes on its own. Draining to *close* — not
    to a short quiescence — is what lets pipelined and keep-alive replays reproduce
    *every* response, however long the lull between them. The quiet plateau is only
    a fallback for a driver that leaves the connection open; `_MAX_PUMPS` bounds
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
    (the native server's production ingress), else `data_received`."""
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

    `response` is the raw bytes the protocol wrote; `normalized` has variable
    fields (`Date`) replaced so two builds can be compared. `terminal` is the
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
    stable comparison key; `streams` holds each stream's decoded status,
    headers, and body. `raw` is kept for byte-level inspection.
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
        # Drop the variable `date` header so two builds compare equal.
        streams: dict[int, tuple[Any, ...]] = {}
        for sid, stream in self.streams.items():
            headers = tuple((k, v) for k, v in stream.headers if k != b"date")
            streams[sid] = (stream.status, headers, stream.body, stream.reset, stream.ended)
        return (streams, self.terminal, self.goaway)

    def matches(self, other: H2ReplayResult) -> bool:
        """Whether two replays produced the same owned outcome (per-stream
        responses, terminal, and GOAWAY), ignoring the variable `date`."""
        return self._canonical() == other._canonical()


def _normalize_response(data: bytes) -> bytes:
    """Replace variable HTTP/1 response fields so builds compare equal.

    Only `Date` varies for an owned HTTP/1 response (connection ids are an
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
    recorder: object | None = None,
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
    arguments = (app, config or ServerConfig(), loop, registry)
    protocol = (
        protocol_cls(*arguments)
        if recorder is None
        else protocol_cls(*arguments, recorder=recorder)
    )
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
            reads: tuple[bytes, ...] = (segment.data,)
            forced_close: int | None = None
            if plan is not None:
                reads, forced_close = plan.rewrite(data_index, clock, segment.data)
            fed_any = False
            for read in reads:
                if not read:
                    continue
                _feed(protocol, read)
                fed_any = True
                await _pump_after_feed(transport)
            if fed_any:
                # One recorded segment, one count, however many reads it became:
                # the counter names what the *recording* delivered, so a SPLIT
                # schedule stays comparable with the unfaulted replay it must equal.
                segments_fed += 1
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
    recorder: object | None = None,
) -> TransportReplayResult:
    """Re-drive the owned HTTP/1 protocol from a transport recording.

    Feeds each recorded segment into the protocol over a fake transport,
    advancing a virtual clock to each segment's offset and pumping the loop so
    the app task can run. Returns the owned response bytes and terminal
    disposition. An optional `FaultSchedule` perturbs the inbound stream
    along transport seams before it reaches the parser. For HTTP/2 use
    `replay_transport_h2`, which decodes the frames it wrote back.
    A supplied Flight `recorder` receives the same completion and phase
    cells as this protocol would emit under a live server.
    """
    if protocol_cls is None:
        protocol_cls = _default_protocol_cls()
    response, terminal, write_count, segments_fed = await _drive_connection(
        app, recording, protocol_cls, config, faults, recorder
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
    recorder: object | None = None,
) -> H2ReplayResult:
    """Re-drive the owned HTTP/2 protocol from a transport recording.

    The recording is raw HTTP/2 wire bytes (client preface, SETTINGS, HEADERS/
    DATA frames); byte-level faults apply exactly as for HTTP/1. The frames the
    server wrote back are decoded into per-stream owned responses so two builds
    can be compared without depending on HPACK byte layout or the `date` value.
    A supplied Flight `recorder` is passed to the protocol unchanged.
    """
    protocol_cls = _default_h2_protocol_cls()
    h2_config = config or ServerConfig(protocols=("h2",))
    response, terminal, write_count, segments_fed = await _drive_connection(
        app, recording, protocol_cls, h2_config, faults, recorder
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


def _default_h2_protocol_cls() -> type:
    return cast(type, _server.Http2Protocol)


def _fire_timeout(protocol: Any) -> None:
    """Fire the driver's owned request/keep-alive timeout enforcement. Both the
    the HTTP/1 driver exposes `_replay_fire_timeout` for exactly
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
    """The compiled HTTP/1 protocol.

    Replay drives the *shipped* driver: a recording replayed through anything
    else reports the timings and the framing decisions of that other thing, and
    the whole point of a recording is that it is what actually happened.

    """
    return cast(type, _server.Http1Protocol)


# --- fault injection ---------------------------------------------------------


#: A `_TransportFaultPlan.rewrite` action sentinel (distinct from any SegmentKind)
#: meaning "fire the driver's owned timeout after feeding this segment".
_FIRE_TIMEOUT = 100


class FaultKind(IntEnum):
    """The owned seam perturbation a fault descriptor applies."""

    SHORT_READ = 0  # deliver only the first `value` bytes of the segment
    TRUNCATE = 1  # drop this segment's bytes past `value` and every later one
    RESET = 2  # inject a peer reset after this segment
    HALF_CLOSE = 3  # inject a peer half-close (EOF) after this segment
    CLOCK_JUMP = 4  # advance the virtual clock by `value` us before this segment
    DUPLICATE = 5  # feed this segment's bytes twice (peer retransmission)
    TIMEOUT = 6  # fire the driver's owned request/keep-alive timeout after this segment
    SPLIT = 7  # deliver this segment as two reads, split at `value`


@dataclass(frozen=True, slots=True)
class FaultDescriptor:
    """One perturbation keyed to a stable owned coordinate.

    `segment_index` is the trigger: the Nth recorded DATA segment. `value`
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
    DB_CONNECTION = 7  # the lease itself fails at the Nth operation, and latches


@dataclass(frozen=True, slots=True)
class AdapterFaultDescriptor:
    """One boundary-adapter fault, keyed to a stable owned coordinate: the named
    database/client and (for query/request seams) the Nth operation. `kind` is
    a `wreath.replay.AdapterFault` value."""

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
    return data[offset : offset + length].decode("utf-8"), offset + length


@dataclass(frozen=True, slots=True)
class FaultSchedule:
    """An ordered, checksummed set of faults — a first-class replay input that
    round-trips through its own `WFS1` container. Transport faults perturb the
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
        chunks = _chunk_map(data, 5, container="WFS1", recover_tail=False, known=_FAULT_CHUNKS)
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

    Keyed to the DATA-segment index (the trigger). `rewrite` returns the reads
    to actually feed -- one per `data_received` the protocol will see -- and,
    when a lifecycle fault fires, the close kind to deliver after them. TRUNCATE
    latches so every later segment is suppressed too.

    A *tuple* of reads rather than one buffer, because a read boundary is
    itself a seam: SPLIT delivers one recorded segment as two reads so an
    incremental parser is made to resume mid-frame, which is where an
    incremental parser is wrong if it is wrong anywhere.
    """

    __slots__ = ("_by_index", "_truncated_from")

    def __init__(self, schedule: FaultSchedule) -> None:
        self._by_index: dict[int, FaultDescriptor] = {}
        self._truncated_from: int | None = None
        for fault in schedule.faults:
            # Last descriptor for an index wins; a schedule is explicit anyway.
            self._by_index[fault.segment_index] = fault

    def rewrite(
        self, index: int, clock: VirtualClock, data: bytes
    ) -> tuple[tuple[bytes, ...], int | None]:
        if self._truncated_from is not None and index >= self._truncated_from:
            return (), None
        fault = self._by_index.get(index)
        if fault is None:
            return (data,), None
        kind = fault.kind
        if kind == int(FaultKind.SHORT_READ):
            return (data[: fault.value],), None
        if kind == int(FaultKind.TRUNCATE):
            self._truncated_from = index + 1
            return (data[: fault.value],), None
        if kind == int(FaultKind.RESET):
            return (data,), int(SegmentKind.RESET)
        if kind == int(FaultKind.HALF_CLOSE):
            return (data,), int(SegmentKind.EOF)
        if kind == int(FaultKind.CLOCK_JUMP):
            # A scheduling perturbation: jump the virtual clock before this
            # segment (keyed to an owned coordinate, so it stays reproducible).
            clock.advance_to(clock.now_us + fault.value)
            return (data,), None
        if kind == int(FaultKind.DUPLICATE):
            # Model a peer retransmission: feed the same bytes twice. One read,
            # not two, so this stays bit-for-bit what it was before reads became
            # a tuple -- the retransmission is the subject here, not the boundary.
            return (data + data,), None
        if kind == int(FaultKind.TIMEOUT):
            # Fire the driver's owned request/keep-alive timeout after this
            # segment -- a real virtual-clock timeout, not an adapter outcome.
            return (data,), _FIRE_TIMEOUT
        if kind == int(FaultKind.SPLIT):
            # The same bytes, across a read boundary. Nothing is lost, so the
            # owned outcome must be *identical* to the unfaulted replay -- the
            # only fault in the corpus whose assertion is equality rather than
            # degradation.
            cut = max(0, min(fault.value, len(data)))
            if cut == 0 or cut == len(data):
                return (data,), None
            return (data[:cut], data[cut:]), None
        return (data,), None


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

    `transport-*` / `schedule-*`
        The inbound byte stream and the virtual clock: short reads, truncation,
        peer reset, half-close, retransmission, and the driver's own timeout.

    `transport-split-*`
        The odd one out, and deliberately so. Every other transport region
        removes or reorders bytes, and its assertion is that the degradation is
        handled. `SPLIT` removes nothing -- it moves the *read boundary* into
        the middle of a frame -- so its assertion is **equality** with the
        unfaulted replay. That is the property an incremental parser breaks
        first, and no "handled it gracefully" outcome can hide a violation of it.

    `adapter-pool_*` / `adapter-server_error` / `adapter-connection_drop`
    `adapter-lost_commit` / `adapter-release_error`
        The PostgreSQL pool: acquire, the Nth statement, and the release that
        has to happen anyway. `lost_commit` is the ambiguous one -- the write
        may or may not be durable, which is the only pool fault where retrying
        is not obviously safe.

    `adapter-connect_error` / `adapter-read_timeout`
        The outbound HTTP client, beneath its own timeout and phase handling.

    `adapter-listen_refused` / `adapter-notify_stream_end`
    `adapter-notify_stream_error`
        The LISTEN/NOTIFY doorbell. `STREAM_END` and `STREAM_ERROR` are
        deliberately separate regions: `Connection.notifications()` *returns*
        when the connection closes rather than raising, so a supervisor written
        around `except` sees nothing at all. Modelling only the raising case
        would have re-blessed the bug where ephemeral fan-out stopped for the
        life of a process with no signal.

    `adapter-begin_error` / `adapter-statement_timeout`
    `adapter-commit_error`
        The transaction scope, at its three distinct moments: no work ran, work
        ran and rolled back, work ran and its durability is unknown. A caller's
        recovery turns on which, so collapsing them makes "did my write happen?"
        unanswerable.

    `adapter-claim_lost`
        An `INSERT ... ON CONFLICT ... RETURNING` claim that succeeds and
        returns *no row*. Not an error, which is the entire hazard: the caller
        sees a successful statement with an empty result and carries on.

    `adapter-connection_failed`
        The *lease* ends, not one statement on it. Separate from
        `connection_drop` because the two want opposite recoveries: a dropped
        statement may be retried on the connection already held, and this one
        never may -- it latches, so every later operation raises the identical
        error. A caller that cannot tell them apart spends its whole attempt
        budget re-issuing into a connection that is gone.

    `adapter-decode_error`
        The statement succeeded and the *answer* could not be read. Modelled as
        a `ValueError`, not a `PostgresError`, because that is what it was:
        `text-format array decoding is not supported`, raised by the driver on
        a cold catalog path in a default configuration. Every
        `except PostgresError` in this tree steps around it, so a region that raised a
        server error would have proved a recovery that does not exist. This is
        also the region that pairs with the no-hang property: the failure is in
        the code that *resolves* a caller's future, which is exactly how a
        printed error and a permanent wait can coexist.

    `adapter-prepared_poison`
        Works once, fails forever after. PostgreSQL infers a parameter's type on
        the first execution and the prepared statement carries the inference, so
        the second execution binds by an OID nothing can encode. It is the only
        region whose failure does not exist on the first call, which makes it
        the only one a smoke test that runs each statement once cannot see --
        and reconnecting does not clear it, so "it worked when I tried it" is
        true and useless.

    `adapter-object_*`
        Object storage: unreachable, a write torn part-way (the key exists and
        the bytes are wrong), and a read shorter than `stat` promised -- the
        last without raising, so a caller trusting the length is truncated
        silently.

    `adapter-*-then-*`
        Compounds, where two faults *together* are the failure and either alone
        is handled: a doorbell that drops and then cannot re-`LISTEN`, a lost
        claim followed by an unknown commit, a torn write followed by a read.
    """
    from ._replay_adapters import AdapterFault

    corpus: dict[str, FaultSchedule] = {}
    # Transport seam: each kind at the head and at a mid-stream segment.
    byte_kinds = {
        int(FaultKind.SHORT_READ),
        int(FaultKind.TRUNCATE),
        int(FaultKind.CLOCK_JUMP),
        int(FaultKind.SPLIT),
    }
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
    connection = {AdapterFault.CONNECTION_FAILED}
    for fault in AdapterFault:
        if fault in connection:
            seam, target, coord = AdapterSeam.DB_CONNECTION, "main", 0
        elif fault in db_acquire:
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


#: What `recording_kind` reports, and the two things a recording can be.
KIND_TRANSPORT = "transport"
KIND_ATTEMPT = "attempt"


def recording_kind(path: str) -> str:
    """Whether a file is a `WTR1` transport recording or a `WFR1` job attempt.

    Four bytes decide it, so a command that accepts both -- `wreath replay
    to-test` -- never has to be told which it was handed. An attempt is a
    *record kind* inside the flight recorder's own container rather than a
    second format, which is why this reads a magic and not an extension.
    """
    with open(path, "rb") as handle:
        magic = handle.read(4)
    if magic == _MAGIC_TRANSPORT:
        return KIND_TRANSPORT
    if magic == _MAGIC_RECORDING:
        return KIND_ATTEMPT
    raise ReplayError(f"unrecognized recording container {magic!r}")


def open_recording(path: str) -> TransportRecording:
    """Read a `WTR1` transport recording -- one connection's inbound bytes.

    A `WFR1` file is refused *by name* rather than silently mis-parsed: it is
    the flight recorder's container, and `open_attempt_recording` is what reads
    the job attempt inside it.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    magic = data[:4]
    if magic == _MAGIC_TRANSPORT:
        return TransportRecording.from_bytes(data)
    if magic == _MAGIC_RECORDING:
        raise ReplayError(
            f"{path} is a WFR1 flight recording, not a WTR1 transport recording. "
            "If it holds a job attempt, `wreath replay to-test` reads it; "
            "`wreath replay transport` replays a connection's bytes and this "
            "file has none"
        )
    raise ReplayError(f"unrecognized recording container {magic!r}")


def open_attempt_recording(path: str) -> AttemptRecord:
    """Read the one job-attempt record in a `WFR1` file.

    A `WTR1` file is refused by name, for the same reason `open_recording`
    refuses a `WFR1`: the two carry different things and guessing is worse than
    saying so.
    """
    from ._recording_format import read_attempt_recording

    with open(path, "rb") as handle:
        data = handle.read()
    magic = data[:4]
    if magic == _MAGIC_RECORDING:
        return read_attempt_recording(data)
    if magic == _MAGIC_TRANSPORT:
        raise ReplayError(
            f"{path} is a WTR1 transport recording of a connection, not a job "
            "attempt. `wreath replay transport` replays it"
        )
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
    """The first HTTP/1.1 request in `recording`, as a canonical request.

    Reads the DATA segments in arrival order and parses one request out of them,
    so a request the peer split across several reads is reassembled exactly as
    the server saw it.

    Deliberately narrow: request line, headers, and a `Content-Length` body.
    Anything else -- a chunked body, a truncated tail -- raises rather than
    guessing, because a generated test that asserts a mis-decoded body is worse
    than no test at all.
    """
    raw = b"".join(
        segment.data for segment in recording.segments if segment.kind == int(SegmentKind.DATA)
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
            f"the recorded request is incomplete: {len(rest)} body bytes of {declared} declared"
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
    slug = "".join(character if character.isalnum() else "_" for character in request.path).strip(
        "_"
    )
    return f"test_{request.method.lower()}_{slug}".rstrip("_").replace("__", "_")


@dataclass(frozen=True, slots=True)
class RingReproduction:
    """Whether re-driving a recording retraces the path a crash file recorded.

    A ring file names the request that was in flight when a process died, and
    the log records it had emitted by then. It does not hold the request's
    bytes -- a completion cell carries a route and a status, never a payload --
    so reproducing the crash needs a transport recording of that request from
    somewhere else. What joins the two is the *sequence of log call sites*: if
    replaying the recording emits the same sites in the same order, the replay
    went where the dead process went.

    Attributes:
        request_id: The request from the ring file this was checked against.
        expected: Its call sites, in the order the ring published them.
        observed: The call sites the replay emitted.
        diverged_at: Index of the first site that differs, or None when the
            replay retraced the whole recorded prefix.
        reproduced: True when `observed` begins with `expected` -- the replay
            got at least as far as the dead process did. A replay that goes
            *further* still reproduces: the crash file stops where the process
            stopped, not where the request would have.
        result: The transport replay's own outcome, for the response bytes.
    """

    request_id: int
    expected: tuple[int, ...]
    observed: tuple[int, ...]
    diverged_at: int | None
    reproduced: bool
    result: TransportReplayResult


async def reproduce_from_ring(
    app: Any,
    ring: Any,
    recording: TransportRecording,
    *,
    request_id: int | None = None,
    config: ServerConfig | None = None,
) -> RingReproduction:
    """Replay `recording` and check it retraces the crash file's last request.

    Args:
        app: The application to drive. It must be the build that crashed --
            see the warning below.
        ring: A `DecodedRing` from `wreath.recording.read_ring_file`.
        recording: The transport recording believed to hold that request.
        request_id: Which request from the ring to check against. Defaults to
            the one that was in flight, and raises when the ring shows no such
            request or shows several, because guessing which crash to reproduce
            is the wrong kind of helpful.
        config: Passed through to `replay_transport`.

    Raises:
        ReplayError: If the ring names no in-flight request, names more than one
            and the caller did not choose, or the chosen request has no records.

    **This compares call-site ids, so it is only meaningful against the build
    that produced the file.** A site's id is its position in the process's
    interned table, which is import order -- stable for one build and entirely
    unrelated across two. Replaying against a different build does not fail
    loudly here; it produces a divergence at index 0 that means nothing. Run it
    against the build that crashed.
    """
    from . import logging as wreath_logging

    if request_id is None:
        candidates = ring.in_flight()
        if not candidates:
            raise ReplayError(
                "the ring file shows no request in flight: every request that "
                "logged also completed, so there is no crash here to reproduce"
            )
        if len(candidates) > 1:
            raise ReplayError(
                f"the ring file shows {len(candidates)} requests in flight "
                f"({', '.join(str(c) for c in candidates)}); name one with "
                "request_id rather than having this pick"
            )
        request_id = candidates[0]

    expected = tuple(record.decode().site_id for record in ring.logs_for(request_id))
    if not expected:
        raise ReplayError(
            f"request {request_id} has no log records in the ring file, so there "
            "is no path to compare a replay against"
        )

    # Capture rather than publish: the replay must not need a recorder, and its
    # records must not reach whatever this process has installed.
    with wreath_logging.testing_runtime(level=wreath_logging.TRACE) as captured:
        result = await replay_transport(app, recording, config=config)
    observed = tuple(cell.site_id for cell in captured)

    diverged_at: int | None = None
    for index, site in enumerate(expected):
        if index >= len(observed) or observed[index] != site:
            diverged_at = index
            break
    return RingReproduction(
        request_id=request_id,
        expected=expected,
        observed=observed,
        diverged_at=diverged_at,
        reproduced=diverged_at is None,
        result=result,
    )


async def generate_test(
    app: Any,
    recording: TransportRecording,
    *,
    target: str,
    name: str | None = None,
    origin: str | None = None,
) -> str:
    """A runnable pytest module that re-drives `recording` against `app`.

    `target` is how the generated file should import the application --
    `"myapp:app"`, or `"myapp"` for a module-level `app`. `origin` is
    the recording's filename, written into the docstring so a reader six months
    later knows where the case came from.

    The request is replayed **now**, through `wreath.testing.TestClient`,
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
            request.path
            + (f"?{request.query_string.decode('latin-1')}" if request.query_string else ""),
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
    import_line = f"from {module} import {attribute}" if attribute else f"import {module}"
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


# --- replaying a durable job attempt ------------------------------------------
#
# A failed durable job is harder to reproduce than a failed request: the request
# that caused it succeeded hours ago, the arguments came from state that has
# since changed, and the failure is on attempt 4 after two retries and a lease
# expiry. Wreath owns the queue, the retry policy, the driver, and the doubles,
# so it can re-run *that attempt* with every boundary it crossed replaced.
#
# The join between recording and replay is the coordinate space. A recorded
# `BoundaryEvent` is `(seam, target, coordinate)` -- which is exactly what an
# `AdapterFaultDescriptor` addresses -- so a recorded failure becomes an
# injected fault without anything in between having to keep a payload.


class AttemptReplayError(ReplayError):
    """An attempt recording cannot be replayed against this build."""


#: Recorded exception type name -> the fault that reproduces it, per seam family.
#: Each entry is the inverse of `_replay_adapters._db_error`/`_object_error`,
#: which are the only places a fault's exception is constructed.
_DB_FAULT_FOR_ERROR: dict[str, AdapterFault] = {
    "PostgresError": AdapterFault.SERVER_ERROR,
    "OperationalError": AdapterFault.CONNECTION_DROP,
    "InterfaceError": AdapterFault.POOL_EXHAUSTED,
    "TimeoutError": AdapterFault.POOL_TIMEOUT,
    "ValueError": AdapterFault.DECODE_ERROR,
    "TypeError": AdapterFault.PREPARED_POISON,
}
_HTTP_FAULT_FOR_ERROR: dict[str, AdapterFault] = {
    "ConnectionError": AdapterFault.CONNECT_ERROR,
    "TimeoutError": AdapterFault.READ_TIMEOUT,
}
_OBJECT_FAULT_FOR_ERROR: dict[str, AdapterFault] = {
    "ObjectError": AdapterFault.OBJECT_UNREACHABLE,
}
_FAULT_TABLES: dict[int, dict[str, AdapterFault]] = {
    int(AdapterSeam.DB_ACQUIRE): _DB_FAULT_FOR_ERROR,
    int(AdapterSeam.DB_QUERY): _DB_FAULT_FOR_ERROR,
    int(AdapterSeam.DB_RELEASE): _DB_FAULT_FOR_ERROR,
    int(AdapterSeam.DB_TRANSACTION): _DB_FAULT_FOR_ERROR,
    int(AdapterSeam.HTTP_REQUEST): _HTTP_FAULT_FOR_ERROR,
    int(AdapterSeam.OBJECT_STORE): _OBJECT_FAULT_FOR_ERROR,
}
#: Which seams live on a database double rather than a client or a store.
_DB_SEAMS = frozenset(
    {
        int(AdapterSeam.DB_ACQUIRE),
        int(AdapterSeam.DB_QUERY),
        int(AdapterSeam.DB_RELEASE),
        int(AdapterSeam.DB_LISTEN),
        int(AdapterSeam.DB_TRANSACTION),
        int(AdapterSeam.DB_CONNECTION),
    }
)


def attempt_fault_schedule(record: Any) -> FaultSchedule:
    """The fault schedule that reproduces a recorded attempt's boundary failures.

    A boundary the attempt crossed *successfully* contributes no fault -- the
    double answers with its empty default -- and one that raised contributes the
    fault whose modelled exception is the one the recording names.

    An error type with no fault that produces it is **refused by name** rather
    than approximated. Injecting the nearest fault instead would replay a
    different failure while reporting that it had reproduced the recorded one,
    which is the whole thing an attempt replay exists to avoid.
    """
    faults: list[AdapterFaultDescriptor] = []
    for event in record.boundaries:
        if not event.error_type:
            continue
        table = _FAULT_TABLES.get(event.seam)
        fault = None if table is None else table.get(event.error_type)
        if fault is None:
            raise AttemptReplayError(
                f"the recorded attempt failed at seam {event.seam} on "
                f"{event.target!r} with {event.error_type}, and no modelled fault "
                "produces that exception. Replaying it would inject a different "
                "failure and report a reproduction; add the mapping rather than "
                "guessing at the nearest one"
            )
        faults.append(
            AdapterFaultDescriptor(
                seam=event.seam,
                target=event.target,
                kind=str(fault),
                coordinate=event.coordinate,
            )
        )
    return FaultSchedule(adapter_faults=tuple(faults))


def attempt_adapters(record: Any, *, databases: tuple[str, ...] = ()) -> ReplayAdapters:
    """Doubles for every boundary a recorded attempt touched, plus `databases`.

    `databases` names boundaries that must be doubled whether or not the
    recording mentions them -- the queue's own database is always one, because
    an attempt that never queried it still runs on a runner that would.
    """
    adapters = ReplayAdapters.from_faults(attempt_fault_schedule(record).adapter_faults)
    for event in record.boundaries:
        if event.seam in _DB_SEAMS:
            adapters.databases.setdefault(event.target, DatabaseDouble(event.target))
        elif event.seam == int(AdapterSeam.HTTP_REQUEST):
            adapters.clients.setdefault(event.target, FaultyHttpClient(event.target))
        elif event.seam == int(AdapterSeam.OBJECT_STORE):
            adapters.object_stores.setdefault(event.target, ObjectStoreDouble(event.target))
        else:
            # A seam this build has no double for. Refused rather than skipped:
            # a boundary with no double is one the replay reaches for real, and
            # the crossing that *succeeded* is the one that would slip through
            # -- `attempt_fault_schedule` above only sees the ones that raised.
            raise AttemptReplayError(
                f"the recording crosses seam {event.seam} on {event.target!r}, "
                "which this build has no boundary double for. A replay would "
                "reach the real resource there; the recording is from a newer "
                "build than this one"
            )
    for name in databases:
        adapters.databases.setdefault(name, DatabaseDouble(name))
    return adapters


@dataclass(frozen=True, slots=True)
class AttemptReplayResult:
    """What re-running a recorded job attempt produced.

    Attributes:
        outcome: A `wreath.recording.AttemptOutcome` value for *this* run.
        error_type: The exception class name this run raised, or `""`.
        error_message: That exception's message, or `""`.
        matched: Whether this run reproduced the recorded outcome *and* error
            type. False is a finding, not a failure: the handler changed, or the
            failure depended on something no double models.
        adapters: The doubles the replay ran against, so a test can assert the
            pool came back balanced or an object store was reached.
        note: What diverged, when something did.
    """

    outcome: str
    error_type: str
    error_message: str
    matched: bool
    adapters: ReplayAdapters
    note: str | None = None


async def replay_attempt(
    runner: Any,
    record: Any,
    *,
    args: tuple[Any, ...] = (),
    scope: Any = None,
    adapters: ReplayAdapters | None = None,
) -> AttemptReplayResult:
    """Re-run one recorded job attempt with every boundary it crossed doubled.

    Args:
        runner: The `wreath.jobs.JobRunner` the task is registered on. It must
            be the build that produced the recording, for the same reason
            `reproduce_from_ring` must be.
        record: An `AttemptRecord`, from `open_recording`.
        args: The job's arguments. **The recording does not contain them** --
            see `wreath.recording.AttemptPolicy` -- so a handler that takes
            arguments needs them supplied here, from the queue row or from a
            fixture. `record.argument_count` says how many there were.
        scope: The application whose database/HTTP/object-store registries the
            handler reaches, if it reaches any. Doubled for the duration.
        adapters: Doubles to use instead of the ones derived from the
            recording, for a test that wants to script a result.

    Raises:
        AttemptReplayError: If the task is not registered on this runner, if the
            supplied arity does not match what the recording says the job
            carried, or if a recorded boundary failure has no modelled fault.

    **This never enqueues, dedupes against, or otherwise mutates the queue.** It
    does not claim, complete, fail, or sweep: it calls the registered handler
    directly, and the runner's own database is replaced with a double for the
    duration, so even a handler that reaches for it cannot arrive at the real
    table. That is what makes it safe to point at a production recording on a
    developer's machine.
    """
    from ._recording_format import AttemptOutcome
    from ._replay_adapters import installed_boundaries
    from .jobs import JobContext

    task = runner._tasks.get(record.task)
    if task is None:
        raise AttemptReplayError(
            f"task {record.task!r} is not registered on runner {runner.name!r}, so "
            "there is no handler to replay. Point this at the build the recording "
            "came from"
        )
    if len(args) != record.argument_count:
        raise AttemptReplayError(
            f"the recorded job carried {record.argument_count} argument(s) and "
            f"{len(args)} were supplied. The recording deliberately does not hold "
            "the values, so a replay cannot invent them; supply them from the "
            "queue row or from a fixture"
        )

    queue_database = getattr(runner._db, "name", "main")
    if adapters is None:
        adapters = attempt_adapters(record, databases=(queue_database,))
    context = JobContext(
        job_id=record.job_id,
        task=record.task,
        attempt=record.attempt,
        fence=record.fence,
        tenant=record.tenant,
        key=record.dedup_key or None,
        progress=None,
    )
    outcome: Any = AttemptOutcome.COMPLETED
    error: BaseException | None = None
    deadline = runner.deadline_for(record.task)
    with installed_boundaries(scope, adapters, slots=((runner, "_db", queue_database),)):
        try:
            async with asyncio.timeout(deadline):
                await task.func(context, *args)
        except TimeoutError:
            outcome = AttemptOutcome.DEADLINE_CANCELLED
        # Broad on purpose, and nothing is suppressed: the handler's exception
        # is the *product* of this function, reported as `outcome` and
        # `error_type` and compared against the recording. Narrowing it would
        # mean deciding in advance which failures a job is allowed to have.
        # `CancelledError` is not an `Exception` and so is not caught here.
        except Exception as failure:  # noqa: BLE001 - see above; it is reported, not swallowed
            outcome = AttemptOutcome.RAISED
            error = failure
    error_type = "" if error is None else type(error).__name__
    matched = str(outcome) == str(record.outcome) and error_type == record.error_type
    note = None
    if not matched:
        recorded = f" ({record.error_type})" if record.error_type else ""
        observed = f" ({error_type})" if error_type else ""
        note = (
            f"the recording ended {record.outcome}{recorded}; this replay ended {outcome}{observed}"
        )
    return AttemptReplayResult(
        outcome=str(outcome),
        error_type=error_type,
        error_message="" if error is None else str(error),
        matched=matched,
        adapters=adapters,
        note=note,
    )


def _attempt_test_name(record: Any) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in record.task).strip("_")
    return f"test_{slug}_attempt_{record.attempt}".replace("__", "_")


async def generate_attempt_test(
    runner: Any,
    record: Any,
    *,
    target: str,
    args: tuple[Any, ...] = (),
    scope: Any = None,
    name: str | None = None,
    origin: str | None = None,
) -> str:
    """A runnable pytest module that re-drives a recorded job attempt.

    `target` is how the generated file should import the **runner** --
    `"herd.app:jobs"` -- because a job attempt replays against the queue its
    task is registered on, not against an ASGI application.

    The attempt is replayed **now** and what it produces becomes the assertion,
    exactly as the request generator works: this is a *characterisation* test.
    When the replay and the recording disagree the assertion follows the replay
    and the docstring says what the recording held, because a generated test
    that fails the moment it is written teaches nothing about which of the two
    is wrong.

    A recorded *failure* generates a test that asserts the raise -- the outcome,
    the error type, its message, and the boundary events that produced it,
    written into the file so the doubles are rebuilt the same way on every run.
    A characterisation test for a failure is as useful as one for a success and
    more often what you want.
    """
    result = await replay_attempt(runner, record, args=args, scope=scope)

    module, _, attribute = target.partition(":")
    import_line = f"from {module} import {attribute}" if attribute else f"import {module}"
    queue = attribute or f"{module}.jobs"
    where = f" from {origin}" if origin else ""
    tenant = f" for tenant {record.tenant!r}" if record.tenant else ""
    cause = (
        f"Enqueued under trace context {record.trace_context}."
        if record.trace_context
        else "The queue row carried no trace context, so the enqueuing request is not named."
    )
    lines = [
        _GENERATED_HEADER,
        '"""A recorded job attempt, replayed.',
        "",
        f"Attempt {record.attempt} of {record.max_attempts} of task "
        f"{record.task!r}, job {record.job_id} on queue {record.queue!r}{tenant}.",
        f"The worker held fence {record.fence}.",
        cause,
        "",
        f"Captured{where} and generated by `wreath replay to-test`. Every boundary",
        "the attempt crossed is doubled, so this runs against no real database,",
        "object store, or upstream -- and it never touches the queue.",
    ]
    if record.argument_count:
        lines += [
            "",
            f"The job carried {record.argument_count} argument(s) and the recording",
            "holds none of their values: `args jsonb` is positional and the",
            "redaction policy is name-keyed, so there is no name to allow. Supply",
            "them below if the handler needs them.",
        ]
    if result.note:
        lines += ["", f"Replay divergence: {result.note}."]
    lines += [
        '"""',
        "",
        "import pytest",
        "",
        "from wreath.recording import AttemptRecord, BoundaryEvent",
        "from wreath.replay import replay_attempt",
        "",
        import_line,
        "",
        "",
        "RECORDED = AttemptRecord(",
        f"    job_id={record.job_id!r},",
        f"    queue={record.queue!r},",
        f"    task={record.task!r},",
        f"    attempt={record.attempt!r},",
        f"    max_attempts={record.max_attempts!r},",
        f"    tenant={record.tenant!r},",
        f"    dedup_key={record.dedup_key!r},",
        f"    fence={record.fence!r},",
        f"    trace_context={record.trace_context!r},",
        "    boundaries=(",
    ]
    lines += [
        f"        BoundaryEvent(seam={event.seam!r}, target={event.target!r}, "
        f"coordinate={event.coordinate!r}, error_type={event.error_type!r}),"
        for event in record.boundaries
    ]
    lines += [
        "    ),",
        f"    outcome={str(record.outcome)!r},",
        f"    error_type={record.error_type!r},",
        f"    error_message={record.error_message!r},",
        f"    argument_count={record.argument_count!r},",
        ")",
        "",
        "",
        "@pytest.mark.asyncio",
        f"async def {name or _attempt_test_name(record)}() -> None:",
        f"    result = await replay_attempt({queue}, RECORDED, args={args!r})",
        "",
        f"    assert result.outcome == {result.outcome!r}",
        f"    assert result.error_type == {result.error_type!r}",
    ]
    if result.error_message:
        lines.append(f"    assert result.error_message == {result.error_message!r}")
    lines += [
        f"    assert result.matched is {result.matched!r}",
        "",
    ]
    return "\n".join(lines)
