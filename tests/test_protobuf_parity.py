"""`_native/protobuf.c` against the protobuf wire format, byte for byte.

This used to assert that two Wreath codecs produced identical bytes, which two
codecs written from one reading of the format do happily while emitting bytes no
peer accepts. `_wire` below is instead an encoder written straight from the
specification — tag byte, varint, zigzag, little-endian fixed, length prefix —
and every scalar case asserts the codec emits exactly what it produces.

The rules `_wire` encodes, from <https://protobuf.dev/programming-guides/encoding/>:

* a field is `(number << 3) | wire_type`, itself a varint;
* wire types are 0 varint, 1 64-bit LE, 2 length-delimited, 5 32-bit LE;
* a varint is base-128 little-endian with the high bit as the continuation
  flag, and a negative `int32`/`int64` is its two's-complement *64-bit* value,
  which is why -1 costs ten bytes;
* `sint32`/`sint64` are zigzag: `(n << 1) ^ (n >> 63)`, so small magnitudes stay
  short whichever sign they have;
* in proto3 a scalar equal to its zero is **not emitted at all**, which is what
  makes `_defaults()` encode to nothing.

Assertions are at the *plan* boundary — `encode_values` / `decode_values` —
because that is what the C implements. Object construction happens in Python.

The boundary cases matter more than the shapes. A varint's length is a property
of the value, so every width transition is a branch that can be off by one; the
same is true of the sign handling, of packed versus unpacked repeated, and of
the -0.0 that must not be mistaken for a default.
"""

from __future__ import annotations

import enum
import math
import struct

import pytest

from wreath.protobuf import ProtobufDecodeError, field, message

_core = pytest.importorskip("wreath._native._core")
native_encode = _core.protobuf_encode
native_decode = _core.protobuf_decode

# The C side raises whatever it was configured with; importing wreath.protobuf
# above has already handed it the real exception class.


class Quality(enum.IntEnum):
    UNKNOWN = 0
    GOOD = 1


@message
class Inner:
    value: int = field(1)


@message
class Everything:
    i32: int = field(1, kind="int32")
    i64: int = field(2, kind="int64")
    u32: int = field(3, kind="uint32")
    u64: int = field(4, kind="uint64")
    s32: int = field(5, kind="sint32")
    s64: int = field(6, kind="sint64")
    fx32: int = field(7, kind="fixed32")
    sfx32: int = field(8, kind="sfixed32")
    fx64: int = field(9, kind="fixed64")
    sfx64: int = field(10, kind="sfixed64")
    dbl: float = field(11)
    flt: float = field(12, kind="float")
    flag: bool = field(13)
    text: str = field(14)
    blob: bytes = field(15)
    quality: Quality = field(16)
    packed: list[int] = field(17)
    loose: list[int] = field(18, packed=False)
    names: list[str] = field(19)
    inner: Inner | None = field(20)
    children: list[Inner] = field(21)
    counts: dict[str, int] = field(22)


PLAN = Everything.__wreath_protobuf_plan__[0]
INNER_PLAN = Inner.__wreath_protobuf_plan__[0]


def _defaults() -> list:
    """A value list holding every field's proto3 zero, in plan order."""
    return [
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0.0, 0.0, False, "", b"", 0,
        [], [], [], None, [], {},
    ]


def _with(index: int, value: object) -> list:
    values = _defaults()
    values[index] = value
    return values


