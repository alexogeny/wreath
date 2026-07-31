"""The protobuf wire codec, in Python — the reference implementation.

This module is bytes-to-values and values-to-bytes only. It knows nothing about
dataclasses: `wreath.protobuf` compiles a declaration to a *plan* once, at class
creation, and this module walks that plan. The split is deliberate and is what
lets `src/wreath/_native/protobuf.c` be a twin of it — the C side reimplements
exactly this file, and object construction stays in Python where it belongs.

`tests/test_protobuf_parity.py` holds the two byte-for-byte.

The plan is a tuple of `(number, kind, flags, subplan)` rows, all plain ints
except `subplan`, so it crosses into C without any conversion:

    number   1..536870911, already validated by the declaration layer
    kind     one of the KIND_* codes below
    flags    FLAG_* bits
    subplan  a nested plan tuple for KIND_MESSAGE / map fields, else None

Values are passed and returned as a list in plan order, so position *is*
identity and no name lookup happens per field.
"""

from __future__ import annotations

import struct
from typing import Any

__all__ = [
    "FLAG_MAP",
    "FLAG_OPTIONAL",
    "FLAG_PACKED",
    "FLAG_REPEATED",
    "KIND_BOOL",
    "KIND_BYTES",
    "KIND_DOUBLE",
    "KIND_ENUM",
    "KIND_FIXED32",
    "KIND_FIXED64",
    "KIND_FLOAT",
    "KIND_INT32",
    "KIND_INT64",
    "KIND_MESSAGE",
    "KIND_SFIXED32",
    "KIND_SFIXED64",
    "KIND_SINT32",
    "KIND_SINT64",
    "KIND_STRING",
    "KIND_UINT32",
    "KIND_UINT64",
    "ProtobufDecodeError",
    "decode_values",
    "encode_values",
]

# -- kinds ------------------------------------------------------------------
# Stable integer codes: the C twin switches on these, so the numbering is part
# of the contract between the two implementations, not an implementation detail.

KIND_INT32 = 1
KIND_INT64 = 2
KIND_UINT32 = 3
KIND_UINT64 = 4
KIND_SINT32 = 5
KIND_SINT64 = 6
KIND_BOOL = 7
KIND_ENUM = 8
KIND_FIXED64 = 9
KIND_SFIXED64 = 10
KIND_DOUBLE = 11
KIND_FIXED32 = 12
KIND_SFIXED32 = 13
KIND_FLOAT = 14
KIND_STRING = 15
KIND_BYTES = 16
KIND_MESSAGE = 17

FLAG_REPEATED = 1
FLAG_PACKED = 2
FLAG_OPTIONAL = 4
FLAG_MAP = 8

WIRE_VARINT = 0
WIRE_I64 = 1
WIRE_LEN = 2
WIRE_SGROUP = 3
WIRE_EGROUP = 4
WIRE_I32 = 5

#: A varint encodes at most a 64-bit value, which needs ten 7-bit groups. An
#: eleventh continuation byte is malformed, and reading it is how a decoder
#: walks off the end of a buffer a peer controls.
MAX_VARINT_BYTES = 10

_VARINT_KINDS = frozenset(
    {
        KIND_INT32,
        KIND_INT64,
        KIND_UINT32,
        KIND_UINT64,
        KIND_SINT32,
        KIND_SINT64,
        KIND_BOOL,
        KIND_ENUM,
    }
)
_I64_KINDS = frozenset({KIND_FIXED64, KIND_SFIXED64, KIND_DOUBLE})
_I32_KINDS = frozenset({KIND_FIXED32, KIND_SFIXED32, KIND_FLOAT})

#: Inclusive value bounds per kind, checked on encode. Encoding a value the
#: declared kind cannot hold would silently truncate at the peer.
_BOUNDS: dict[int, tuple[int, int]] = {
    KIND_INT32: (-(2**31), 2**31 - 1),
    KIND_SFIXED32: (-(2**31), 2**31 - 1),
    KIND_SINT32: (-(2**31), 2**31 - 1),
    KIND_INT64: (-(2**63), 2**63 - 1),
    KIND_SFIXED64: (-(2**63), 2**63 - 1),
    KIND_SINT64: (-(2**63), 2**63 - 1),
    KIND_UINT32: (0, 2**32 - 1),
    KIND_FIXED32: (0, 2**32 - 1),
    KIND_UINT64: (0, 2**64 - 1),
    KIND_FIXED64: (0, 2**64 - 1),
    KIND_ENUM: (-(2**31), 2**31 - 1),
}


class ProtobufDecodeError(ValueError):
    """A buffer could not be parsed as the declared message.

    Always raised by name rather than allowed to surface as an `IndexError` or a
    `UnicodeDecodeError`: this codec sits in front of bytes a peer controls, and
    a caller needs one exception type to catch and turn into a 400.
    """


