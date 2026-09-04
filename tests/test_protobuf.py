from __future__ import annotations

import enum
from typing import Any, cast

import pytest

from wreath.protobuf import (
    ProtobufDeclarationError,
    ProtobufDecodeError,
    decode,
    encode,
    field,
    message,
)


def test_a_duplicate_field_number_is_refused_naming_the_field() -> None:
    with pytest.raises(ProtobufDeclarationError) as caught:

        @message
        class Duplicate:
            first: int = field(1)
            second: int = field(1)

    text = str(caught.value)
    assert "1" in text
    assert "second" in text


def test_a_reserved_field_number_is_refused_naming_the_range() -> None:
    with pytest.raises(ProtobufDeclarationError) as caught:

        @message
        class Reserved:
            nope: int = field(19123)

    assert "19000" in str(caught.value)


@pytest.mark.parametrize("number", [0, -1, 536870912])
def test_a_field_number_outside_the_legal_span_is_refused(number: int) -> None:
    with pytest.raises(ProtobufDeclarationError):

        @message
        class OutOfRange:
            nope: int = field(number)


def test_a_type_with_no_wire_mapping_is_refused_naming_the_field() -> None:
    with pytest.raises(ProtobufDeclarationError) as caught:

        @message
        class Unmappable:
            when: complex = field(1)

    assert "when" in str(caught.value)


def test_a_kind_that_does_not_exist_is_refused_as_unknown_not_as_incompatible() -> None:
    with pytest.raises(ProtobufDeclarationError) as caught:

        @message
        class BadKind:
            n: int = field(1, kind="int24")

    text = str(caught.value)
    assert "unknown kind" in text
    assert "sint32" in text, "the refusal must name the kinds that do exist"


def test_a_kind_incompatible_with_the_annotation_says_so() -> None:
    with pytest.raises(ProtobufDeclarationError) as caught:

        @message
        class Mismatch:
            text: str = field(1, kind="sint32")

    assert "not compatible" in str(caught.value)


def test_a_kind_on_a_type_with_no_compatible_kinds_at_all_is_refused() -> None:
    with pytest.raises(ProtobufDeclarationError) as caught:

        @message
        class Unmappable:
            n: complex = field(1, kind="double")

    assert "not compatible" in str(caught.value)


class NoZero(enum.IntEnum):
    """Declared at module level on purpose.

    This module uses `from __future__ import annotations`, so annotations are
    stringified (PEP 563) and evaluate against module globals only — a type
    declared inside a test function is genuinely out of reach there, and that is
    a property of PEP 563 rather than of the decorator.
    """

    ONE = 1


class MapKeyEnum(enum.IntEnum):
    ZERO = 0


def test_an_enum_without_a_zero_member_is_refused() -> None:
    with pytest.raises(ProtobufDeclarationError) as caught:

        @message
        class Holder:
            value: NoZero = field(1)

    assert "zero" in str(caught.value).lower()


def test_a_field_without_a_number_is_refused() -> None:
    with pytest.raises(ProtobufDeclarationError) as caught:

        @message
        class Bare:
            n: int = 0

    assert "n" in str(caught.value)


@pytest.mark.parametrize(
    ("option", "value", "correct_form"),
    [
        ("kind", ["int32"], "string"),
        ("packed", 1, "boolean"),
        ("oneof", 7, "non-empty string"),
        ("oneof", "", "non-empty string"),
    ],
)
def test_field_options_refuse_inexact_types_at_declaration(
    option: str, value: object, correct_form: str
) -> None:
    kwargs = {option: value}
    unsafe_field = cast(Any, field)
    with pytest.raises(ProtobufDeclarationError) as caught:

        @message
        class InvalidOption:
            value: int | None = unsafe_field(1, **kwargs)

    text = str(caught.value)
    assert option in text
    assert correct_form in text


def test_packed_is_refused_on_a_singular_field() -> None:
    with pytest.raises(ProtobufDeclarationError, match="packed.*repeated"):

        @message
        class SingularPacked:
            value: int = field(1, packed=True)


@message
class PackedChild:
    value: int = field(1)


