from __future__ import annotations

import struct

import pytest

from wreath import _pgdriver as pure
from wreath.orm.errors import DeclarationError
from wreath.orm.types import BIT_OID, MAX_BIT_LENGTH, Bit
from wreath.postgres import ProtocolError

native = pytest.importorskip("wreath._native._postgres")


def _varbit_send(bits: str) -> bytes:
    """PostgreSQL's `varbit_send` frame, built from the format, not from the codec.

    Derived straight from `bit_in`'s loop -- one byte at a time, high bit first,
    zeros for the tail -- rather than from `_encode_bit`'s single big-integer
    shift, so the two cannot share a mistake.
    """
    padded = bits + "0" * (-len(bits) % 8)
    body = bytes(int(padded[at : at + 8], 2) for at in range(0, len(padded), 8))
    return struct.pack(">i", len(bits)) + body


def _both_twins_frame(bits: str) -> bytes:
    """Assert both twins encode to PostgreSQL's frame and decode it back to `bits`.

    Each arm is asserted against the anchor, never against the other arm.
    """
    expected = _varbit_send(bits)
    assert pure._encode_binary(bits, BIT_OID) == expected
    assert native._encode_binary(bits, BIT_OID) == expected
    assert pure._decode_value(BIT_OID, 1, expected) == bits
    assert native._decode_value(BIT_OID, 1, expected) == bits
    return expected


def test_the_header_is_the_bit_count_not_the_byte_count() -> None:
    wire = _both_twins_frame("101")
    assert struct.unpack_from("!i", wire, 0)[0] == 3
    assert len(wire) == 5


def test_bits_pack_from_the_high_end_of_the_first_byte() -> None:
    assert _both_twins_frame("101") == bytes.fromhex("00000003a0")


def test_a_full_byte_packs_in_the_obvious_order() -> None:
    assert _both_twins_frame("10000001") == bytes.fromhex("0000000881")


def test_the_last_byte_is_padded_on_the_right() -> None:
    assert _both_twins_frame("1" * 9) == bytes.fromhex("00000009ff80")


def test_a_quantized_embedding_is_thirty_two_times_smaller() -> None:
    wire = _both_twins_frame("10" * 768)
    assert len(wire) == 4 + 192
    # '10' repeated is 0xAA per byte; reversed it would also be 0xAA, which is
    # why the asymmetric cases above carry the byte-order claim and this one
    # carries only the size claim.
    assert wire == struct.pack(">i", 1536) + b"\xaa" * 192


def test_every_bit_survives_the_round_trip() -> None:
    bits = "1101001000011111011010101010101100000001"
    wire = _both_twins_frame(bits)
    assert wire == bytes.fromhex("00000028d21f6aab01")
    assert pure._decode_value(BIT_OID, 1, wire) == bits
    assert native._decode_value(BIT_OID, 1, wire) == bits


def test_an_empty_bit_string_is_just_a_header() -> None:
    assert _both_twins_frame("") == bytes.fromhex("00000000")


def test_the_text_form_is_the_string_itself() -> None:
    assert pure._encode_text("1011", BIT_OID) == b"1011"
    assert native._encode_text("1011", BIT_OID) == b"1011"


def test_text_decoding_answers_a_str_not_bytes() -> None:
    assert pure._decode_value(BIT_OID, 0, b"1011") == "1011"
    assert native._decode_value(BIT_OID, 0, b"1011") == "1011"


def test_a_character_that_is_not_a_bit_is_refused_by_both_twins() -> None:
    for codec in (pure, native):
        with pytest.raises(ValueError, match="only '0' and '1'"):
            codec._encode_binary("1021", BIT_OID)
        with pytest.raises(ValueError, match="only '0' and '1'"):
            codec._encode_text("1021", BIT_OID)


def test_bytes_are_refused_by_the_codec_even_though_a_column_accepts_them() -> None:
    for codec in (pure, native):
        with pytest.raises(TypeError, match="str of '0' and '1'"):
            codec._encode_binary(b"\xa0", BIT_OID)


def test_a_truncated_header_is_refused() -> None:
    for codec in (pure, native):
        with pytest.raises((ProtocolError, ValueError), match="header is truncated"):
            codec._decode_value(BIT_OID, 1, b"\x00\x00")


def test_a_payload_that_disagrees_with_the_bit_count_is_refused() -> None:
    for codec in (pure, native):
        with pytest.raises((ProtocolError, ValueError), match="does not match"):
            codec._decode_value(BIT_OID, 1, struct.pack("!i", 9) + b"\xff")


def test_non_zero_padding_is_refused_rather_than_silently_dropped() -> None:
    for codec in (pure, native):
        with pytest.raises((ProtocolError, ValueError), match="padding"):
            codec._decode_value(BIT_OID, 1, struct.pack("!i", 3) + b"\xa1")


def test_a_negative_bit_count_is_refused() -> None:
    for codec in (pure, native):
        with pytest.raises((ProtocolError, ValueError), match="negative"):
            codec._decode_value(BIT_OID, 1, struct.pack("!i", -1))


def test_the_declared_sql_names_the_length() -> None:
    assert Bit(1536).sql == "bit(1536)"


def test_the_oid_is_a_constant_because_bit_is_not_an_extension_type() -> None:
    assert Bit(8).oid == BIT_OID == 1560


def test_a_string_of_the_declared_length_passes_through_unchanged() -> None:
    assert Bit(4).coerce("1011") == "1011"


def test_a_wrong_length_is_refused_at_assignment() -> None:
    with pytest.raises(ValueError, match="exactly 4 bits, got 3"):
        Bit(4).coerce("101")


def test_a_character_that_is_not_a_bit_is_refused_at_assignment() -> None:
    with pytest.raises(ValueError, match="only '0' and '1'"):
        Bit(4).coerce("10a1")


def test_packed_bytes_are_accepted_and_unpacked_to_the_declared_length() -> None:
    assert Bit(8).coerce(b"\x81") == "10000001"
    assert Bit(3).coerce(b"\xa0") == "101"
    assert Bit(16).coerce(bytes([0b10000001, 0b01000010])) == "1000000101000010"


def test_bytes_of_the_wrong_width_are_refused() -> None:
    with pytest.raises(ValueError, match="packs into 2 bytes, got 1"):
        Bit(9).coerce(b"\xff")


def test_bytes_whose_padding_is_not_zero_are_refused() -> None:
    with pytest.raises(ValueError, match="unused bits"):
        Bit(3).coerce(b"\xa1")


def test_neither_a_list_nor_an_int_is_a_bit_string() -> None:
    for bad in ([1, 0, 1], 0b101, None):
        with pytest.raises(TypeError):
            Bit(3).coerce(bad)


def test_a_nonsense_length_is_refused_at_declaration() -> None:
    for bad in (0, -1, MAX_BIT_LENGTH + 1):
        with pytest.raises(DeclarationError, match="out of range"):
            Bit(bad)
    with pytest.raises(DeclarationError, match="int length"):
        Bit(8.0)


def test_a_bool_is_not_a_length() -> None:
    with pytest.raises(DeclarationError, match="int length"):
        Bit(True)


def test_a_bit_column_round_trips_through_the_codec_it_declares() -> None:
    column = Bit(12)
    stored = column.coerce(b"\xab\xc0")
    assert stored == "101010111100"
    assert column.oid == BIT_OID
    for codec in (pure, native):
        wire = codec._encode_binary(stored, column.oid)
        assert wire == bytes.fromhex("0000000cabc0")
        assert codec._decode_value(column.oid, 1, wire) == stored