def _varint(value: int) -> bytes:
    """Base-128 little-endian, high bit as the continuation flag."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _tag(number: int, wire_type: int) -> bytes:
    return _varint((number << 3) | wire_type)


#: index in the value list -> (field number, how the payload is written).
#: `"varint"` takes the two's-complement 64-bit form for a negative, which is
#: what makes int32 -1 ten bytes rather than five.
_SCALARS: dict[int, tuple[int, str]] = {
    0: (1, "varint"), 1: (2, "varint"), 2: (3, "varint"), 3: (4, "varint"),
    4: (5, "zigzag"), 5: (6, "zigzag"),
    6: (7, "fixed32"), 7: (8, "fixed32"), 8: (9, "fixed64"), 9: (10, "fixed64"),
    10: (11, "double"), 11: (12, "float"), 12: (13, "varint"),
    13: (14, "string"), 14: (15, "bytes"), 15: (16, "varint"),
}


def _wire(index: int, value: object) -> bytes:
    """One scalar field on the wire, from the specification."""
    number, how = _SCALARS[index]
    if how in ("varint", "zigzag"):
        raw = int(value)
        if how == "zigzag":
            raw = (raw << 1) ^ (raw >> 63)
        return _tag(number, 0) + _varint(raw & 0xFFFFFFFFFFFFFFFF)
    if how == "fixed32":
        return _tag(number, 5) + int(value).to_bytes(4, "little", signed=int(value) < 0)
    if how == "fixed64":
        return _tag(number, 1) + int(value).to_bytes(8, "little", signed=int(value) < 0)
    if how == "double":
        return _tag(number, 1) + struct.pack("<d", value)
    if how == "float":
        return _tag(number, 5) + struct.pack("<f", value)
    payload = value.encode("utf-8") if how == "string" else bytes(value)  # type: ignore[union-attr]
    return _tag(number, 2) + _varint(len(payload)) + payload


def _scalar(index: int, value: object) -> bytes:
    """Encode one scalar field and hold it to `_wire`, or to nothing if default.

    proto3 omits a scalar equal to its zero, so the expectation for a default is
    the empty string -- and that is a real assertion, not a vacuous one: an
    encoder that emitted an explicit zero would fail here.

    "Equal to its zero" is compared *by bits* for a float. `-0.0 == 0.0` is true
    in Python and false on the wire: the sign bit differs, and dropping the
    field would hand a peer `0.0` for a value that was not.
    """
    default = _defaults()[index]
    if isinstance(value, float):
        is_default = struct.pack("<d", value) == struct.pack("<d", 0.0)
    else:
        is_default = type(value) is type(default) and value == default
    expected = b"" if is_default else _wire(index, value)
    actual = native_encode(PLAN, _with(index, value), b"")
    assert actual == expected, f"field {_SCALARS[index][0]} = {value!r}"
    return actual


def _same_bytes(values: list, unknown: bytes = b"") -> bytes:
    """Encode, and hold the result to a decode round trip.

    Weaker than `_scalar`, and used only for the composite fields -- repeated,
    map, nested message -- whose spec encoding is not one line. A round trip
    catches a length prefix or a packed/unpacked confusion; it would not catch
    the codec agreeing with itself on a wrong tag, which is what `_scalar`
    exists for.
    """
    encoded = native_encode(PLAN, values, unknown)
    decoded, _unknown = native_decode(PLAN, encoded)
    assert list(decoded) == values, f"round trip lost {values!r}"
    return encoded


def _same_values(raw: bytes) -> tuple:
    """Decode `raw`, and hold the result to an encode round trip."""
    decoded = native_decode(PLAN, raw)
    values, _unknown = decoded
    assert native_decode(PLAN, native_encode(PLAN, list(values), b""))[0] == values
    return decoded


# -- encoding parity --------------------------------------------------------


def test_an_all_default_message_encodes_to_nothing_in_both() -> None:
    assert _same_bytes(_defaults()) == b""


@pytest.mark.parametrize(
    "value",
    [
        0, 1, 127, 128, 255, 256, 16383, 16384,          # varint width steps
        2**31 - 1, -1, -2, -128, -129, -(2**31),
    ],
)
def test_int32_width_transitions(value: int) -> None:
    _scalar(0, value)


@pytest.mark.parametrize(
    "value", [0, 1, -1, 2**63 - 1, -(2**63), 2**40, -(2**40)]
)
def test_int64_width_transitions(value: int) -> None:
    _scalar(1, value)


@pytest.mark.parametrize("value", [0, 1, 2**32 - 1, 2**16])
def test_uint32_width_transitions(value: int) -> None:
    _scalar(2, value)


@pytest.mark.parametrize("value", [0, 1, 2**64 - 1, 2**63])
def test_uint64_reaches_the_very_top(value: int) -> None:
    _scalar(3, value)


@pytest.mark.parametrize(
    "value", [0, -1, 1, -2, 2, 2**31 - 1, -(2**31)]
)
def test_sint32_zigzag(value: int) -> None:
    _scalar(4, value)


@pytest.mark.parametrize(
    "value", [0, -1, 1, 2**63 - 1, -(2**63)]
)
def test_sint64_zigzag(value: int) -> None:
    _scalar(5, value)


@pytest.mark.parametrize("index,value", [(6, 2**32 - 1), (8, 2**64 - 1)])
def test_unsigned_fixed_width_tops(index: int, value: int) -> None:
    _scalar(index, value)


@pytest.mark.parametrize("index,value", [(7, -(2**31)), (9, -(2**63))])
def test_signed_fixed_width_bottoms(index: int, value: int) -> None:
    _scalar(index, value)


@pytest.mark.parametrize(
    "value",
    [1.5, -1.5, 0.1, 1e308, 1e-308, math.inf, -math.inf, -0.0],
)
def test_double_values_including_negative_zero(value: float) -> None:
    # -0.0 must survive: it is not the proto3 default, because its bit pattern
    # differs and encoding it as 0.0 would lose the sign.
    _scalar(10, value)


def test_double_nan() -> None:
    raw = native_encode(PLAN, _with(10, math.nan), b"")
    # A literal would pin CPython's quiet-NaN payload rather than anything the
    # codec owes; what it owes is a float 64 field carrying the double through.
    assert raw[:1] == _tag(11, 1)
    assert math.isnan(struct.unpack("<d", raw[1:])[0])


@pytest.mark.parametrize("value", [0.5, -0.5, 1e38, -0.0, math.inf])
def test_float_values(value: float) -> None:
    _scalar(11, value)


def test_bool_true_is_written_and_false_is_not() -> None:
    assert _scalar(12, True) == _tag(13, 0) + b"\x01"
    assert _scalar(12, False) == b""


@pytest.mark.parametrize(
    "value", ["", "a", "snø", "\U0001f600", "x" * 200, "y" * 20000]
)
def test_string_length_transitions(value: str) -> None:
    _scalar(13, value)


@pytest.mark.parametrize("value", [b"", b"\x00", b"\xff" * 300])
def test_bytes_length_transitions(value: bytes) -> None:
    _scalar(14, value)


def test_enum_members_and_an_unknown_integer() -> None:
    _scalar(15, int(Quality.GOOD))
    _scalar(15, 99)


@pytest.mark.parametrize(
    "items", [[], [0], [1, 2, 3], list(range(300)), [-1, 2**40]]
)
def test_packed_repeated(items: list) -> None:
    _same_bytes(_with(16, items))


@pytest.mark.parametrize("items", [[], [1], [1, 2, 3]])
def test_unpacked_repeated(items: list) -> None:
    _same_bytes(_with(17, items))


@pytest.mark.parametrize("items", [[], ["a"], ["a", "", "ccc"]])
def test_repeated_strings(items: list) -> None:
    _same_bytes(_with(18, items))


def test_nested_message_present_absent_and_empty() -> None:
    _same_bytes(_with(19, None))
    _same_bytes(_with(19, ([0], b"")))
    _same_bytes(_with(19, ([7], b"")))


def test_repeated_nested_messages() -> None:
    _same_bytes(_with(20, [([1], b""), ([2], b"")]))


@pytest.mark.parametrize(
    "mapping", [{}, {"a": 1}, {"": 0}, {"a": 1, "b": 2, "c": 3}]
)
def test_maps(mapping: dict) -> None:
    _same_bytes(_with(21, mapping))


def test_unknown_field_bytes_are_appended_identically() -> None:
    _same_bytes(_defaults(), b"\xfa\x01\x02hi")
    _same_bytes(_with(0, 5), b"\xfa\x01\x02hi")


def test_a_fully_populated_message_agrees() -> None:
    values = [
        -7, -(2**40), 4294967295, 2**64 - 1, -3, 3,
        4294967295, -2147483648, 2**64 - 1, -(2**63),
        1.25, 0.5, True, "hello", b"\x01\x02", int(Quality.GOOD),
        [1, 2, 3], [4, 5], ["p", "q"], ([9], b""), [([1], b"")], {"k": 2},
    ]
    _same_bytes(values, b"\xfa\x01\x01z")


# -- decoding parity --------------------------------------------------------


def test_decoding_an_empty_buffer_agrees() -> None:
    _same_values(b"")


def test_a_fully_populated_message_survives_a_round_trip() -> None:
    values = [
        -7, -(2**40), 4294967295, 2**64 - 1, -3, 3,
        4294967295, -2147483648, 2**64 - 1, -(2**63),
        1.25, 0.5, True, "hello", b"\x01\x02", int(Quality.GOOD),
        [1, 2, 3], [4, 5], ["p", "q"], ([9], b""), [([1], b"")], {"k": 2},
    ]
    _same_values(native_encode(PLAN, values, b""))


def test_decoding_the_unpacked_form_of_a_packed_field_agrees() -> None:
    _same_values(b"\x88\x01\x01\x88\x01\x02")


def test_decoding_the_packed_form_of_an_unpacked_field_agrees() -> None:
    _same_values(b"\x92\x01\x02\x01\x02")


def test_decoding_unknown_fields_of_every_wire_type_agrees() -> None:
    raw = (
        b"\x08\x01"
        b"\xf8\x01\x2a"
        b"\xfd\x01\x01\x00\x00\x00"
        b"\x81\x02\x01\x00\x00\x00\x00\x00\x00\x00"
        b"\x8a\x02\x01z"
    )
    _same_values(raw)


def test_decoding_a_repeated_field_twice_accumulates_in_both() -> None:
    # Field 18 unpacked: tag (18 << 3) | 0 == 144, itself a two-byte varint.
    _same_values(b"\x90\x01\x01\x90\x01\x02")


def test_a_later_scalar_overwrites_an_earlier_one_in_both() -> None:
    # Last-one-wins for a non-repeated field is specified behaviour, not an
    # accident, and both implementations must land on the same value.
    _same_values(b"\x08\x01\x08\x02")


# -- refusal parity ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"\x08" + b"\x80" * 11 + b"\x01",   # varint over ten bytes
        b"\x08\x80",                        # truncated varint
        b"\x72\xc8\x01ab",                  # length past the end
        b"\x3d\x01\x00",                    # truncated fixed32
        b"\x00\x01",                        # field number zero
        b"\x0b",                            # group wire type
        b"\x0e",                            # unknown wire type
        b"\x72\x01\xff",                    # invalid UTF-8 in a string
    ],
)
def test_malformed_buffers_are_refused(raw: bytes) -> None:
    # `ProtobufDecodeError` and not some C-invented class: `wreath.protobuf`
    # hands the codec the exception at import, so this is the type a caller
    # catches and the type raised.
    with pytest.raises(ProtobufDecodeError):
        native_decode(PLAN, raw)


@pytest.mark.parametrize(
    "index,value",
    [(0, 2**31), (0, -(2**31) - 1), (2, -1), (2, 2**32), (3, -1), (3, 2**64)],
)
def test_out_of_range_values_are_refused(index: int, value: int) -> None:
    """A value the declared width cannot hold is refused, not truncated.

    Silently wrapping is the failure mode that reaches a peer as a plausible
    wrong number rather than as an error.
    """
    with pytest.raises(ValueError):
        native_encode(PLAN, _with(index, value), b"")
