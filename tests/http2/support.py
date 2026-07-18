"""Independent HTTP/2 reference codec for tests only.

This is an obvious, self-contained reference implementation of HTTP/2 framing
(RFC 9113) and HPACK (RFC 7541). It is deliberately NOT imported by production
code; it exists so tests can encode requests and decode server output without
trusting the implementation under test.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --- connection preface (RFC 9113 s3.4) ------------------------------------
PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

# --- frame types (RFC 9113 s6) ---------------------------------------------
DATA = 0x0
HEADERS = 0x1
PRIORITY = 0x2
RST_STREAM = 0x3
SETTINGS = 0x4
PUSH_PROMISE = 0x5
PING = 0x6
GOAWAY = 0x7
WINDOW_UPDATE = 0x8
CONTINUATION = 0x9

# --- flags -----------------------------------------------------------------
FLAG_END_STREAM = 0x1
FLAG_ACK = 0x1
FLAG_END_HEADERS = 0x4
FLAG_PADDED = 0x8
FLAG_PRIORITY = 0x20

# --- settings identifiers (RFC 9113 s6.5.2) --------------------------------
SETTINGS_HEADER_TABLE_SIZE = 0x1
SETTINGS_ENABLE_PUSH = 0x2
SETTINGS_MAX_CONCURRENT_STREAMS = 0x3
SETTINGS_INITIAL_WINDOW_SIZE = 0x4
SETTINGS_MAX_FRAME_SIZE = 0x5
SETTINGS_MAX_HEADER_LIST_SIZE = 0x6

# --- error codes (RFC 9113 s7) ---------------------------------------------
NO_ERROR = 0x0
PROTOCOL_ERROR = 0x1
INTERNAL_ERROR = 0x2
FLOW_CONTROL_ERROR = 0x3
SETTINGS_TIMEOUT = 0x4
STREAM_CLOSED = 0x5
FRAME_SIZE_ERROR = 0x6
REFUSED_STREAM = 0x7
CANCEL = 0x8
COMPRESSION_ERROR = 0x9
CONNECT_ERROR = 0xA
ENHANCE_YOUR_CALM = 0xB
INADEQUATE_SECURITY = 0xC
HTTP_1_1_REQUIRED = 0xD

DEFAULT_MAX_FRAME_SIZE = 16384


# --- frame header ----------------------------------------------------------
@dataclass
class Frame:
    type: int
    flags: int
    stream_id: int
    payload: bytes = b""

    def encode(self) -> bytes:
        length = len(self.payload)
        if length > 0xFFFFFF:
            raise ValueError("frame payload too large")
        header = (
            length.to_bytes(3, "big")
            + bytes([self.type & 0xFF, self.flags & 0xFF])
            + (self.stream_id & 0x7FFFFFFF).to_bytes(4, "big")
        )
        return header + self.payload


def encode_frame(type_: int, flags: int, stream_id: int, payload: bytes = b"") -> bytes:
    return Frame(type_, flags, stream_id, payload).encode()


class FrameParser:
    """Incremental parser: feed bytes, pull whole frames as they complete."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    def frames(self) -> list[Frame]:
        out: list[Frame] = []
        while True:
            if len(self._buf) < 9:
                break
            length = int.from_bytes(self._buf[0:3], "big")
            if len(self._buf) < 9 + length:
                break
            type_ = self._buf[3]
            flags = self._buf[4]
            stream_id = int.from_bytes(self._buf[5:9], "big") & 0x7FFFFFFF
            reserved = self._buf[5] >> 7
            payload = bytes(self._buf[9 : 9 + length])
            del self._buf[0 : 9 + length]
            frame = Frame(type_, flags, stream_id, payload)
            frame.reserved_bit = reserved  # type: ignore[attr-defined]
            out.append(frame)
        return out


# --- SETTINGS payload ------------------------------------------------------
def encode_settings(settings: dict[int, int] | None = None, *, ack: bool = False) -> bytes:
    if ack:
        return encode_frame(SETTINGS, FLAG_ACK, 0, b"")
    payload = b"".join(
        ident.to_bytes(2, "big") + value.to_bytes(4, "big")
        for ident, value in (settings or {}).items()
    )
    return encode_frame(SETTINGS, 0, 0, payload)