@pytest.mark.parametrize("annotation", [list[str], list[bytes], list[PackedChild]])
def test_packed_is_refused_on_a_non_packable_repeated_field(annotation: object) -> None:
    namespace = {
        "__annotations__": {"values": annotation},
        "values": field(1, packed=False),
    }
    with pytest.raises(ProtobufDeclarationError, match="packed.*repeated"):
        message(type("InvalidPacked", (), namespace))


def test_an_explicit_packed_option_is_valid_on_a_repeated_scalar() -> None:
    namespace = {
        "__annotations__": {"values": list[int]},
        "values": field(1, packed=False),
    }
    message(type("ValidPacked", (), namespace))


@pytest.mark.parametrize("annotation", [list[str], list[PackedChild]])
def test_non_packable_repeated_fields_compile_without_a_packed_option(
    annotation: object,
) -> None:
    namespace = {
        "__annotations__": {"values": annotation},
        "values": field(1),
    }
    message(type("ValidRepeated", (), namespace))


@pytest.mark.parametrize(
    "annotation",
    [list[int | None], list[list[int]], list[dict[str, int]]],
)
def test_a_repeated_item_must_be_a_non_container_non_optional_value(
    annotation: object,
) -> None:
    namespace = {
        "__annotations__": {"values": annotation},
        "values": field(1),
    }
    with pytest.raises(ProtobufDeclarationError, match="repeated item"):
        message(type("InvalidRepeatedItem", (), namespace))


@pytest.mark.parametrize("annotation", [dict[bytes, int], dict[MapKeyEnum, int]])
def test_a_map_key_must_be_a_protobuf_legal_key_type(annotation: object) -> None:
    namespace = {
        "__annotations__": {"values": annotation},
        "values": field(1),
    }
    with pytest.raises(ProtobufDeclarationError, match="map key"):
        message(type("InvalidMapKey", (), namespace))


@pytest.mark.parametrize("annotation", [dict[list[int], int], dict[int | None, int]])
def test_a_map_key_cannot_be_repeated_or_optional(annotation: object) -> None:
    namespace = {
        "__annotations__": {"values": annotation},
        "values": field(1),
    }
    with pytest.raises(ProtobufDeclarationError, match="map key"):
        message(type("InvalidMapKeyShape", (), namespace))


@pytest.mark.parametrize("option", [{"kind": "int32"}, {"packed": False}])
def test_scalar_field_options_are_refused_on_a_map(option: dict[str, object]) -> None:
    unsafe_field = cast(Any, field)
    namespace = {
        "__annotations__": {"values": dict[str, int]},
        "values": unsafe_field(1, **option),
    }
    with pytest.raises(
        ProtobufDeclarationError,
        match=(
            "kind is not valid on a map"
            if "kind" in option
            else "packed is only valid for a repeated scalar field"
        ),
    ):
        message(type("InvalidMapOption", (), namespace))


def test_legal_map_key_types_still_compile() -> None:
    for index, annotation in enumerate((dict[int, int], dict[bool, int], dict[str, int])):
        namespace = {
            "__annotations__": {"values": annotation},
            "values": field(1),
        }
        message(type(f"ValidMap{index}", (), namespace))


def test_a_valid_oneof_group_name_still_compiles() -> None:
    namespace = {
        "__annotations__": {"value": int | None},
        "value": field(1, oneof="choice"),
    }
    message(type("ValidOneof", (), namespace))


@pytest.mark.parametrize(
    "annotation",
    [dict[str, list[int]], dict[str, dict[str, int]], dict[str, int | None]],
)
def test_a_map_value_cannot_be_repeated_nested_or_optional(annotation: object) -> None:
    namespace = {
        "__annotations__": {"values": annotation},
        "values": field(1),
    }
    with pytest.raises(ProtobufDeclarationError, match="map value"):
        message(type("InvalidMapValue", (), namespace))


@message
class Scalars:
    i32: int = field(1, kind="int32")
    i64: int = field(2, kind="int64")
    u32: int = field(3, kind="uint32")
    s32: int = field(4, kind="sint32")
    f64: float = field(5)
    f32: float = field(6, kind="float")
    fx32: int = field(7, kind="fixed32")
    fx64: int = field(8, kind="fixed64")
    flag: bool = field(9)
    text: str = field(10)
    blob: bytes = field(11)


def test_proto3_omits_fields_holding_their_zero_value() -> None:
    assert encode(Scalars()) == b""


