"""Pure-Python twin of the native WebSocket frame primitives."""

from __future__ import annotations


def ws_mask(data: bytes, key: bytes) -> bytes:
    if len(key) != 4:
        raise ValueError("mask key must be exactly 4 bytes")
    if not data:
        return b""
    repeated = key * ((len(data) + 3) // 4)
    return (int.from_bytes(data) ^ int.from_bytes(repeated[: len(data)])).to_bytes(
        len(data)
    )


def ws_build_frame(
    opcode: int,
    payload: bytes,
    fin: bool = True,
    mask_key: bytes | None = None,
) -> bytes:
    """Serialize one frame. Servers send unmasked; clients pass a 4-byte key."""
    if opcode < 0 or opcode > 0x0F:
        raise ValueError("opcode must be in range 0..15")
    if mask_key is not None and len(mask_key) != 4:
        raise ValueError("mask key must be exactly 4 bytes")
    b0 = (0x80 if fin else 0) | opcode
    mask_bit = 0x80 if mask_key is not None else 0
    length = len(payload)
    if length < 126:
        header = bytes((b0, mask_bit | length))
    elif length < 65536:
        header = bytes((b0, mask_bit | 126)) + length.to_bytes(2)
    else:
        header = bytes((b0, mask_bit | 127)) + length.to_bytes(8)
    if mask_key is None:
        return header + payload
    return header + mask_key + ws_mask(payload, mask_key)


def ws_parse_frame(buffer: bytes) -> tuple[bool, int, bytes, int] | None:
    if not isinstance(buffer, bytes):
        # Accept any bytes-like input, matching the C twin's buffer protocol.
        buffer = bytes(buffer)
    length = len(buffer)
    if length < 2:
        return None
    b0 = buffer[0]
    b1 = buffer[1]
    if b0 & 0x70:
        raise ValueError("reserved bits set")
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    payload_len = b1 & 0x7F
    pos = 2

    if payload_len == 126:
        if length < pos + 2:
            return None
        payload_len = int.from_bytes(buffer[pos : pos + 2])
        pos += 2
    elif payload_len == 127:
        if length < pos + 8:
            return None
        payload_len = int.from_bytes(buffer[pos : pos + 8])
        pos += 8

    key = b""
    if masked:
        if length < pos + 4:
            return None
        key = buffer[pos : pos + 4]
        pos += 4
    if length - pos < payload_len:
        return None

    payload = buffer[pos : pos + payload_len]
    if masked:
        payload = ws_mask(payload, key)
    return fin, opcode, payload, pos + payload_len
