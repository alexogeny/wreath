"""The native protobuf codec is byte-for-byte the pure codec.

`src/wreath/_pure/protobuf.py` stays the reference implementation and the parity
contract; `src/wreath/_native/protobuf.c` is a faster twin of it. Every case here
asserts the two produce identical bytes on the way out and identical values on
the way in, so a divergence fails as a parity bug rather than as a mysterious
wire-format change at a peer.

Parity is asserted at the *plan* boundary — `encode_values` / `decode_values` —
because that is where the twin actually is. Object construction happens only in
Python and has nothing to diverge.

The boundary cases matter more than the shapes. A varint's length is a property
of the value, so every width transition is a branch that can be off by one in
exactly one implementation; the same is true of the sign handling, of packed
versus unpacked repeated, and of the -0.0 that must not be mistaken for a
default.
"""

from __future__ import annotations

import enum
import math

import pytest

from wreath._pure import protobuf as pure
from wreath.protobuf import field, message

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


def _same_bytes(values: list, unknown: bytes = b"") -> bytes:
    """Assert both encoders agree, and return the bytes for spec assertions."""
    expected = pure.encode_values(PLAN, values, unknown)
    actual = native_encode(PLAN, values, unknown)
    assert actual == expected, f"native diverged from pure encoding {values!r}"
    return expected


def _same_values(raw: bytes) -> tuple:
    """Assert both decoders agree, and return the decoded pair."""
    expected = pure.decode_values(PLAN, raw)
    actual = native_decode(PLAN, raw)
    assert actual == expected, f"native diverged from pure decoding {raw!r}"
    return expected


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
    _same_bytes(_with(0, value))


@pytest.mark.parametrize(
    "value", [0, 1, -1, 2**63 - 1, -(2**63), 2**40, -(2**40)]
)
def test_int64_width_transitions(value: int) -> None:
    _same_bytes(_with(1, value))


@pytest.mark.parametrize("value", [0, 1, 2**32 - 1, 2**16])
def test_uint32_width_transitions(value: int) -> None:
    _same_bytes(_with(2, value))


@pytest.mark.parametrize("value", [0, 1, 2**64 - 1, 2**63])
def test_uint64_reaches_the_very_top(value: int) -> None:
    _same_bytes(_with(3, value))


@pytest.mark.parametrize(
    "value", [0, -1, 1, -2, 2, 2**31 - 1, -(2**31)]
)
def test_sint32_zigzag(value: int) -> None:
    _same_bytes(_with(4, value))


@pytest.mark.parametrize(
    "value", [0, -1, 1, 2**63 - 1, -(2**63)]
)
def test_sint64_zigzag(value: int) -> None:
    _same_bytes(_with(5, value))


@pytest.mark.parametrize("index,value", [(6, 2**32 - 1), (8, 2**64 - 1)])
def test_unsigned_fixed_width_tops(index: int, value: int) -> None:
    _same_bytes(_with(index, value))


@pytest.mark.parametrize("index,value", [(7, -(2**31)), (9, -(2**63))])
def test_signed_fixed_width_bottoms(index: int, value: int) -> None:
    _same_bytes(_with(index, value))


@pytest.mark.parametrize(
    "value",
    [1.5, -1.5, 0.1, 1e308, 1e-308, math.inf, -math.inf, -0.0],
)
def test_double_values_including_negative_zero(value: float) -> None:
    # -0.0 must survive: it is not the proto3 default, because its bit pattern
    # differs and encoding it as 0.0 would lose the sign.
    _same_bytes(_with(10, value))


def test_double_nan() -> None:
    raw = _same_bytes(_with(10, math.nan))
    assert raw != b""


@pytest.mark.parametrize("value", [0.5, -0.5, 1e38, -0.0, math.inf])
def test_float_values(value: float) -> None:
    _same_bytes(_with(11, value))


def test_bool_true_is_written_and_false_is_not() -> None:
    assert _same_bytes(_with(12, True)) != b""
    assert _same_bytes(_with(12, False)) == b""


@pytest.mark.parametrize(
    "value", ["", "a", "snø", "\U0001f600", "x" * 200, "y" * 20000]
)
def test_string_length_transitions(value: str) -> None:
    _same_bytes(_with(13, value))


@pytest.mark.parametrize("value", [b"", b"\x00", b"\xff" * 300])
def test_bytes_length_transitions(value: bytes) -> None:
    _same_bytes(_with(14, value))


def test_enum_members_and_an_unknown_integer() -> None:
    _same_bytes(_with(15, int(Quality.GOOD)))
    _same_bytes(_with(15, 99))


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


def test_decoding_a_fully_populated_message_agrees() -> None:
    values = [
        -7, -(2**40), 4294967295, 2**64 - 1, -3, 3,
        4294967295, -2147483648, 2**64 - 1, -(2**63),
        1.25, 0.5, True, "hello", b"\x01\x02", int(Quality.GOOD),
        [1, 2, 3], [4, 5], ["p", "q"], ([9], b""), [([1], b"")], {"k": 2},
    ]
    _same_values(pure.encode_values(PLAN, values))


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
def test_both_implementations_refuse_the_same_malformed_buffers(raw: bytes) -> None:
    # The same class on both sides is the point: `wreath.protobuf` hands the C
    # module the exception the pure twin already raises, so a caller has one
    # thing to catch whichever implementation is loaded.
    with pytest.raises(pure.ProtobufDecodeError):
        pure.decode_values(PLAN, raw)
    with pytest.raises(pure.ProtobufDecodeError):
        native_decode(PLAN, raw)


@pytest.mark.parametrize(
    "index,value",
    [(0, 2**31), (0, -(2**31) - 1), (2, -1), (2, 2**32), (3, -1), (3, 2**64)],
)
def test_both_implementations_refuse_out_of_range_values(index: int, value: int) -> None:
    with pytest.raises(ValueError):
        pure.encode_values(PLAN, _with(index, value))
    with pytest.raises(ValueError):
        native_encode(PLAN, _with(index, value))
