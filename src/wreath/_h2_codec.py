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


# --- frame layer -------------------------------------------------------------


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
        length = int.from_bytes(data[pos:pos + 3], "big")
        if pos + 9 + length > n:
            break
        type_ = data[pos + 3]
        flags = data[pos + 4]
        stream_id = int.from_bytes(data[pos + 5:pos + 9], "big") & 0x7FFFFFFF
        payload = data[pos + 9:pos + 9 + length]
        frames.append(Frame(type_, flags, stream_id, bytes(payload)))
        pos += 9 + length
    return frames


# --- HPACK static table (RFC 7541 Appendix A) --------------------------------

_STATIC_TABLE: list[tuple[bytes, bytes]] = [
    (b":authority", b""), (b":method", b"GET"), (b":method", b"POST"),
    (b":path", b"/"), (b":path", b"/index.html"), (b":scheme", b"http"),
    (b":scheme", b"https"), (b":status", b"200"), (b":status", b"204"),
    (b":status", b"206"), (b":status", b"304"), (b":status", b"400"),
    (b":status", b"404"), (b":status", b"500"), (b"accept-charset", b""),
    (b"accept-encoding", b"gzip, deflate"), (b"accept-language", b""),
    (b"accept-ranges", b""), (b"accept", b""), (b"access-control-allow-origin", b""),
    (b"age", b""), (b"allow", b""), (b"authorization", b""), (b"cache-control", b""),
    (b"content-disposition", b""), (b"content-encoding", b""),
    (b"content-language", b""), (b"content-length", b""), (b"content-location", b""),
    (b"content-range", b""), (b"content-type", b""), (b"cookie", b""), (b"date", b""),
    (b"etag", b""), (b"expect", b""), (b"expires", b""), (b"from", b""), (b"host", b""),
    (b"if-match", b""), (b"if-modified-since", b""), (b"if-none-match", b""),
    (b"if-range", b""), (b"if-unmodified-since", b""), (b"last-modified", b""),
    (b"link", b""), (b"location", b""), (b"max-forwards", b""),
    (b"proxy-authenticate", b""), (b"proxy-authorization", b""), (b"range", b""),
    (b"referer", b""), (b"refresh", b""), (b"retry-after", b""), (b"server", b""),
    (b"set-cookie", b""), (b"strict-transport-security", b""),
    (b"transfer-encoding", b""), (b"user-agent", b""), (b"vary", b""), (b"via", b""),
    (b"www-authenticate", b""),
]

# --- HPACK Huffman table (RFC 7541 Appendix B): (code, num_bits) per symbol ---

