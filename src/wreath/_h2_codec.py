"""Decode-only HTTP/2 frame + HPACK reader for replay.

Transport replay drives the *real* native `Http2Protocol` and needs to read the
frames it wrote back so the owned response can be compared across builds. That
requires decoding HPACK (RFC 7541) and the frame layer (RFC 9113) — but only the
*decode* direction: replay never encodes HTTP/2. The server owns encoding; this
module only reads what the server produced.

The static and Huffman tables here are the RFC-published tables, not anything
implementation-specific. This module is deliberately independent of the native
codec so a decode bug here can never be masked by the same bug in the server.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "Frame",
    "parse_frames",
    "HpackError",
    "HpackDecoder",
    "H2StreamResponse",
    "decode_response",
    "goaway_error",
    "DATA",
    "HEADERS",
    "RST_STREAM",
    "GOAWAY",
    "CONTINUATION",
]

DATA = 0x0
HEADERS = 0x1
RST_STREAM = 0x3
SETTINGS = 0x4
PING = 0x6
GOAWAY = 0x7
WINDOW_UPDATE = 0x8
CONTINUATION = 0x9

FLAG_END_STREAM = 0x1
FLAG_END_HEADERS = 0x4
FLAG_PADDED = 0x8
FLAG_PRIORITY = 0x20


@dataclass(frozen=True, slots=True)
class Frame:
    type: int
    flags: int
    stream_id: int
    payload: bytes


def parse_frames(data: bytes) -> list[Frame]:
    """Parse every complete frame in `data`; a trailing partial frame is left
    out (replay reads a settled buffer, but this stays robust to a torn tail)."""
    frames: list[Frame] = []
    pos = 0
    n = len(data)
    while pos + 9 <= n:
        length = int.from_bytes(data[pos : pos + 3], "big")
        if pos + 9 + length > n:
            break
        type_ = data[pos + 3]
        flags = data[pos + 4]
        stream_id = int.from_bytes(data[pos + 5 : pos + 9], "big") & 0x7FFFFFFF
        payload = data[pos + 9 : pos + 9 + length]
        frames.append(Frame(type_, flags, stream_id, bytes(payload)))
        pos += 9 + length
    return frames


_STATIC_TABLE: list[tuple[bytes, bytes]] = [
    (b":authority", b""),
    (b":method", b"GET"),
    (b":method", b"POST"),
    (b":path", b"/"),
    (b":path", b"/index.html"),
    (b":scheme", b"http"),
    (b":scheme", b"https"),
    (b":status", b"200"),
    (b":status", b"204"),
    (b":status", b"206"),
    (b":status", b"304"),
    (b":status", b"400"),
    (b":status", b"404"),
    (b":status", b"500"),
    (b"accept-charset", b""),
    (b"accept-encoding", b"gzip, deflate"),
    (b"accept-language", b""),
    (b"accept-ranges", b""),
    (b"accept", b""),
    (b"access-control-allow-origin", b""),
    (b"age", b""),
    (b"allow", b""),
    (b"authorization", b""),
    (b"cache-control", b""),
    (b"content-disposition", b""),
    (b"content-encoding", b""),
    (b"content-language", b""),
    (b"content-length", b""),
    (b"content-location", b""),
    (b"content-range", b""),
    (b"content-type", b""),
    (b"cookie", b""),
    (b"date", b""),
    (b"etag", b""),
    (b"expect", b""),
    (b"expires", b""),
    (b"from", b""),
    (b"host", b""),
    (b"if-match", b""),
    (b"if-modified-since", b""),
    (b"if-none-match", b""),
    (b"if-range", b""),
    (b"if-unmodified-since", b""),
    (b"last-modified", b""),
    (b"link", b""),
    (b"location", b""),
    (b"max-forwards", b""),
    (b"proxy-authenticate", b""),
    (b"proxy-authorization", b""),
    (b"range", b""),
    (b"referer", b""),
    (b"refresh", b""),
    (b"retry-after", b""),
    (b"server", b""),
    (b"set-cookie", b""),
    (b"strict-transport-security", b""),
    (b"transfer-encoding", b""),
    (b"user-agent", b""),
    (b"vary", b""),
    (b"via", b""),
    (b"www-authenticate", b""),
]


_HUFFMAN_CODES: list[tuple[int, int]] = [
    (0x1FF8, 13),
    (0x7FFFD8, 23),
    (0xFFFFFE2, 28),
    (0xFFFFFE3, 28),
    (0xFFFFFE4, 28),
    (0xFFFFFE5, 28),
    (0xFFFFFE6, 28),
    (0xFFFFFE7, 28),
    (0xFFFFFE8, 28),
    (0xFFFFEA, 24),
    (0x3FFFFFFC, 30),
    (0xFFFFFE9, 28),
    (0xFFFFFEA, 28),
    (0x3FFFFFFD, 30),
    (0xFFFFFEB, 28),
    (0xFFFFFEC, 28),
    (0xFFFFFED, 28),
    (0xFFFFFEE, 28),
    (0xFFFFFEF, 28),
    (0xFFFFFF0, 28),
    (0xFFFFFF1, 28),
    (0xFFFFFF2, 28),
    (0x3FFFFFFE, 30),
    (0xFFFFFF3, 28),
    (0xFFFFFF4, 28),
    (0xFFFFFF5, 28),
    (0xFFFFFF6, 28),
    (0xFFFFFF7, 28),
    (0xFFFFFF8, 28),
    (0xFFFFFF9, 28),
    (0xFFFFFFA, 28),
    (0xFFFFFFB, 28),
    (0x14, 6),
    (0x3F8, 10),
    (0x3F9, 10),
    (0xFFA, 12),
    (0x1FF9, 13),
    (0x15, 6),
    (0xF8, 8),
    (0x7FA, 11),
    (0x3FA, 10),
    (0x3FB, 10),
    (0xF9, 8),
    (0x7FB, 11),
    (0xFA, 8),
    (0x16, 6),
    (0x17, 6),
    (0x18, 6),
    (0x0, 5),
    (0x1, 5),
    (0x2, 5),
    (0x19, 6),
    (0x1A, 6),
    (0x1B, 6),
    (0x1C, 6),
    (0x1D, 6),
    (0x1E, 6),
    (0x1F, 6),
    (0x5C, 7),
    (0xFB, 8),
    (0x7FFC, 15),
    (0x20, 6),
    (0xFFB, 12),
    (0x3FC, 10),
    (0x1FFA, 13),
    (0x21, 6),
    (0x5D, 7),
    (0x5E, 7),
    (0x5F, 7),
    (0x60, 7),
    (0x61, 7),
    (0x62, 7),
    (0x63, 7),
    (0x64, 7),
    (0x65, 7),
    (0x66, 7),
    (0x67, 7),
    (0x68, 7),
    (0x69, 7),
    (0x6A, 7),
    (0x6B, 7),
    (0x6C, 7),
    (0x6D, 7),
    (0x6E, 7),
    (0x6F, 7),
    (0x70, 7),
    (0x71, 7),
    (0x72, 7),
    (0xFC, 8),
    (0x73, 7),
    (0xFD, 8),
    (0x1FFB, 13),
    (0x7FFF0, 19),
    (0x1FFC, 13),
    (0x3FFC, 14),
    (0x22, 6),
    (0x7FFD, 15),
    (0x3, 5),
    (0x23, 6),
    (0x4, 5),
    (0x24, 6),
    (0x5, 5),
    (0x25, 6),
    (0x26, 6),
    (0x27, 6),
    (0x6, 5),
    (0x74, 7),
    (0x75, 7),
    (0x28, 6),
    (0x29, 6),
    (0x2A, 6),
    (0x7, 5),
    (0x2B, 6),
    (0x76, 7),
    (0x2C, 6),
    (0x8, 5),
    (0x9, 5),
    (0x2D, 6),
    (0x77, 7),
    (0x78, 7),
    (0x79, 7),
    (0x7A, 7),
    (0x7B, 7),
    (0x7FFE, 15),
    (0x7FC, 11),
    (0x3FFD, 14),
    (0x1FFD, 13),
    (0xFFFFFFC, 28),
    (0xFFFE6, 20),
    (0x3FFFD2, 22),
    (0xFFFE7, 20),
    (0xFFFE8, 20),
    (0x3FFFD3, 22),
    (0x3FFFD4, 22),
    (0x3FFFD5, 22),
    (0x7FFFD9, 23),
    (0x3FFFD6, 22),
    (0x7FFFDA, 23),
    (0x7FFFDB, 23),
    (0x7FFFDC, 23),
    (0x7FFFDD, 23),
    (0x7FFFDE, 23),
    (0xFFFFEB, 24),
    (0x7FFFDF, 23),
    (0xFFFFEC, 24),
    (0xFFFFED, 24),
    (0x3FFFD7, 22),
    (0x7FFFE0, 23),
    (0xFFFFEE, 24),
    (0x7FFFE1, 23),
    (0x7FFFE2, 23),
    (0x7FFFE3, 23),
    (0x7FFFE4, 23),
    (0x1FFFDC, 21),
    (0x3FFFD8, 22),
    (0x7FFFE5, 23),
    (0x3FFFD9, 22),
    (0x7FFFE6, 23),
    (0x7FFFE7, 23),
    (0xFFFFEF, 24),
    (0x3FFFDA, 22),
    (0x1FFFDD, 21),
    (0xFFFE9, 20),
    (0x3FFFDB, 22),
    (0x3FFFDC, 22),
    (0x7FFFE8, 23),
    (0x7FFFE9, 23),
    (0x1FFFDE, 21),
    (0x7FFFEA, 23),
    (0x3FFFDD, 22),
    (0x3FFFDE, 22),
    (0xFFFFF0, 24),
    (0x1FFFDF, 21),
    (0x3FFFDF, 22),
    (0x7FFFEB, 23),
    (0x7FFFEC, 23),
    (0x1FFFE0, 21),
    (0x1FFFE1, 21),
    (0x3FFFE0, 22),
    (0x1FFFE2, 21),
    (0x7FFFED, 23),
    (0x3FFFE1, 22),
    (0x7FFFEE, 23),
    (0x7FFFEF, 23),
    (0xFFFEA, 20),
    (0x3FFFE2, 22),
    (0x3FFFE3, 22),
    (0x3FFFE4, 22),
    (0x7FFFF0, 23),
    (0x3FFFE5, 22),
    (0x3FFFE6, 22),
    (0x7FFFF1, 23),
    (0x3FFFFE0, 26),
    (0x3FFFFE1, 26),
    (0xFFFEB, 20),
    (0x7FFF1, 19),
    (0x3FFFE7, 22),
    (0x7FFFF2, 23),
    (0x3FFFE8, 22),
    (0x1FFFFEC, 25),
    (0x3FFFFE2, 26),
    (0x3FFFFE3, 26),
    (0x3FFFFE4, 26),
    (0x7FFFFDE, 27),
    (0x7FFFFDF, 27),
    (0x3FFFFE5, 26),
    (0xFFFFF1, 24),
    (0x1FFFFED, 25),
    (0x7FFF2, 19),
    (0x1FFFE3, 21),
    (0x3FFFFE6, 26),
    (0x7FFFFE0, 27),
    (0x7FFFFE1, 27),
    (0x3FFFFE7, 26),
    (0x7FFFFE2, 27),
    (0xFFFFF2, 24),
    (0x1FFFE4, 21),
    (0x1FFFE5, 21),
    (0x3FFFFE8, 26),
    (0x3FFFFE9, 26),
    (0xFFFFFFD, 28),
    (0x7FFFFE3, 27),
    (0x7FFFFE4, 27),
    (0x7FFFFE5, 27),
    (0xFFFEC, 20),
    (0xFFFFF3, 24),
    (0xFFFED, 20),
    (0x1FFFE6, 21),
    (0x3FFFE9, 22),
    (0x1FFFE7, 21),
    (0x1FFFE8, 21),
    (0x7FFFF3, 23),
    (0x3FFFEA, 22),
    (0x3FFFEB, 22),
    (0x1FFFFEE, 25),
    (0x1FFFFEF, 25),
    (0xFFFFF4, 24),
    (0xFFFFF5, 24),
    (0x3FFFFEA, 26),
    (0x7FFFF4, 23),
    (0x3FFFFEB, 26),
    (0x7FFFFE6, 27),
    (0x3FFFFEC, 26),
    (0x3FFFFED, 26),
    (0x7FFFFE7, 27),
    (0x7FFFFE8, 27),
    (0x7FFFFE9, 27),
    (0x7FFFFEA, 27),
    (0x7FFFFEB, 27),
    (0xFFFFFFE, 28),
    (0x7FFFFEC, 27),
    (0x7FFFFED, 27),
    (0x7FFFFEE, 27),
    (0x7FFFFEF, 27),
    (0x7FFFFF0, 27),
    (0x3FFFFEE, 26),
    (0x3FFFFFFF, 30),
]


class HpackError(Exception):
    """A malformed HPACK header block."""


class _HuffmanDecoder:
    __slots__ = ("_by_len",)

    def __init__(self) -> None:
        self._by_len: dict[int, dict[int, int]] = {}
        for sym, (code, length) in enumerate(_HUFFMAN_CODES):
            self._by_len.setdefault(length, {})[code] = sym

    def decode(self, data: bytes) -> bytes:
        bits = 0
        nbits = 0
        out = bytearray()
        for byte in data:
            bits = (bits << 8) | byte
            nbits += 8
            while True:
                matched = False
                for length in range(5, 31):
                    if length > nbits:
                        break
                    code = (bits >> (nbits - length)) & ((1 << length) - 1)
                    sym = self._by_len.get(length, {}).get(code)
                    if sym is not None and sym < 256:
                        out.append(sym)
                        nbits -= length
                        bits &= (1 << nbits) - 1
                        matched = True
                        break
                if not matched:
                    break
        return bytes(out)


_HUFFMAN = _HuffmanDecoder()


def _decode_integer(data: bytes, pos: int, prefix_bits: int) -> tuple[int, int]:
    max_prefix = (1 << prefix_bits) - 1
    value = data[pos] & max_prefix
    pos += 1
    if value < max_prefix:
        return value, pos
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value += (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    return value, pos


def _decode_string(data: bytes, pos: int) -> tuple[bytes, int]:
    huffman = bool(data[pos] & 0x80)
    length, pos = _decode_integer(data, pos, 7)
    raw = data[pos : pos + length]
    pos += length
    return (_HUFFMAN.decode(raw) if huffman else bytes(raw)), pos


@dataclass
class HpackDecoder:
    """A stateful HPACK decoder — one per connection, so the dynamic table
    tracks across the connection's header blocks in emission order."""

    dynamic: list[tuple[bytes, bytes]] = field(default_factory=list)
    max_size: int = 4096
    _size: int = 0

    def _entry_size(self, name: bytes, value: bytes) -> int:
        return len(name) + len(value) + 32

    def _add(self, name: bytes, value: bytes) -> None:
        size = self._entry_size(name, value)
        while self.dynamic and self._size + size > self.max_size:
            self._size -= self._entry_size(*self.dynamic.pop())
        if size <= self.max_size:
            self.dynamic.insert(0, (name, value))
            self._size += size

    def _lookup(self, index: int) -> tuple[bytes, bytes]:
        if index == 0:
            raise HpackError("index 0 is invalid")
        if index <= len(_STATIC_TABLE):
            return _STATIC_TABLE[index - 1]
        dyn = index - len(_STATIC_TABLE) - 1
        if dyn >= len(self.dynamic):
            raise HpackError("dynamic index out of range")
        return self.dynamic[dyn]

    def _name_value(self, data: bytes, pos: int, index: int) -> tuple[bytes, bytes, int]:
        if index == 0:
            name, pos = _decode_string(data, pos)
        else:
            name = self._lookup(index)[0]
        value, pos = _decode_string(data, pos)
        return name, value, pos

    def decode(self, data: bytes) -> list[tuple[bytes, bytes]]:
        pos = 0
        headers: list[tuple[bytes, bytes]] = []
        n = len(data)
        while pos < n:
            byte = data[pos]
            if byte & 0x80:
                index, pos = _decode_integer(data, pos, 7)
                headers.append(self._lookup(index))
            elif byte & 0x40:
                index, pos = _decode_integer(data, pos, 6)
                name, value, pos = self._name_value(data, pos, index)
                self._add(name, value)
                headers.append((name, value))
            elif byte & 0x20:
                new_size, pos = _decode_integer(data, pos, 5)
                if new_size > self.max_size:
                    raise HpackError("size update exceeds maximum")
                self.max_size = new_size
                while self.dynamic and self._size > self.max_size:
                    self._size -= self._entry_size(*self.dynamic.pop())
            else:
                index, pos = _decode_integer(data, pos, 4)
                name, value, pos = self._name_value(data, pos, index)
                headers.append((name, value))
        return headers


