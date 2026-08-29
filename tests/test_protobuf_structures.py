from __future__ import annotations

import enum

import pytest

from wreath.protobuf import (
    ProtobufDeclarationError,
    decode,
    encode,
    field,
    message,
    unknown_fields,
)


class Quality(enum.IntEnum):
    UNKNOWN = 0
    GOOD = 1
    POOR = 2


@message
class Inner:
    value: int = field(1)


@message
class Repeats:
    numbers: list[int] = field(1)
    names: list[str] = field(2)
    loose: list[int] = field(3, packed=False)
    children: list[Inner] = field(4)


@message
class Nested:
    label: str = field(1)
    inner: Inner | None = field(2)


@message
class WithEnum:
    quality: Quality = field(1)


@message
class Choice:
    text: str | None = field(1, oneof="payload")
    blob: bytes | None = field(2, oneof="payload")
    unrelated: int = field(3)


@message
class Maps:
    counts: dict[str, int] = field(1)
    nested: dict[str, Inner] = field(2)


def test_repeated_scalars_pack_into_one_length_delimited_field() -> None:
    # field 1, LEN -> tag 0x0A; three varints in one body.
    assert encode(Repeats(numbers=[1, 2, 300])) == b"\x0a\x04\x01\x02\xac\x02"


def test_packed_false_emits_one_tagged_field_per_item() -> None:
    # field 3, VARINT -> tag 0x18, repeated.
    assert encode(Repeats(loose=[1, 2])) == b"\x18\x01\x18\x02"


def test_repeated_strings_are_never_packed() -> None:
    # Length-delimited values already carry a length; packing gains nothing and
    # the specification does not allow it.
    assert encode(Repeats(names=["a", "b"])) == b"\x12\x01a\x12\x01b"


def test_a_decoder_accepts_the_unpacked_form_of_a_packed_field() -> None:
    # proto3 requires a parser to accept both representations, because a peer
    # built against an older declaration may send either.
    assert decode(Repeats, b"\x08\x01\x08\x02").numbers == [1, 2]


def test_a_decoder_accepts_the_packed_form_of_an_unpacked_field() -> None:
    assert decode(Repeats, b"\x1a\x02\x01\x02").loose == [1, 2]


def test_an_empty_repeated_field_writes_nothing() -> None:
    assert encode(Repeats()) == b""


def test_repeated_round_trips() -> None:
    original = Repeats(numbers=[1, 2, 3], names=["x", "y"], loose=[9], children=[Inner(value=4)])
    assert decode(Repeats, encode(original)) == original


def test_a_nested_message_is_length_delimited() -> None:
    # inner is field 2 -> tag 0x12; Inner(value=1) encodes to 0x08 0x01.
    assert encode(Nested(inner=Inner(value=1))) == b"\x12\x02\x08\x01"


def test_an_absent_nested_message_writes_nothing() -> None:
    assert encode(Nested(label="x")) == b"\x0a\x01x"


def test_a_present_but_empty_nested_message_is_distinguishable_from_absent() -> None:
    # This is the whole reason message fields have explicit presence: an empty
    # submessage still occupies a zero-length field.
    assert encode(Nested(inner=Inner())) == b"\x12\x00"
    assert decode(Nested, b"\x12\x00").inner == Inner()
    assert decode(Nested, b"").inner is None


def test_nested_round_trips() -> None:
    original = Nested(label="a", inner=Inner(value=7))
    assert decode(Nested, encode(original)) == original


def test_an_enum_encodes_as_a_varint_and_omits_its_zero() -> None:
    assert encode(WithEnum(quality=Quality.UNKNOWN)) == b""
    assert encode(WithEnum(quality=Quality.POOR)) == b"\x08\x02"


def test_an_enum_round_trips_as_its_member() -> None:
    decoded = decode(WithEnum, encode(WithEnum(quality=Quality.GOOD)))
    assert decoded.quality is Quality.GOOD


