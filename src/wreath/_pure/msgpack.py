"""A dependency-free MessagePack encoder (serialize only).

Enough of the spec to serialize JSON-shaped data — nil, bool, int, float, str,
bin, array, map — for content negotiation. Deserialization is not needed for
response encoding and is intentionally absent. Pinned to the spec's byte layout
by known-answer vectors in ``tests/test_negotiation.py``.

Map keys must be scalars. Encoding a container key produces bytes no decoder
targeting a mapping can rebuild, and :func:`json.dumps` refuses the same value,
so allowing it would mean the same handler return value was a ``TypeError`` on
one content type and silently unreadable on the other.
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


def _key_ok(key: Any) -> bool:
    """Whether *key* can survive the round trip through a decoder.

    A decoder targeting a mapping cannot rebuild a key that is itself a
    container: an array key decodes to a list, which is unhashable, so the map
    cannot be reassembled. In practice only ``tuple`` reaches this position --
    list and dict are unhashable and so can never be dict keys -- but this is
    written as an allowlist of the scalars the format can represent, which stays
    correct if a hashable container type is ever added to the encoder.

    ``bytes`` is here and absent from :func:`json.dumps`'s list because msgpack
    has a genuine scalar encoding for it (``bin``) and it round-trips; the point
    of the refusal is representability, not matching JSON's set exactly.
    """
    return key is None or isinstance(key, (bool, int, float, str, bytes))


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
        if not _key_ok(key):
            raise TypeError(
                "keys must be str, int, float, bool, bytes or None, "
                f"not {type(key).__name__}"
            )
        _pack(key, out)
        _pack(value, out)
