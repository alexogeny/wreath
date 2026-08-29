from __future__ import annotations

from dataclasses import dataclass, field

PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

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

FLAG_END_STREAM = 0x1
FLAG_ACK = 0x1
FLAG_END_HEADERS = 0x4
FLAG_PADDED = 0x8
FLAG_PRIORITY = 0x20

SETTINGS_HEADER_TABLE_SIZE = 0x1
SETTINGS_ENABLE_PUSH = 0x2
SETTINGS_MAX_CONCURRENT_STREAMS = 0x3
SETTINGS_INITIAL_WINDOW_SIZE = 0x4
SETTINGS_MAX_FRAME_SIZE = 0x5
SETTINGS_MAX_HEADER_LIST_SIZE = 0x6

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

# (code, num_bits) indexed by symbol 0..255, with EOS at index 256.
HUFFMAN_CODES: list[tuple[int, int]] = [
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

    def _read_name_value(self, data: bytes, pos: int, index: int) -> tuple[bytes, bytes, int]:
        if index == 0:
            name, pos = decode_string(data, pos)
        else:
            name = self._lookup(index)[0]
        value, pos = decode_string(data, pos)
        return name, value, pos


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


def _self_check() -> None:
    # RFC 7541 C.4.1: "www.example.com" Huffman
    assert huffman_encode(b"www.example.com") == bytes.fromhex("f1e3c2e5f23a6ba0ab90f4ff"), (
        "Huffman www.example.com mismatch"
    )
    # RFC 7541 C.4.2: "no-cache"
    assert huffman_encode(b"no-cache") == bytes.fromhex("a8eb10649cbf")
    # RFC 7541 C.4.3: "custom-key" / "custom-value"
    assert huffman_encode(b"custom-key") == bytes.fromhex("25a849e95ba97d7f")
    assert huffman_encode(b"custom-value") == bytes.fromhex("25a849e95bb8e8b4bf")
    # Round-trip decode
    for s in (
        b"www.example.com",
        b"no-cache",
        b"custom-key",
        b"custom-value",
        b":method",
        b"/index.html",
        b"302",
        b"private",
        b"gzip",
    ):
        assert huffman_decode(huffman_encode(s)) == s, f"round-trip failed: {s!r}"


_self_check()
