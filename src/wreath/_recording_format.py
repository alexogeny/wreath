"""`WFR1` recording container + async recording sink (Stage 5, slice 5c).

The `WFR1` container is the on-disk forensic recording: a fixed header (magic,
versions, the application metadata hash, a recording UUID, clock calibration, and
a build id), a metadata chunk that gives the numeric ids meaning, and a stream of
checksummed chunks -- capture slabs (`CAPT`) and optional completion cells
(`EVNT`) -- terminated by a footer (`FOOT`) on a clean close. A reader rejects
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
from typing import Any, Final

from ._flight_schema import (
    CAPTURE_SLAB_HEADER_SIZE,
    CELL_SIZE,
    SCHEMA_VERSION,
    CaptureSlab,
    MetadataImage,
    SchemaError,
)
from ._pure.flight import decode_metadata_image

__all__ = [
    "MAGIC",
    "WFR1Writer",
    "DecodedRecording",
    "read_recording",
    "RecordingSink",
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

_TAG_META: Final = b"META"
_TAG_CAPTURE: Final = b"CAPT"
_TAG_EVENT: Final = b"EVNT"
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


class WFR1Writer:
    """Streams a `WFR1` recording to a binary file object.

    Writes the header and metadata chunk on construction, appends capture/event
    chunks as they arrive, and writes the footer on `close`. It performs no
    buffering policy of its own beyond the file object's; the caller (the sink)
    decides how often to flush and owns the file's permissions and lifetime.
    """

    __slots__ = ("_file", "_chunk_count", "_capture_slabs", "_event_cells", "_closed")

    def __init__(self, file: Any, image: MetadataImage) -> None:
        self._file = file
        self._chunk_count = 0
        self._capture_slabs = 0
        self._event_cells = 0
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

    def close(self) -> None:
        """Write the footer (proving a clean close) and flush. Idempotent."""
        if self._closed:
            return
        self._closed = True
        footer = _FOOTER.pack(self._chunk_count + 1, self._capture_slabs, self._event_cells)
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
    clean = False
    footer_captures = 0
    footer_events = 0

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
            if len(payload) % CELL_SIZE != 0:
                break
            for i in range(0, len(payload), CELL_SIZE):
                cell = bytes(payload[i : i + CELL_SIZE])
                if cell[0] != SCHEMA_VERSION:
                    raise SchemaError("event cell has an unsupported schema version")
                events.append(cell)
        elif tag == _TAG_FOOTER:
            if len(payload) >= _FOOTER.size:
                _count, footer_captures, footer_events = _FOOTER.unpack_from(payload, 0)
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


# --- async recording sink ---------------------------------------------------


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
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            self._fh = os.fdopen(fd, "wb")
            self._writer = WFR1Writer(self._fh, self._image)
        except OSError:
            self._degraded = True
            self._close_file()
        self._stop.clear()
        thread = threading.Thread(
            target=self._run, name="wreath-flight-recording", daemon=True
        )
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
            except (OSError, ValueError):
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