@dataclass
class H2StreamResponse:
    """The owned response the server produced on one stream, decoded."""

    stream_id: int
    status: int = 0
    headers: tuple[tuple[bytes, bytes], ...] = ()
    body: bytes = b""
    ended: bool = False
    reset: int | None = None  # RST_STREAM error code, if the stream was reset

    def header(self, name: bytes) -> bytes | None:
        for key, value in self.headers:
            if key == name:
                return value
        return None


def _header_block(frame: Frame) -> bytes:
    """The HPACK block inside a HEADERS frame, past any pad/priority prefix."""
    payload = frame.payload
    pos = 0
    pad = 0
    if frame.flags & FLAG_PADDED:
        pad = payload[0]
        pos = 1
    if frame.flags & FLAG_PRIORITY:
        pos += 5  # stream dependency (4) + weight (1)
    end = len(payload) - pad
    return payload[pos:end]


def goaway_error(data: bytes) -> int | None:
    """The error code of the first GOAWAY the server sent, or None. A GOAWAY is
    the owned response to a connection-level protocol error."""
    for frame in parse_frames(data):
        if frame.type == GOAWAY and len(frame.payload) >= 8:
            return int.from_bytes(frame.payload[4:8], "big")
    return None


def decode_response(data: bytes) -> dict[int, H2StreamResponse]:
    """Decode all server-emitted frames into per-stream owned responses.

    Handles HEADERS(+CONTINUATION) blocks, DATA bodies, and RST_STREAM, decoding
    HPACK in frame-emission order (so the dynamic table stays consistent). The
    connection-control stream 0 (SETTINGS/PING/GOAWAY/WINDOW_UPDATE) is ignored.
    """
    decoder = HpackDecoder()
    streams: dict[int, H2StreamResponse] = {}
    pending: dict[int, bytearray] = {}  # stream_id -> partial header block
    bodies: dict[int, bytearray] = {}  # stream_id -> DATA, frozen once below

    def _stream(sid: int) -> H2StreamResponse:
        got = streams.get(sid)
        if got is None:
            got = streams[sid] = H2StreamResponse(sid)
        return got

    def _apply_headers(sid: int, block: bytes) -> None:
        stream = _stream(sid)
        decoded = decoder.decode(block)
        rest: list[tuple[bytes, bytes]] = []
        for name, value in decoded:
            if name == b":status":
                try:
                    stream.status = int(value)
                except ValueError:
                    stream.status = 0
            elif not name.startswith(b":"):
                rest.append((name, value))
        stream.headers = tuple(rest)

    for frame in parse_frames(data):
        if frame.stream_id == 0:
            continue
        if frame.type == HEADERS:
            block = _header_block(frame)
            if frame.flags & FLAG_END_HEADERS:
                _apply_headers(frame.stream_id, block)
            else:
                pending[frame.stream_id] = bytearray(block)
            if frame.flags & FLAG_END_STREAM:
                _stream(frame.stream_id).ended = True
        elif frame.type == CONTINUATION:
            buf = pending.get(frame.stream_id)
            if buf is not None:
                buf += frame.payload
                if frame.flags & FLAG_END_HEADERS:
                    _apply_headers(frame.stream_id, bytes(buf))
                    del pending[frame.stream_id]
        elif frame.type == DATA:
            stream = _stream(frame.stream_id)
            bodies.setdefault(frame.stream_id, bytearray()).extend(frame.payload)
            if frame.flags & FLAG_END_STREAM:
                stream.ended = True
        elif frame.type == RST_STREAM:
            stream = _stream(frame.stream_id)
            stream.reset = int.from_bytes(frame.payload[:4], "big") if frame.payload else 0
    for stream_id, body in bodies.items():
        streams[stream_id].body = bytes(body)
    return streams