def wire_type_for(kind: int) -> int:
    """The wire type a field of `kind` occupies. Not valid for packed bodies."""
    if kind in _VARINT_KINDS:
        return WIRE_VARINT
    if kind in _I64_KINDS:
        return WIRE_I64
    if kind in _I32_KINDS:
        return WIRE_I32
    return WIRE_LEN


def _packable(kind: int) -> bool:
    """Whether repeated fields of `kind` may use the packed representation.

    Only fixed-width and varint scalars pack; a length-delimited value already
    carries its own length and gains nothing.
    """
    return kind in _VARINT_KINDS or kind in _I64_KINDS or kind in _I32_KINDS


# -- encoding ---------------------------------------------------------------


def _put_varint(out: bytearray, value: int) -> None:
    """Append `value` as a base-128 varint. `value` must already be unsigned."""
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def _unsigned(kind: int, value: int) -> int:
    """The unsigned 64-bit pattern a varint field of `kind` writes."""
    if kind in (KIND_SINT32, KIND_SINT64):
        # ZigZag: interleave positives and negatives so small magnitudes stay
        # short. Without it every negative int costs the full ten bytes.
        bits = 32 if kind == KIND_SINT32 else 64
        return ((value << 1) ^ (value >> (bits - 1))) & (2**bits - 1)
    if value < 0:
        # int32 negatives are sign-extended to 64 bits by the specification, so
        # they occupy ten bytes exactly as int64 negatives do.
        return value + 2**64
    return value


def _check_bounds(kind: int, value: int) -> None:
    bounds = _BOUNDS.get(kind)
    if bounds is not None and not (bounds[0] <= value <= bounds[1]):
        raise ValueError(
            f"{value} is out of range for the declared protobuf kind "
            f"(expected {bounds[0]}..{bounds[1]})"
        )


def _put_tag(out: bytearray, number: int, wire: int) -> None:
    _put_varint(out, (number << 3) | wire)


def _put_scalar(out: bytearray, kind: int, value: Any) -> None:
    """Append the body of one scalar, with no tag."""
    if kind in _VARINT_KINDS:
        if kind == KIND_BOOL:
            out.append(1 if value else 0)
            return
        number = int(value)
        _check_bounds(kind, number)
        _put_varint(out, _unsigned(kind, number))
    elif kind == KIND_DOUBLE:
        out += struct.pack("<d", value)
    elif kind == KIND_FLOAT:
        out += struct.pack("<f", value)
    elif kind in (KIND_FIXED64, KIND_SFIXED64):
        _check_bounds(kind, int(value))
        out += struct.pack("<Q" if kind == KIND_FIXED64 else "<q", int(value))
    elif kind in (KIND_FIXED32, KIND_SFIXED32):
        _check_bounds(kind, int(value))
        out += struct.pack("<I" if kind == KIND_FIXED32 else "<i", int(value))
    else:  # pragma: no cover - callers route LEN kinds through _put_delimited
        raise ValueError(f"kind {kind} is not a scalar")


def _put_delimited(out: bytearray, data: bytes) -> None:
    _put_varint(out, len(data))
    out += data


def _is_default(kind: int, value: Any) -> bool:
    """Whether proto3 implicit presence omits this value.

    A field holding its type's zero is not written, which is why a default and
    an absent field are indistinguishable on the wire — the reason explicit
    presence (`X | None`) exists.
    """
    if kind == KIND_BOOL:
        return value is False
    if kind == KIND_STRING:
        return value == ""
    if kind == KIND_BYTES:
        return not value
    if kind in (KIND_DOUBLE, KIND_FLOAT):
        # `0.0 == -0.0` is True, and protobuf treats -0.0 as non-default: its
        # bit pattern differs and round-tripping it as 0.0 would lose the sign.
        return value == 0.0 and not _is_negative_zero(value)
    return value == 0


def _is_negative_zero(value: float) -> bool:
    return value == 0.0 and struct.pack("<d", value)[7] == 0x80


def encode_values(plan: tuple, values: list, unknown: bytes = b"") -> bytes:
    """Encode `values` (in plan order) against `plan`, appending `unknown`.

    `unknown` is the raw bytes of fields this build did not recognise on the way
    in. Re-emitting them is what lets a message round-trip through an
    intermediary built against an older declaration without losing data.
    """
    out = bytearray()
    for index, row in enumerate(plan):
        number, kind, flags, subplan = row
        value = values[index]
        if value is None:
            # Both an unset optional and an absent message: nothing on the wire.
            continue
        if flags & FLAG_MAP:
            _encode_map(out, number, subplan, value)
        elif flags & FLAG_REPEATED:
            _encode_repeated(out, number, kind, flags, subplan, value)
        elif kind == KIND_MESSAGE:
            _put_tag(out, number, WIRE_LEN)
            _put_delimited(out, encode_values(subplan, value[0], value[1]))
        elif flags & FLAG_OPTIONAL or not _is_default(kind, value):
            _encode_single(out, number, kind, value)
    out += unknown
    return bytes(out)