def parse_settings(payload: bytes) -> dict[int, int]:
    if len(payload) % 6 != 0:
        raise ValueError("SETTINGS payload not a multiple of 6")
    out: dict[int, int] = {}
    for i in range(0, len(payload), 6):
        ident = int.from_bytes(payload[i : i + 2], "big")
        value = int.from_bytes(payload[i + 2 : i + 6], "big")
        out[ident] = value
    return out


def encode_window_update(stream_id: int, increment: int) -> bytes:
    return encode_frame(WINDOW_UPDATE, 0, stream_id, increment.to_bytes(4, "big"))


def encode_rst_stream(stream_id: int, error_code: int) -> bytes:
    return encode_frame(RST_STREAM, 0, stream_id, error_code.to_bytes(4, "big"))


def encode_ping(opaque: bytes = b"\x00" * 8, *, ack: bool = False) -> bytes:
    if len(opaque) != 8:
        raise ValueError("PING opaque data must be 8 bytes")
    return encode_frame(PING, FLAG_ACK if ack else 0, 0, opaque)


def parse_goaway(payload: bytes) -> tuple[int, int, bytes]:
    last_stream_id = int.from_bytes(payload[0:4], "big") & 0x7FFFFFFF
    error_code = int.from_bytes(payload[4:8], "big")
    debug = payload[8:]
    return last_stream_id, error_code, debug


