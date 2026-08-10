"""PostgreSQL's `bit` codec, the storage half of binary quantization.

Unlike `vector`, `halfvec` and `sparsevec`, `bit` is one of PostgreSQL's own
types: OID 1560, a compile-time constant, with no `CREATE EXTENSION` and no OID
to resolve. Only the *operators* over it -- `<~>` and `<%>`, covered in
`test_binary_quantization_queries.py` -- are pgvector's.

The whole file exists for one byte-order decision. The wire format is an int32
bit count followed by the bits packed **MSB-first**, the final byte padded on
the right. Reversed, every value still round-trips through this codec and every
distance the server computes is wrong -- so the packing is asserted against
literal bytes, not only through a round trip, and not against the other twin.

PostgreSQL settles it, in `src/backend/utils/adt/varbit.c`. `bit_send` is
"exactly the same as varbit_send, so share code", and `varbit_send` is

    pq_sendint32(&buf, VARBITLEN(s));
    pq_sendbytes(&buf, VARBITS(s), VARBITBYTES(s));

-- a big-endian int32 *bit* count, then the raw bytes of the bit string. The
ordering within a byte comes from `bit_in`, which fills each byte starting at
`x = HIGHBIT` and shifts right, and the padding from `varbit.h`: "if bit_len is
not a multiple of BITS_PER_BYTE, the low-order bits of the last byte of
bit_dat[] are unused and MUST be zeroes." So: MSB-first, tail padded with zeros
on the right, `ceil(len / 8)` bytes.
"""

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


# -- wire format --------------------------------------------------------------


def test_the_header_is_the_bit_count_not_the_byte_count() -> None:
    """Three bits are one byte, and the header still says three.

    `varbit_send` sends `VARBITLEN(s)`, the bit count, and `VARBITBYTES(s)` bytes
    after it -- so the header is not a length prefix for what follows.
    """
    wire = _both_twins_frame("101")
    assert struct.unpack_from("!i", wire, 0)[0] == 3
    assert len(wire) == 5


def test_bits_pack_from_the_high_end_of_the_first_byte() -> None:
    """The decision this file exists for, in hex: '101' is 0xA0, not 0x05.

    `bit_in` sets `x = HIGHBIT` and shifts right per character, so the first bit
    of the string is the 0x80 bit of the first byte. Reversed, this value would
    be `00000005` `05` -- which round-trips through any self-consistent codec
    and makes every Hamming distance the server computes wrong.
    """
    assert _both_twins_frame("101") == bytes.fromhex("00000003" "a0")


def test_a_full_byte_packs_in_the_obvious_order() -> None:
    """An asymmetric byte, so a reversed twin cannot pass: 0x81, never 0x18."""
    assert _both_twins_frame("10000001") == bytes.fromhex("00000008" "81")


def test_the_last_byte_is_padded_on_the_right() -> None:
    """`varbit.h`: the low-order bits of the last byte are unused and MUST be zero.

    Nine ones are 0xFF then 0x80 -- the ninth bit at the top of the second byte,
    the seven pad bits below it zero. Padding on the left would give 0x01, 0xFF.
    """
    assert _both_twins_frame("1" * 9) == bytes.fromhex("00000009" "ff80")


def test_a_quantized_embedding_is_thirty_two_times_smaller() -> None:
    """The reason to reach for this: 1,536 float4s are 6,148 bytes on the wire."""
    wire = _both_twins_frame("10" * 768)
    assert len(wire) == 4 + 192
    # '10' repeated is 0xAA per byte; reversed it would also be 0xAA, which is
    # why the asymmetric cases above carry the byte-order claim and this one
    # carries only the size claim.
    assert wire == struct.pack(">i", 1536) + b"\xaa" * 192


def test_every_bit_survives_the_round_trip() -> None:
    """Forty bits, five whole bytes, checked against the hex they must pack into."""
    bits = "1101001000011111011010101010101100000001"
    wire = _both_twins_frame(bits)
    assert wire == bytes.fromhex("00000028" "d2" "1f" "6a" "ab" "01")
    assert pure._decode_value(BIT_OID, 1, wire) == bits
    assert native._decode_value(BIT_OID, 1, wire) == bits