def test_an_unknown_enum_value_survives_as_the_integer_the_peer_sent() -> None:
    # proto3 enums are open: a decoder that dropped an unrecognised value would
    # lose data a newer peer considers meaningful, and re-encoding must return it.
    decoded = decode(WithEnum, b"\x08\x63")
    assert decoded.quality == 99
    assert encode(decoded) == b"\x08\x63"


def test_setting_one_oneof_member_encodes_only_it() -> None:
    assert encode(Choice(text="a")) == b"\x0a\x01a"
    assert encode(Choice(blob=b"z")) == b"\x12\x01z"


def test_the_last_oneof_member_on_the_wire_wins() -> None:
    # Two members present is a malformed message the specification resolves by
    # last-one-wins rather than by refusing.
    decoded = decode(Choice, b"\x0a\x01a\x12\x01z")
    assert decoded.text is None
    assert decoded.blob == b"z"


def test_a_oneof_does_not_disturb_fields_outside_it() -> None:
    decoded = decode(Choice, encode(Choice(text="a", unrelated=5)))
    assert decoded.text == "a"
    assert decoded.unrelated == 5


def test_a_non_optional_oneof_member_is_refused_at_declaration() -> None:
    with pytest.raises(ProtobufDeclarationError) as caught:

        @message
        class BadChoice:
            a: str = field(1, oneof="g")

    assert "oneof" in str(caught.value).lower()


def test_a_map_encodes_as_repeated_key_value_entries() -> None:
    # Each entry is a length-delimited message of {1: key, 2: value}.
    assert encode(Maps(counts={"a": 1})) == b"\x0a\x05\x0a\x01a\x10\x01"


def test_a_map_round_trips() -> None:
    original = Maps(counts={"a": 1, "b": 2}, nested={"k": Inner(value=3)})
    assert decode(Maps, encode(original)) == original


def test_an_empty_map_writes_nothing() -> None:
    assert encode(Maps()) == b""


def test_a_map_entry_holding_defaults_still_produces_an_entry() -> None:
    # The entry exists even though both halves are their zero, because the key's
    # presence in the map is the information.
    assert encode(Maps(counts={"": 0})) == b"\x0a\x00"
    assert decode(Maps, b"\x0a\x00").counts == {"": 0}


def test_a_float_map_key_is_refused_at_declaration() -> None:
    with pytest.raises(ProtobufDeclarationError):

        @message
        class BadMap:
            m: dict[float, int] = field(1)


def test_an_unknown_field_survives_a_decode_encode_round_trip() -> None:
    # Field 15 is not declared on Inner. A newer peer sent it; an older build
    # must hand it back untouched or the newer peer loses data by relaying.
    raw = b"\x08\x01\x7a\x02hi"
    relayed = encode(decode(Inner, raw))
    assert relayed == raw


def test_unknown_fields_are_readable() -> None:
    decoded = decode(Inner, b"\x08\x01\x7a\x02hi")
    assert unknown_fields(decoded) == b"\x7a\x02hi"
    assert decoded.value == 1


def test_a_message_with_no_unknown_fields_reports_none() -> None:
    assert unknown_fields(decode(Inner, b"\x08\x01")) == b""


def test_unknown_fields_of_every_wire_type_survive() -> None:
    raw = (
        b"\x08\x01"  # known field 1
        b"\x78\x2a"  # unknown 15, varint
        b"\x7d\x01\x00\x00\x00"  # unknown 15, i32
        b"\x81\x01\x01\x00\x00\x00\x00\x00\x00\x00"  # unknown 16, i64
        b"\x8a\x01\x01z"  # unknown 17, len
    )
    assert encode(decode(Inner, raw)) == raw


def test_an_unknown_field_inside_a_nested_message_survives() -> None:
    inner_raw = b"\x08\x01\x7a\x01q"
    raw = b"\x12" + bytes([len(inner_raw)]) + inner_raw
    assert encode(decode(Nested, raw)) == raw