def _encode_single(out: bytearray, number: int, kind: int, value: Any) -> None:
    if kind == KIND_STRING:
        _put_tag(out, number, WIRE_LEN)
        _put_delimited(out, value.encode("utf-8"))
    elif kind == KIND_BYTES:
        _put_tag(out, number, WIRE_LEN)
        _put_delimited(out, bytes(value))
    else:
        _put_tag(out, number, wire_type_for(kind))
        _put_scalar(out, kind, value)


def _encode_repeated(
    out: bytearray, number: int, kind: int, flags: int, subplan: Any, items: list
) -> None:
    if not items:
        return
    if kind == KIND_MESSAGE:
        for item in items:
            _put_tag(out, number, WIRE_LEN)
            _put_delimited(out, encode_values(subplan, item[0], item[1]))
        return
    if flags & FLAG_PACKED and _packable(kind):
        body = bytearray()
        for item in items:
            _put_scalar(body, kind, item)
        _put_tag(out, number, WIRE_LEN)
        _put_delimited(out, bytes(body))
        return
    for item in items:
        _encode_single(out, number, kind, item)


def _encode_map(out: bytearray, number: int, subplan: tuple, mapping: dict) -> None:
    """A map field is repeated messages of `{1: key, 2: value}`.

    Encoding it as its sugar-free form is what makes a wreath map wire-identical
    to a `map<k, v>` declared in a `.proto`.
    """
    for key, value in mapping.items():
        entry = bytearray()
        key_row, value_row = subplan
        if not _is_default(key_row[1], key):
            _encode_single(entry, 1, key_row[1], key)
        if value_row[1] == KIND_MESSAGE:
            _put_tag(entry, 2, WIRE_LEN)
            _put_delimited(entry, encode_values(value_row[3], value[0], value[1]))
        elif not _is_default(value_row[1], value):
            _encode_single(entry, 2, value_row[1], value)
        _put_tag(out, number, WIRE_LEN)
        _put_delimited(out, bytes(entry))


# -- decoding ---------------------------------------------------------------


