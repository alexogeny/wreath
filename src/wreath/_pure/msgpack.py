"""A dependency-free MessagePack encoder (serialize only).

Enough of the spec to serialize JSON-shaped data — nil, bool, int, float, str,
bin, array, map — for content negotiation. Deserialization is not needed for
response encoding and is intentionally absent. Pinned to the spec's byte layout
by known-answer vectors in ``tests/test_negotiation.py``.
"""

from __future__ import annotations

import struct
from typing import Any

__all__ = ["packb"]


def packb(obj: Any) -> bytes:
    """Encode ``obj`` to MessagePack bytes."""
    out = bytearray()
    _pack(obj, out)
    return bytes(out)


def _pack(obj: Any, out: bytearray) -> None:
    if obj is None:
        out.append(0xC0)
    elif obj is True:
        out.append(0xC3)
    elif obj is False:
        out.append(0xC2)
    elif isinstance(obj, int):        # bool is handled above (subclass of int)
        _pack_int(obj, out)
    elif isinstance(obj, float):
        out.append(0xCB)
        out += struct.pack(">d", obj)
    elif isinstance(obj, str):
        _pack_str(obj, out)
    elif isinstance(obj, (bytes, bytearray, memoryview)):
        _pack_bin(bytes(obj), out)
    elif isinstance(obj, (list, tuple)):
        _pack_seq(obj, out)
    elif isinstance(obj, dict):
        _pack_map(obj, out)
    else:
        raise TypeError(f"cannot msgpack-encode {type(obj).__name__}")


def _pack_int(n: int, out: bytearray) -> None:
    if 0 <= n <= 0x7F:
        out.append(n)
    elif -0x20 <= n < 0:
        out.append(n & 0xFF)
    elif 0 <= n <= 0xFF:
        out += bytes((0xCC, n))
    elif 0 <= n <= 0xFFFF:
        out.append(0xCD)
        out += struct.pack(">H", n)
    elif 0 <= n <= 0xFFFFFFFF:
        out.append(0xCE)
        out += struct.pack(">I", n)
    elif 0 <= n <= 0xFFFFFFFFFFFFFFFF:
        out.append(0xCF)
        out += struct.pack(">Q", n)
    elif -0x80 <= n < 0:
        out.append(0xD0)
        out += struct.pack(">b", n)
    elif -0x8000 <= n < 0:
        out.append(0xD1)
        out += struct.pack(">h", n)
    elif -0x80000000 <= n < 0:
        out.append(0xD2)
        out += struct.pack(">i", n)
    elif -0x8000000000000000 <= n < 0:
        out.append(0xD3)
        out += struct.pack(">q", n)
    else:
        raise ValueError("integer out of range for MessagePack")


def _pack_str(value: str, out: bytearray) -> None:
    data = value.encode("utf-8")
    n = len(data)
    if n <= 0x1F:
        out.append(0xA0 | n)
    elif n <= 0xFF:
        out += bytes((0xD9, n))
    elif n <= 0xFFFF:
        out.append(0xDA)
        out += struct.pack(">H", n)
    elif n <= 0xFFFFFFFF:
        out.append(0xDB)
        out += struct.pack(">I", n)
    else:
        raise ValueError("string too long for MessagePack")
    out += data


def _pack_bin(data: bytes, out: bytearray) -> None:
    n = len(data)
    if n <= 0xFF:
        out += bytes((0xC4, n))
    elif n <= 0xFFFF:
        out.append(0xC5)
        out += struct.pack(">H", n)
    elif n <= 0xFFFFFFFF:
        out.append(0xC6)
        out += struct.pack(">I", n)
    else:
        raise ValueError("bytes too long for MessagePack")
    out += data


def _pack_seq(seq: Any, out: bytearray) -> None:
    n = len(seq)
    if n <= 0xF:
        out.append(0x90 | n)
    elif n <= 0xFFFF:
        out.append(0xDC)
        out += struct.pack(">H", n)
    elif n <= 0xFFFFFFFF:
        out.append(0xDD)
        out += struct.pack(">I", n)
    else:
        raise ValueError("array too long for MessagePack")
    for item in seq:
        _pack(item, out)


def _pack_map(mapping: dict, out: bytearray) -> None:
    n = len(mapping)
    if n <= 0xF:
        out.append(0x80 | n)
    elif n <= 0xFFFF:
        out.append(0xDE)
        out += struct.pack(">H", n)
    elif n <= 0xFFFFFFFF:
        out.append(0xDF)
        out += struct.pack(">I", n)
    else:
        raise ValueError("map too long for MessagePack")
    for key, value in mapping.items():
        _pack(key, out)
        _pack(value, out)