_HUFFMAN_CODES: list[tuple[int, int]] = [
    (0x1ff8, 13), (0x7fffd8, 23), (0xfffffe2, 28), (0xfffffe3, 28), (0xfffffe4, 28),
    (0xfffffe5, 28), (0xfffffe6, 28), (0xfffffe7, 28), (0xfffffe8, 28), (0xffffea, 24),
    (0x3ffffffc, 30), (0xfffffe9, 28), (0xfffffea, 28), (0x3ffffffd, 30), (0xfffffeb, 28),
    (0xfffffec, 28), (0xfffffed, 28), (0xfffffee, 28), (0xfffffef, 28), (0xffffff0, 28),
    (0xffffff1, 28), (0xffffff2, 28), (0x3ffffffe, 30), (0xffffff3, 28), (0xffffff4, 28),
    (0xffffff5, 28), (0xffffff6, 28), (0xffffff7, 28), (0xffffff8, 28), (0xffffff9, 28),
    (0xffffffa, 28), (0xffffffb, 28), (0x14, 6), (0x3f8, 10), (0x3f9, 10), (0xffa, 12),
    (0x1ff9, 13), (0x15, 6), (0xf8, 8), (0x7fa, 11), (0x3fa, 10), (0x3fb, 10), (0xf9, 8),
    (0x7fb, 11), (0xfa, 8), (0x16, 6), (0x17, 6), (0x18, 6), (0x0, 5), (0x1, 5), (0x2, 5),
    (0x19, 6), (0x1a, 6), (0x1b, 6), (0x1c, 6), (0x1d, 6), (0x1e, 6), (0x1f, 6), (0x5c, 7),
    (0xfb, 8), (0x7ffc, 15), (0x20, 6), (0xffb, 12), (0x3fc, 10), (0x1ffa, 13), (0x21, 6),
    (0x5d, 7), (0x5e, 7), (0x5f, 7), (0x60, 7), (0x61, 7), (0x62, 7), (0x63, 7), (0x64, 7),
    (0x65, 7), (0x66, 7), (0x67, 7), (0x68, 7), (0x69, 7), (0x6a, 7), (0x6b, 7), (0x6c, 7),
    (0x6d, 7), (0x6e, 7), (0x6f, 7), (0x70, 7), (0x71, 7), (0x72, 7), (0xfc, 8), (0x73, 7),
    (0xfd, 8), (0x1ffb, 13), (0x7fff0, 19), (0x1ffc, 13), (0x3ffc, 14), (0x22, 6),
    (0x7ffd, 15), (0x3, 5), (0x23, 6), (0x4, 5), (0x24, 6), (0x5, 5), (0x25, 6), (0x26, 6),
    (0x27, 6), (0x6, 5), (0x74, 7), (0x75, 7), (0x28, 6), (0x29, 6), (0x2a, 6), (0x7, 5),
    (0x2b, 6), (0x76, 7), (0x2c, 6), (0x8, 5), (0x9, 5), (0x2d, 6), (0x77, 7), (0x78, 7),
    (0x79, 7), (0x7a, 7), (0x7b, 7), (0x7ffe, 15), (0x7fc, 11), (0x3ffd, 14), (0x1ffd, 13),
    (0xffffffc, 28), (0xfffe6, 20), (0x3fffd2, 22), (0xfffe7, 20), (0xfffe8, 20),
    (0x3fffd3, 22), (0x3fffd4, 22), (0x3fffd5, 22), (0x7fffd9, 23), (0x3fffd6, 22),
    (0x7fffda, 23), (0x7fffdb, 23), (0x7fffdc, 23), (0x7fffdd, 23), (0x7fffde, 23),
    (0xffffeb, 24), (0x7fffdf, 23), (0xffffec, 24), (0xffffed, 24), (0x3fffd7, 22),
    (0x7fffe0, 23), (0xffffee, 24), (0x7fffe1, 23), (0x7fffe2, 23), (0x7fffe3, 23),
    (0x7fffe4, 23), (0x1fffdc, 21), (0x3fffd8, 22), (0x7fffe5, 23), (0x3fffd9, 22),
    (0x7fffe6, 23), (0x7fffe7, 23), (0xffffef, 24), (0x3fffda, 22), (0x1fffdd, 21),
    (0xfffe9, 20), (0x3fffdb, 22), (0x3fffdc, 22), (0x7fffe8, 23), (0x7fffe9, 23),
    (0x1fffde, 21), (0x7fffea, 23), (0x3fffdd, 22), (0x3fffde, 22), (0xfffff0, 24),
    (0x1fffdf, 21), (0x3fffdf, 22), (0x7fffeb, 23), (0x7fffec, 23), (0x1fffe0, 21),
    (0x1fffe1, 21), (0x3fffe0, 22), (0x1fffe2, 21), (0x7fffed, 23), (0x3fffe1, 22),
    (0x7fffee, 23), (0x7fffef, 23), (0xfffea, 20), (0x3fffe2, 22), (0x3fffe3, 22),
    (0x3fffe4, 22), (0x7ffff0, 23), (0x3fffe5, 22), (0x3fffe6, 22), (0x7ffff1, 23),
    (0x3ffffe0, 26), (0x3ffffe1, 26), (0xfffeb, 20), (0x7fff1, 19), (0x3fffe7, 22),
    (0x7ffff2, 23), (0x3fffe8, 22), (0x1ffffec, 25), (0x3ffffe2, 26), (0x3ffffe3, 26),
    (0x3ffffe4, 26), (0x7ffffde, 27), (0x7ffffdf, 27), (0x3ffffe5, 26), (0xfffff1, 24),
    (0x1ffffed, 25), (0x7fff2, 19), (0x1fffe3, 21), (0x3ffffe6, 26), (0x7ffffe0, 27),
    (0x7ffffe1, 27), (0x3ffffe7, 26), (0x7ffffe2, 27), (0xfffff2, 24), (0x1fffe4, 21),
    (0x1fffe5, 21), (0x3ffffe8, 26), (0x3ffffe9, 26), (0xffffffd, 28), (0x7ffffe3, 27),
    (0x7ffffe4, 27), (0x7ffffe5, 27), (0xfffec, 20), (0xfffff3, 24), (0xfffed, 20),
    (0x1fffe6, 21), (0x3fffe9, 22), (0x1fffe7, 21), (0x1fffe8, 21), (0x7ffff3, 23),
    (0x3fffea, 22), (0x3fffeb, 22), (0x1ffffee, 25), (0x1ffffef, 25), (0xfffff4, 24),
    (0xfffff5, 24), (0x3ffffea, 26), (0x7ffff4, 23), (0x3ffffeb, 26), (0x7ffffe6, 27),
    (0x3ffffec, 26), (0x3ffffed, 26), (0x7ffffe7, 27), (0x7ffffe8, 27), (0x7ffffe9, 27),
    (0x7ffffea, 27), (0x7ffffeb, 27), (0xffffffe, 28), (0x7ffffec, 27), (0x7ffffed, 27),
    (0x7ffffee, 27), (0x7ffffef, 27), (0x7fffff0, 27), (0x3ffffee, 26), (0x3fffffff, 30),
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
    raw = data[pos:pos + length]
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


# --- response reassembly -----------------------------------------------------


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