def _get_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read one varint at `pos`; return `(value, next_pos)`.

    Bounded at `MAX_VARINT_BYTES` so a run of continuation bytes cannot walk the
    cursor past the buffer, and refused rather than truncated so a malformed
    length prefix never becomes a plausible-looking one.
    """
    result = 0
    shift = 0
    end = len(data)
    start = pos
    while True:
        if pos >= end:
            raise ProtobufDecodeError(
                f"buffer ends inside a varint that began at offset {start}"
            )
        if pos - start >= MAX_VARINT_BYTES:
            raise ProtobufDecodeError(
                f"varint at offset {start} exceeds {MAX_VARINT_BYTES} bytes"
            )
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if not byte & 0x80:
            return result, pos
        shift += 7


def _signed(kind: int, raw: int) -> int:
    """Interpret the unsigned varint `raw` as the declared kind."""
    if kind in (KIND_SINT32, KIND_SINT64):
        return (raw >> 1) ^ -(raw & 1)
    if kind in (KIND_INT32, KIND_ENUM):
        raw &= 0xFFFFFFFFFFFFFFFF
        if raw >= 2**63:
            raw -= 2**64
        # int32 on the wire is sign-extended to 64 bits; narrow it back.
        raw &= 0xFFFFFFFF
        return raw - 2**32 if raw >= 2**31 else raw
    if kind == KIND_INT64:
        return raw - 2**64 if raw >= 2**63 else raw
    if kind == KIND_UINT32:
        return raw & 0xFFFFFFFF
    return raw


def _take(data: bytes, pos: int, count: int, what: str) -> tuple[bytes, int]:
    end = pos + count
    if end > len(data):
        raise ProtobufDecodeError(
            f"{what} at offset {pos} needs {count} bytes, "
            f"but only {len(data) - pos} remain"
        )
    return data[pos:end], end


def _read_scalar(data: bytes, pos: int, kind: int) -> tuple[Any, int]:
    if kind in _VARINT_KINDS:
        raw, pos = _get_varint(data, pos)
        if kind == KIND_BOOL:
            return bool(raw), pos
        return _signed(kind, raw), pos
    if kind in _I64_KINDS:
        chunk, pos = _take(data, pos, 8, "a 64-bit field")
        if kind == KIND_DOUBLE:
            return struct.unpack("<d", chunk)[0], pos
        fmt = "<Q" if kind == KIND_FIXED64 else "<q"
        return struct.unpack(fmt, chunk)[0], pos
    chunk, pos = _take(data, pos, 4, "a 32-bit field")
    if kind == KIND_FLOAT:
        return struct.unpack("<f", chunk)[0], pos
    fmt = "<I" if kind == KIND_FIXED32 else "<i"
    return struct.unpack(fmt, chunk)[0], pos


def _skip(data: bytes, pos: int, wire: int) -> int:
    """Advance past one field body of `wire` type, for an unknown field."""
    if wire == WIRE_VARINT:
        _, pos = _get_varint(data, pos)
        return pos
    if wire == WIRE_I64:
        _, pos = _take(data, pos, 8, "an unknown 64-bit field")
        return pos
    if wire == WIRE_I32:
        _, pos = _take(data, pos, 4, "an unknown 32-bit field")
        return pos
    length, pos = _get_varint(data, pos)
    _, pos = _take(data, pos, length, "an unknown length-delimited field")
    return pos


def _defaults(plan: tuple) -> list:
    values: list = []
    for _number, kind, flags, _subplan in plan:
        if flags & FLAG_MAP:
            values.append({})
        elif flags & FLAG_REPEATED:
            values.append([])
        elif flags & FLAG_OPTIONAL or kind == KIND_MESSAGE:
            values.append(None)
        elif kind == KIND_STRING:
            values.append("")
        elif kind == KIND_BYTES:
            values.append(b"")
        elif kind == KIND_BOOL:
            values.append(False)
        elif kind in (KIND_DOUBLE, KIND_FLOAT):
            values.append(0.0)
        else:
            values.append(0)
    return values


def decode_values(plan: tuple, data: bytes) -> tuple[list, bytes]:
    """Decode `data` against `plan`; return `(values in plan order, unknown)`.

    Unknown fields are captured verbatim, tag included, and returned rather than
    discarded — see `encode_values` for why.
    """
    index = {row[0]: (position, row) for position, row in enumerate(plan)}
    values = _defaults(plan)
    unknown = bytearray()
    pos = 0
    end = len(data)
    while pos < end:
        tag_start = pos
        tag, pos = _get_varint(data, pos)
        number = tag >> 3
        wire = tag & 0x07
        if number == 0:
            raise ProtobufDecodeError(f"field number 0 at offset {tag_start}")
        if wire in (WIRE_SGROUP, WIRE_EGROUP):
            raise ProtobufDecodeError(
                f"group wire type {wire} at offset {tag_start}: groups are "
                "deprecated and this codec refuses them rather than guessing"
            )
        if wire > WIRE_I32:
            raise ProtobufDecodeError(
                f"unknown wire type {wire} at offset {tag_start}"
            )
        found = index.get(number)
        if found is None:
            pos = _skip(data, pos, wire)
            unknown += data[tag_start:pos]
            continue
        position, row = found
        pos = _read_field(data, pos, row, values, position, wire)
    return values, bytes(unknown)


def _read_field(
    data: bytes, pos: int, row: tuple, values: list, position: int, wire: int
) -> int:
    _number, kind, flags, subplan = row
    if flags & FLAG_MAP:
        body, pos = _read_len(data, pos)
        entries, _ = decode_values(subplan, body)
        values[position][entries[0]] = entries[1]
        return pos
    if kind == KIND_MESSAGE:
        body, pos = _read_len(data, pos)
        nested = decode_values(subplan, body)
        if flags & FLAG_REPEATED:
            values[position].append(nested)
        else:
            values[position] = nested
        return pos
    if kind in (KIND_STRING, KIND_BYTES):
        body, pos = _read_len(data, pos)
        value: Any = body
        if kind == KIND_STRING:
            try:
                value = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProtobufDecodeError(
                    f"a string field carried bytes that are not UTF-8: {exc}"
                ) from exc
        if flags & FLAG_REPEATED:
            values[position].append(value)
        else:
            values[position] = value
        return pos
    if flags & FLAG_REPEATED and wire == WIRE_LEN:
        # A packed body, regardless of what this build declared: proto3 requires
        # a parser to accept both representations for a repeated scalar.
        body, pos = _read_len(data, pos)
        inner = 0
        while inner < len(body):
            item, inner = _read_scalar(body, inner, kind)
            values[position].append(item)
        return pos
    item, pos = _read_scalar(data, pos, kind)
    if flags & FLAG_REPEATED:
        values[position].append(item)
    else:
        values[position] = item
    return pos


def _read_len(data: bytes, pos: int) -> tuple[bytes, int]:
    length, pos = _get_varint(data, pos)
    return _take(data, pos, length, "a length-delimited field")