def test_a_varint_field_matches_the_specified_bytes() -> None:
    # field 1, wire type 0 -> tag 0x08; 300 -> 0xAC 0x02
    assert encode(Scalars(i32=300)) == b"\x08\xac\x02"


def test_zigzag_encoding_is_used_for_sint() -> None:
    # field 4 -> tag 0x20. sint32 -1 zigzags to 1.
    assert encode(Scalars(s32=-1)) == b"\x20\x01"
    # -2 -> 3, 1 -> 2
    assert encode(Scalars(s32=-2)) == b"\x20\x03"
    assert encode(Scalars(s32=1)) == b"\x20\x02"


def test_a_negative_int64_occupies_ten_varint_bytes() -> None:
    # Two's complement over 64 bits, which is why sint exists.
    raw = encode(Scalars(i64=-1))
    assert raw == b"\x10" + b"\xff" * 9 + b"\x01"


def test_a_string_is_utf8_length_delimited() -> None:
    # field 10, wire type 2 -> tag 0x52
    assert encode(Scalars(text="hi")) == b"\x52\x02hi"


def test_fixed_width_fields_are_little_endian() -> None:
    assert encode(Scalars(fx32=1)) == b"\x3d\x01\x00\x00\x00"
    assert encode(Scalars(fx64=1)) == b"\x41\x01\x00\x00\x00\x00\x00\x00\x00"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"i32": -7},
        {"i64": -(2**40)},
        {"u32": 4294967295},
        {"s32": -2147483648},
        {"f64": 1.5},
        {"f32": 0.5},
        {"fx32": 4294967295},
        {"fx64": 2**63},
        {"flag": True},
        {"text": "snø"},
        {"blob": b"\x00\xff"},
    ],
)
def test_every_scalar_round_trips(kwargs: dict) -> None:
    original = Scalars(**kwargs)
    assert decode(Scalars, encode(original)) == original


def test_an_out_of_range_value_for_the_declared_kind_is_refused() -> None:
    with pytest.raises(ValueError):
        encode(Scalars(u32=-1))
    with pytest.raises(ValueError):
        encode(Scalars(i32=2**31))


def test_a_truncated_buffer_raises_rather_than_over_reading() -> None:
    raw = encode(Scalars(text="hello"))
    with pytest.raises(ProtobufDecodeError):
        decode(Scalars, raw[:-2])


def test_a_length_prefix_past_the_end_of_the_buffer_is_refused() -> None:
    # tag 0x52 (field 10, LEN), length 200, but only two bytes follow.
    with pytest.raises(ProtobufDecodeError) as caught:
        decode(Scalars, b"\x52\xc8\x01ab")
    assert "length" in str(caught.value).lower()


def test_a_varint_longer_than_ten_bytes_is_refused() -> None:
    with pytest.raises(ProtobufDecodeError) as caught:
        decode(Scalars, b"\x08" + b"\x80" * 11 + b"\x01")
    assert "varint" in str(caught.value).lower()


def test_a_truncated_varint_at_the_end_of_the_buffer_is_refused() -> None:
    with pytest.raises(ProtobufDecodeError):
        decode(Scalars, b"\x08\x80")


def test_a_truncated_fixed_width_field_is_refused() -> None:
    with pytest.raises(ProtobufDecodeError):
        decode(Scalars, b"\x3d\x01\x00")


def test_field_number_zero_on_the_wire_is_refused() -> None:
    # A tag whose field number is 0 is not expressible in a valid message and is
    # the shape a fuzzer finds first.
    with pytest.raises(ProtobufDecodeError):
        decode(Scalars, b"\x00\x01")


def test_a_group_wire_type_is_refused_by_name() -> None:
    # Wire type 3 is SGROUP, deprecated since proto2. Refusing beats mis-parsing.
    with pytest.raises(ProtobufDecodeError) as caught:
        decode(Scalars, b"\x0b")
    assert "group" in str(caught.value).lower()


def test_an_unknown_wire_type_is_refused() -> None:
    with pytest.raises(ProtobufDecodeError):
        decode(Scalars, b"\x0e")


def test_invalid_utf8_in_a_string_field_is_refused() -> None:
    with pytest.raises(ProtobufDecodeError):
        decode(Scalars, b"\x52\x01\xff")