# --- HPACK: static table (RFC 7541 Appendix A) -----------------------------
STATIC_TABLE: list[tuple[bytes, bytes]] = [
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

# --- HPACK: Huffman table (RFC 7541 Appendix B) ----------------------------
# (code, num_bits) indexed by symbol 0..255, with EOS at index 256.
HUFFMAN_CODES: list[tuple[int, int]] = [
    (0x1ff8, 13), (0x7fffd8, 23), (0xfffffe2, 28), (0xfffffe3, 28),
    (0xfffffe4, 28), (0xfffffe5, 28), (0xfffffe6, 28), (0xfffffe7, 28),
    (0xfffffe8, 28), (0xffffea, 24), (0x3ffffffc, 30), (0xfffffe9, 28),
    (0xfffffea, 28), (0x3ffffffd, 30), (0xfffffeb, 28), (0xfffffec, 28),
    (0xfffffed, 28), (0xfffffee, 28), (0xfffffef, 28), (0xffffff0, 28),
    (0xffffff1, 28), (0xffffff2, 28), (0x3ffffffe, 30), (0xffffff3, 28),
    (0xffffff4, 28), (0xffffff5, 28), (0xffffff6, 28), (0xffffff7, 28),
    (0xffffff8, 28), (0xffffff9, 28), (0xffffffa, 28), (0xffffffb, 28),
    (0x14, 6), (0x3f8, 10), (0x3f9, 10), (0xffa, 12),
    (0x1ff9, 13), (0x15, 6), (0xf8, 8), (0x7fa, 11),
    (0x3fa, 10), (0x3fb, 10), (0xf9, 8), (0x7fb, 11),
    (0xfa, 8), (0x16, 6), (0x17, 6), (0x18, 6),
    (0x0, 5), (0x1, 5), (0x2, 5), (0x19, 6),
    (0x1a, 6), (0x1b, 6), (0x1c, 6), (0x1d, 6),
    (0x1e, 6), (0x1f, 6), (0x5c, 7), (0xfb, 8),
    (0x7ffc, 15), (0x20, 6), (0xffb, 12), (0x3fc, 10),
    (0x1ffa, 13), (0x21, 6), (0x5d, 7), (0x5e, 7),
    (0x5f, 7), (0x60, 7), (0x61, 7), (0x62, 7),
    (0x63, 7), (0x64, 7), (0x65, 7), (0x66, 7),
    (0x67, 7), (0x68, 7), (0x69, 7), (0x6a, 7),
    (0x6b, 7), (0x6c, 7), (0x6d, 7), (0x6e, 7),
    (0x6f, 7), (0x70, 7), (0x71, 7), (0x72, 7),
    (0xfc, 8), (0x73, 7), (0xfd, 8), (0x1ffb, 13),
    (0x7fff0, 19), (0x1ffc, 13), (0x3ffc, 14), (0x22, 6),
    (0x7ffd, 15), (0x3, 5), (0x23, 6), (0x4, 5),
    (0x24, 6), (0x5, 5), (0x25, 6), (0x26, 6),
    (0x27, 6), (0x6, 5), (0x74, 7), (0x75, 7),
    (0x28, 6), (0x29, 6), (0x2a, 6), (0x7, 5),
    (0x2b, 6), (0x76, 7), (0x2c, 6), (0x8, 5),
    (0x9, 5), (0x2d, 6), (0x77, 7), (0x78, 7),
    (0x79, 7), (0x7a, 7), (0x7b, 7), (0x7ffe, 15),
    (0x7fc, 11), (0x3ffd, 14), (0x1ffd, 13), (0xffffffc, 28),
    (0xfffe6, 20), (0x3fffd2, 22), (0xfffe7, 20), (0xfffe8, 20),
    (0x3fffd3, 22), (0x3fffd4, 22), (0x3fffd5, 22), (0x7fffd9, 23),
    (0x3fffd6, 22), (0x7fffda, 23), (0x7fffdb, 23), (0x7fffdc, 23),
    (0x7fffdd, 23), (0x7fffde, 23), (0xffffeb, 24), (0x7fffdf, 23),
    (0xffffec, 24), (0xffffed, 24), (0x3fffd7, 22), (0x7fffe0, 23),
    (0xffffee, 24), (0x7fffe1, 23), (0x7fffe2, 23), (0x7fffe3, 23),
    (0x7fffe4, 23), (0x1fffdc, 21), (0x3fffd8, 22), (0x7fffe5, 23),
    (0x3fffd9, 22), (0x7fffe6, 23), (0x7fffe7, 23), (0xffffef, 24),
    (0x3fffda, 22), (0x1fffdd, 21), (0xfffe9, 20), (0x3fffdb, 22),
    (0x3fffdc, 22), (0x7fffe8, 23), (0x7fffe9, 23), (0x1fffde, 21),
    (0x7fffea, 23), (0x3fffdd, 22), (0x3fffde, 22), (0xfffff0, 24),
    (0x1fffdf, 21), (0x3fffdf, 22), (0x7fffeb, 23), (0x7fffec, 23),
    (0x1fffe0, 21), (0x1fffe1, 21), (0x3fffe0, 22), (0x1fffe2, 21),
    (0x7fffed, 23), (0x3fffe1, 22), (0x7fffee, 23), (0x7fffef, 23),
    (0xfffea, 20), (0x3fffe2, 22), (0x3fffe3, 22), (0x3fffe4, 22),
    (0x7ffff0, 23), (0x3fffe5, 22), (0x3fffe6, 22), (0x7ffff1, 23),
    (0x3ffffe0, 26), (0x3ffffe1, 26), (0xfffeb, 20), (0x7fff1, 19),
    (0x3fffe7, 22), (0x7ffff2, 23), (0x3fffe8, 22), (0x1ffffec, 25),
    (0x3ffffe2, 26), (0x3ffffe3, 26), (0x3ffffe4, 26), (0x7ffffde, 27),
    (0x7ffffdf, 27), (0x3ffffe5, 26), (0xfffff1, 24), (0x1ffffed, 25),
    (0x7fff2, 19), (0x1fffe3, 21), (0x3ffffe6, 26), (0x7ffffe0, 27),
    (0x7ffffe1, 27), (0x3ffffe7, 26), (0x7ffffe2, 27), (0xfffff2, 24),
    (0x1fffe4, 21), (0x1fffe5, 21), (0x3ffffe8, 26), (0x3ffffe9, 26),
    (0xffffffd, 28), (0x7ffffe3, 27), (0x7ffffe4, 27), (0x7ffffe5, 27),
    (0xfffec, 20), (0xfffff3, 24), (0xfffed, 20), (0x1fffe6, 21),
    (0x3fffe9, 22), (0x1fffe7, 21), (0x1fffe8, 21), (0x7ffff3, 23),
    (0x3fffea, 22), (0x3fffeb, 22), (0x1ffffee, 25), (0x1ffffef, 25),
    (0xfffff4, 24), (0xfffff5, 24), (0x3ffffea, 26), (0x7ffff4, 23),
    (0x3ffffeb, 26), (0x7ffffe6, 27), (0x3ffffec, 26), (0x3ffffed, 26),
    (0x7ffffe7, 27), (0x7ffffe8, 27), (0x7ffffe9, 27), (0x7ffffea, 27),
    (0x7ffffeb, 27), (0xffffffe, 28), (0x7ffffec, 27), (0x7ffffed, 27),
    (0x7ffffee, 27), (0x7ffffef, 27), (0x7fffff0, 27), (0x3ffffee, 26),
    (0x3fffffff, 30),
]


def _huffman_encode(data: bytes) -> bytes:
    bits = 0
    nbits = 0
    out = bytearray()
    for byte in data:
        code, length = HUFFMAN_CODES[byte]
        bits = (bits << length) | code
        nbits += length
        while nbits >= 8:
            nbits -= 8
            out.append((bits >> nbits) & 0xFF)
    if nbits > 0:
        # pad with 1-bits (EOS prefix)
        out.append(((bits << (8 - nbits)) | ((1 << (8 - nbits)) - 1)) & 0xFF)
    return bytes(out)


class _HuffmanDecoder:
    def __init__(self) -> None:
        self._by_len: dict[int, dict[int, int]] = {}
        for sym, (code, length) in enumerate(HUFFMAN_CODES):
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


_HUFFMAN_DECODER = _HuffmanDecoder()


def huffman_encode(data: bytes) -> bytes:
    return _huffman_encode(data)


def huffman_decode(data: bytes) -> bytes:
    return _HUFFMAN_DECODER.decode(data)


# --- HPACK integer/string primitives (RFC 7541 s5) -------------------------
def encode_integer(value: int, prefix_bits: int, prefix_flags: int = 0) -> bytes:
    max_prefix = (1 << prefix_bits) - 1
    if value < max_prefix:
        return bytes([prefix_flags | value])
    out = bytearray([prefix_flags | max_prefix])
    value -= max_prefix
    while value >= 128:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_integer(data: bytes, pos: int, prefix_bits: int) -> tuple[int, int]:
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


def encode_string(data: bytes, *, huffman: bool = False) -> bytes:
    if huffman:
        encoded = huffman_encode(data)
        return encode_integer(len(encoded), 7, 0x80) + encoded
    return encode_integer(len(data), 7, 0x00) + data


def decode_string(data: bytes, pos: int) -> tuple[bytes, int]:
    huffman = bool(data[pos] & 0x80)
    length, pos = decode_integer(data, pos, 7)
    raw = data[pos : pos + length]
    pos += length
    if huffman:
        return huffman_decode(raw), pos
    return raw, pos


# --- HPACK encoder ---------------------------------------------------------
@dataclass
class HpackEncoder:
    dynamic: list[tuple[bytes, bytes]] = field(default_factory=list)
    max_size: int = 4096
    _size: int = 0

    def _entry_size(self, name: bytes, value: bytes) -> int:
        return len(name) + len(value) + 32

    def _add(self, name: bytes, value: bytes) -> None:
        size = self._entry_size(name, value)
        while self.dynamic and self._size + size > self.max_size:
            old = self.dynamic.pop()
            self._size -= self._entry_size(*old)
        if size <= self.max_size:
            self.dynamic.insert(0, (name, value))
            self._size += size

    def encode(
        self,
        headers: list[tuple[bytes, bytes]],
        *,
        huffman: bool = False,
        index: bool = False,
    ) -> bytes:
        out = bytearray()
        for name, value in headers:
            # Always literal (never-indexed or with/without incremental indexing)
            # so tests keep full control of the emitted bytes.
            if index:
                out += encode_integer(0, 6, 0x40)  # literal w/ incremental, new name
                out += encode_string(name, huffman=huffman)
                out += encode_string(value, huffman=huffman)
                self._add(name, value)
            else:
                out += encode_integer(0, 4, 0x00)  # literal w/o indexing, new name
                out += encode_string(name, huffman=huffman)
                out += encode_string(value, huffman=huffman)
        return bytes(out)

    def encode_dynamic_table_size_update(self, new_size: int) -> bytes:
        self.max_size = new_size
        while self.dynamic and self._size > self.max_size:
            old = self.dynamic.pop()
            self._size -= self._entry_size(*old)
        return encode_integer(new_size, 5, 0x20)


# --- HPACK decoder ---------------------------------------------------------
class HpackError(Exception):
    pass


@dataclass
class HpackDecoder:
    dynamic: list[tuple[bytes, bytes]] = field(default_factory=list)
    max_size: int = 4096
    _size: int = 0

    def _entry_size(self, name: bytes, value: bytes) -> int:
        return len(name) + len(value) + 32

    def _add(self, name: bytes, value: bytes) -> None:
        size = self._entry_size(name, value)
        while self.dynamic and self._size + size > self.max_size:
            old = self.dynamic.pop()
            self._size -= self._entry_size(*old)
        if size <= self.max_size:
            self.dynamic.insert(0, (name, value))
            self._size += size

    def _lookup(self, index: int) -> tuple[bytes, bytes]:
        if index == 0:
            raise HpackError("index 0 is invalid")
        if index <= len(STATIC_TABLE):
            return STATIC_TABLE[index - 1]
        dyn_index = index - len(STATIC_TABLE) - 1
        if dyn_index >= len(self.dynamic):
            raise HpackError("index out of range")
        return self.dynamic[dyn_index]

    def decode(self, data: bytes) -> list[tuple[bytes, bytes]]:
        pos = 0
        headers: list[tuple[bytes, bytes]] = []
        n = len(data)
        while pos < n:
            byte = data[pos]
            if byte & 0x80:  # indexed header field
                index, pos = decode_integer(data, pos, 7)
                headers.append(self._lookup(index))
            elif byte & 0x40:  # literal with incremental indexing
                index, pos = decode_integer(data, pos, 6)
                name, value, pos = self._read_name_value(data, pos, index)
                self._add(name, value)
                headers.append((name, value))
            elif byte & 0x20:  # dynamic table size update
                new_size, pos = decode_integer(data, pos, 5)
                if new_size > self.max_size:
                    raise HpackError("size update exceeds maximum")
                self.max_size = new_size
                while self.dynamic and self._size > self.max_size:
                    old = self.dynamic.pop()
                    self._size -= self._entry_size(*old)
            else:  # literal without indexing / never indexed
                index, pos = decode_integer(data, pos, 4)
                name, value, pos = self._read_name_value(data, pos, index)
                headers.append((name, value))
        return headers

    def _read_name_value(
        self, data: bytes, pos: int, index: int
    ) -> tuple[bytes, bytes, int]:
        if index == 0:
            name, pos = decode_string(data, pos)
        else:
            name = self._lookup(index)[0]
        value, pos = decode_string(data, pos)
        return name, value, pos


# --- convenience: build a HEADERS frame from a header list -----------------
def build_headers_frame(
    stream_id: int,
    headers: list[tuple[bytes, bytes]],
    *,
    encoder: HpackEncoder | None = None,
    end_stream: bool = True,
    end_headers: bool = True,
    huffman: bool = False,
) -> bytes:
    encoder = encoder or HpackEncoder()
    block = encoder.encode(headers, huffman=huffman)
    flags = 0
    if end_stream:
        flags |= FLAG_END_STREAM
    if end_headers:
        flags |= FLAG_END_HEADERS
    return encode_frame(HEADERS, flags, stream_id, block)


def request_headers(
    method: bytes = b"GET",
    path: bytes = b"/",
    authority: bytes = b"localhost",
    scheme: bytes = b"https",
    extra: list[tuple[bytes, bytes]] | None = None,
) -> list[tuple[bytes, bytes]]:
    headers = [
        (b":method", method),
        (b":path", path),
        (b":scheme", scheme),
        (b":authority", authority),
    ]
    if extra:
        headers.extend(extra)
    return headers


# --- self-check against RFC 7541 Appendix C published byte vectors ----------
def _self_check() -> None:
    # RFC 7541 C.4.1: "www.example.com" Huffman
    assert huffman_encode(b"www.example.com") == bytes.fromhex(
        "f1e3c2e5f23a6ba0ab90f4ff"
    ), "Huffman www.example.com mismatch"
    # RFC 7541 C.4.2: "no-cache"
    assert huffman_encode(b"no-cache") == bytes.fromhex("a8eb10649cbf")
    # RFC 7541 C.4.3: "custom-key" / "custom-value"
    assert huffman_encode(b"custom-key") == bytes.fromhex("25a849e95ba97d7f")
    assert huffman_encode(b"custom-value") == bytes.fromhex("25a849e95bb8e8b4bf")
    # Round-trip decode
    for s in (b"www.example.com", b"no-cache", b"custom-key", b"custom-value",
              b":method", b"/index.html", b"302", b"private", b"gzip"):
        assert huffman_decode(huffman_encode(s)) == s, f"round-trip failed: {s!r}"


_self_check()