def test_an_empty_bit_string_is_just_a_header() -> None:
    assert _both_twins_frame("") == bytes.fromhex("00000000")


# -- the text form ------------------------------------------------------------


def test_the_text_form_is_the_string_itself() -> None:
    assert pure._encode_text("1011", BIT_OID) == b"1011"
    assert native._encode_text("1011", BIT_OID) == b"1011"


def test_text_decoding_answers_a_str_not_bytes() -> None:
    """The fall-through would have returned the raw bytes, which is the bug the
    handoff's six-dispatch-site note is about one type over."""
    assert pure._decode_value(BIT_OID, 0, b"1011") == "1011"
    assert native._decode_value(BIT_OID, 0, b"1011") == "1011"


# -- refusals -----------------------------------------------------------------


def test_a_character_that_is_not_a_bit_is_refused_by_both_twins() -> None:
    for codec in (pure, native):
        with pytest.raises(ValueError, match="only '0' and '1'"):
            codec._encode_binary("1021", BIT_OID)
        with pytest.raises(ValueError, match="only '0' and '1'"):
            codec._encode_text("1021", BIT_OID)


def test_bytes_are_refused_by_the_codec_even_though_a_column_accepts_them() -> None:
    """`Bit(n).coerce` unpacks bytes because it knows the length; the codec does
    not, and a bytes value here would have to guess how many bits it carries."""
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
    """PostgreSQL zeroes the pad, so a set bit there means the frame is not ours.

    Dropping it quietly would make two different wire values decode to the same
    string, and the difference would only ever show up as a distance nobody can
    reproduce.
    """
    for codec in (pure, native):
        with pytest.raises((ProtocolError, ValueError), match="padding"):
            codec._decode_value(BIT_OID, 1, struct.pack("!i", 3) + b"\xa1")


def test_a_negative_bit_count_is_refused() -> None:
    for codec in (pure, native):
        with pytest.raises((ProtocolError, ValueError), match="negative"):
            codec._decode_value(BIT_OID, 1, struct.pack("!i", -1))


# -- the declaration ----------------------------------------------------------


def test_the_declared_sql_names_the_length() -> None:
    assert Bit(1536).sql == "bit(1536)"


def test_the_oid_is_a_constant_because_bit_is_not_an_extension_type() -> None:
    """No resolution, no startup step, no `ExtensionNotInstalledError` path."""
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
    """The convenience the quantizers want: numpy.packbits(...).tobytes()."""
    assert Bit(8).coerce(b"\x81") == "10000001"
    assert Bit(3).coerce(b"\xa0") == "101"
    assert Bit(16).coerce(bytes([0b10000001, 0b01000010])) == "1000000101000010"


def test_bytes_of_the_wrong_width_are_refused() -> None:
    with pytest.raises(ValueError, match="packs into 2 bytes, got 1"):
        Bit(9).coerce(b"\xff")


def test_bytes_whose_padding_is_not_zero_are_refused() -> None:
    """Those bits name positions the column does not have."""
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
    """`True` is an `int` to `isinstance`, and `bit(True)` is not a column."""
    with pytest.raises(DeclarationError, match="int length"):
        Bit(True)


def test_a_bit_column_round_trips_through_the_codec_it_declares() -> None:
    """The declaration and the wire agree about which OID frames the value.

    Twelve bits, so the second byte carries four bits and four zero pads:
    `0xAB 0xC0`. The column's `coerce` unpacks exactly those bytes, and the codec
    must put them back -- the whole path stated against `varbit_send`'s frame
    rather than against a round trip that would survive a consistent reversal.
    """
    column = Bit(12)
    stored = column.coerce(b"\xab\xc0")
    assert stored == "101010111100"
    assert column.oid == BIT_OID
    for codec in (pure, native):
        wire = codec._encode_binary(stored, column.oid)
        assert wire == bytes.fromhex("0000000c" "abc0")
        assert codec._decode_value(column.oid, 1, wire) == stored
