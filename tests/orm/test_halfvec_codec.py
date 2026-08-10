"""The pgvector `halfvec` binary codec, against pgvector's published wire format.

Same shape as `test_vector_codec.py` one type over: `src/wreath/_pgdriver.py`
frames it with `struct.pack`'s `e` format, and
`src/wreath/_native/postgres/codec.c` with `PyFloat_Pack2` per element.
Both are driven, and **neither is the other's oracle** -- binary16 carries three
decimal digits, so two implementations that disagree in the last bit produce
vectors that still *look* like vectors and rank almost the same. A parity check
between them would pass on exactly that drift.

The anchor is pgvector's `halfvec_send`, in `src/halfvec.c`:

    pq_sendint(&buf, vec->dim, sizeof(int16));
    pq_sendint(&buf, vec->unused, sizeof(int16));
    for (int i = 0; i < vec->dim; i++)
        pq_sendhalf(&buf, vec->x[i]);

and `pq_sendhalf` is `pq_sendint16` over the binary16 bit pattern, so the frame
is `vector`'s header -- `int16 dim`, `int16 unused` (zero) -- followed by `dim`
big-endian IEEE-754 binary16s. `struct.pack(">e", ...)` is the stdlib's binary16
packer and stands in for it below, which is legitimate because pgvector's
`Float4ToHalfUnchecked` rounds the same way: `_cvtss_sh(num, 0)` is
round-to-nearest-even, as is the `(_Float16)` cast on the `FLT16_SUPPORT` arm.
The sharpest cases spell the bits in hex instead, owing nothing even to `struct`.

A 1536-dimension embedding is 3,076 bytes against `vector`'s 6,148, which is the
entire reason the type exists.
"""

from __future__ import annotations

import math
import struct

import pytest

from wreath import _pgdriver as pure
from wreath.orm.errors import DeclarationError
from wreath.orm.types import (
    EXT_KIND_HALFVEC,
    MAX_HALF_MAGNITUDE,
    MAX_HALFVEC_DIM,
    Halfvec,
)
from wreath.postgres import ProtocolError

native = pytest.importorskip("wreath._native._postgres")

#: A plausible extension-assigned OID, distinct from the one the `vector` suite
#: registers: one OID must never mean two wire formats, and registering the same
#: number under both kinds is the collision the codec table is built to refuse.
HALFVEC_OID = 987655


@pytest.fixture(scope="module", autouse=True)
def _registered() -> None:
    """Register the test OID with both codecs, as startup resolution would."""
    pure._register_extension_type("halfvec", HALFVEC_OID, EXT_KIND_HALFVEC)
    native._register_extension_type("halfvec", HALFVEC_OID, EXT_KIND_HALFVEC)


def _halfvec_send(values: list[float]) -> bytes:
    """pgvector's `halfvec_send` frame, built from the format rather than the codec.

    Header and body are packed separately, unlike `_encode_halfvec`'s single
    format string, so one wrong format cannot satisfy both sides of a test.
    """
    return struct.pack(">HH", len(values), 0) + struct.pack(f">{len(values)}e", *values)


def _narrowed(values: list[float]) -> list[float]:
    """What IEEE-754 binary16 does to `values`, per the stdlib's own packer."""
    return [struct.unpack(">e", struct.pack(">e", value))[0] for value in values]


def _both_twins_frame(values: list[float]) -> bytes:
    """Assert both twins encode to pgvector's frame and decode it back to IEEE-754.

    Each arm is asserted against the anchor, never against the other arm.
    """
    expected = _halfvec_send(values)
    assert pure._encode_binary(values, HALFVEC_OID) == expected
    assert native._encode_binary(values, HALFVEC_OID) == expected
    assert pure._decode_value(HALFVEC_OID, 1, expected) == _narrowed(values)
    assert native._decode_value(HALFVEC_OID, 1, expected) == _narrowed(values)
    return expected


# -- wire format --------------------------------------------------------------


def test_the_published_frame_for_one_two_three() -> None:
    """The sanity vector, in hex, owing nothing to `struct` or to either twin.

    Source: pgvector `src/halfvec.c`, `halfvec_send` -- big-endian `int16` dim,
    big-endian `int16` unused, then one IEEE-754 binary16 per element. 1.0, 2.0
    and 3.0 are 0x3C00, 0x4000 and 0x4200.
    """
    assert _both_twins_frame([1.0, 2.0, 3.0]) == bytes.fromhex(
        "0003" "0000" "3c00" "4000" "4200"
    )


def test_header_is_dimension_then_two_reserved_bytes() -> None:
    wire = _both_twins_frame([1.0, 2.0, 3.0])
    assert struct.unpack_from("!HH", wire, 0) == (3, 0)


def test_each_element_is_two_bytes_not_four() -> None:
    """The point of the type. 1536 dimensions must be 3,076 bytes, not 6,148."""
    wire = _both_twins_frame([0.5] * 1536)
    assert len(wire) == 4 + 1536 * 2 == 3076


def test_an_empty_halfvec_is_just_a_header() -> None:
    assert _both_twins_frame([]) == struct.pack("!HH", 0, 0)


def test_exactly_representable_values_round_trip_bit_for_bit() -> None:
    """Powers of two and their halves are exact in binary16, so no tolerance.

    The hex is the anchor: sign, five exponent bits biased by 15, ten mantissa
    bits. -4.0 is 0xC400 and -0.125 is 0xB000, so a twin that dropped the sign or
    mis-biased the exponent cannot pass by agreeing with the other twin.
    """
    values = [1.0, 2.0, 0.5, -4.0, 0.25, -0.125, 0.0]
    wire = _both_twins_frame(values)
    assert wire == bytes.fromhex(
        "0007" "0000" "3c00" "4000" "3800" "c400" "3400" "b000" "0000"
    )
    assert pure._decode_value(HALFVEC_OID, 1, wire) == values
    assert native._decode_value(HALFVEC_OID, 1, wire) == values


def test_precision_loss_lands_on_the_binary16_ieee_754_requires() -> None:
    """`0.1` is not representable, so name the pattern it must become.

    0.1 is 1.6 x 2^-4; the biased exponent is 11 and the mantissa rounds
    0.6 x 1024 = 614.4 to 614, giving `0 01011 1001100110` = 0x2E66 =
    0.0999755859375. Anchored on that rather than on the other twin, because the
    values stay close enough that an approximate comparison -- or a matching
    mistake in both halves -- would pass while the bits differed.
    """
    wire = _both_twins_frame([0.1])
    assert wire == bytes.fromhex("0001" "0000" "2e66")
    assert pure._decode_value(HALFVEC_OID, 1, wire) == [0.0999755859375]
    assert native._decode_value(HALFVEC_OID, 1, wire) == [0.0999755859375]


@pytest.mark.parametrize(
    ("value", "pattern", "rounded"),
    [
        # Halfway between 0x3C00 (1.0) and 0x3C01: ties-to-even takes the even
        # mantissa, so it rounds *down*. Round-half-away or round-half-up would
        # answer 0x3C01 here, and only here -- which is why both directions are
        # asserted rather than one.
        (1.00048828125, "3c00", 1.0),
        # Halfway between 0x3C01 and 0x3C02: the even mantissa is above, so the
        # same rule rounds *up*. Truncation would answer 0x3C01.
        (1.00146484375, "3c02", 1.001953125),
        # Halfway between zero and the smallest subnormal 0x0001 (2^-24): even
        # wins, so it flushes to zero.
        (2.0**-25, "0000", 0.0),
        # Three quarters of the way: above the tie, so it rounds up to 0x0001.
        (2.0**-25 * 1.5, "0001", 2.0**-24),
    ],
)
def test_rounding_is_to_nearest_even_at_the_ties(
    value: float, pattern: str, rounded: float
) -> None:
    """pgvector rounds with `_cvtss_sh(num, 0)`, which is round-to-nearest-even.

    A twin using any other mode differs only at an exact tie, so these are the
    four inputs that distinguish them at all.
    """
    wire = _both_twins_frame([value])
    assert wire == bytes.fromhex("0001" "0000" + pattern)
    assert pure._decode_value(HALFVEC_OID, 1, wire) == [rounded]
    assert native._decode_value(HALFVEC_OID, 1, wire) == [rounded]


def test_the_largest_finite_binary16_survives() -> None:
    """0x7BFF is the largest finite binary16; 0x7C00 would be an infinity."""
    wire = _both_twins_frame([MAX_HALF_MAGNITUDE, -MAX_HALF_MAGNITUDE])
    assert wire == bytes.fromhex("0002" "0000" "7bff" "fbff")
    assert pure._decode_value(HALFVEC_OID, 1, wire) == [65504.0, -65504.0]
    assert native._decode_value(HALFVEC_OID, 1, wire) == [65504.0, -65504.0]


def test_subnormals_round_trip_rather_than_flushing_to_zero() -> None:
    """binary16 subnormals go down to 2^-24, whose pattern is 0x0001.

    All five exponent bits zero and a mantissa of 1: a twin that flushed
    subnormals would emit 0x0000, and a twin that treated the field as normalised
    would emit something in the 0x0400 range.
    """
    smallest = 2.0**-24
    wire = _both_twins_frame([smallest])
    assert wire == bytes.fromhex("0001" "0000" "0001")
    assert pure._decode_value(HALFVEC_OID, 1, wire) == [smallest]
    assert native._decode_value(HALFVEC_OID, 1, wire) == [smallest]


# -- refusals -----------------------------------------------------------------


@pytest.mark.parametrize("bad", [70000.0, -70000.0, 65505.0 * 2])
def test_a_value_beyond_binary16_is_refused_by_both_twins(bad: float) -> None:
    """It would round to an infinity, which pgvector rejects on the way in.

    Refused at encode rather than left to the server, so the error names the
    element instead of arriving as an `INSERT` failure naming neither it nor the
    column.

    Every case here really does overflow to an infinity, so wreath and pgvector
    agree on all three. They do **not** agree everywhere: pgvector's
    `Float4ToHalf` raises only when the *result* is an infinity, so it accepts
    anything below the 65520.0 tie and rounds it to 65504.0, whereas wreath
    refuses the whole band above 65504.0. That band is left untested here rather
    than asserted in either direction -- see the report accompanying this change.
    """
    with pytest.raises(ValueError, match="binary16|out of binary16"):
        pure._encode_binary([bad], HALFVEC_OID)
    with pytest.raises(ValueError, match="binary16|out of binary16"):
        native._encode_binary([bad], HALFVEC_OID)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nan_and_infinity_are_refused_by_both_twins(bad: float) -> None:
    with pytest.raises(ValueError):
        pure._encode_binary([bad], HALFVEC_OID)
    with pytest.raises(ValueError):
        native._encode_binary([bad], HALFVEC_OID)


@pytest.mark.parametrize("bad", [True, "1.0", None, object()])
def test_non_numeric_elements_are_refused_by_both_twins(bad: object) -> None:
    with pytest.raises(TypeError):
        pure._encode_binary([bad], HALFVEC_OID)
    with pytest.raises(TypeError):
        native._encode_binary([bad], HALFVEC_OID)


def test_a_truncated_body_is_refused_rather_than_short_decoded() -> None:
    # Both twins refuse, with each one's own type: the Python reference raises
    # `ProtocolError` (its wire-format error), the C twin raises `ValueError`.
    # Built from the published frame, not from a twin: the input to a refusal
    # test should not come from the thing being tested either.
    wire = _halfvec_send([1.0, 2.0, 3.0])
    for cut in (1, 3, 5, len(wire) - 1):
        with pytest.raises((ProtocolError, ValueError)):
            pure._decode_value(HALFVEC_OID, 1, wire[:cut])
        with pytest.raises((ProtocolError, ValueError)):
            native._decode_value(HALFVEC_OID, 1, wire[:cut])


def test_a_vector_length_body_is_refused() -> None:
    """float4 bytes under a halfvec OID must not silently decode as twice the dims.

    The header says 3; a float4 body is 12 bytes where 6 is required. Accepting it
    would decode one type's rows with the other's rules.
    """
    wrong = struct.pack("!HH3f", 3, 0, 1.0, 2.0, 3.0)
    with pytest.raises((ProtocolError, ValueError)):
        pure._decode_value(HALFVEC_OID, 1, wrong)
    with pytest.raises((ProtocolError, ValueError)):
        native._decode_value(HALFVEC_OID, 1, wrong)


def test_reserved_header_bytes_must_be_zero() -> None:
    wire = bytearray(_halfvec_send([1.0]))
    wire[2] = 0x01
    with pytest.raises((ProtocolError, ValueError)):
        pure._decode_value(HALFVEC_OID, 1, bytes(wire))
    with pytest.raises((ProtocolError, ValueError)):
        native._decode_value(HALFVEC_OID, 1, bytes(wire))


# -- the text form is shared with `vector` ------------------------------------


def test_the_text_form_is_the_same_bracketed_list() -> None:
    """pgvector prints both types identically, so one text codec serves both.

    Every arm is asserted against the written-down literal. As for `vector`, the
    literal stands in for a published expectation the format cannot supply:
    pgvector's `halfvec_out` prints shortest-decimal and wreath prints
    `repr(float)`, and only the input grammar `halfvec_in` accepts is shared.
    """
    assert pure._encode_text([1.5, -2.0], HALFVEC_OID) == b"[1.5,-2.0]"
    assert native._encode_text([1.5, -2.0], HALFVEC_OID) == b"[1.5,-2.0]"
    assert pure._decode_value(HALFVEC_OID, 0, b"[1.5,-2.0]") == [1.5, -2.0]
    assert native._decode_value(HALFVEC_OID, 0, b"[1.5,-2.0]") == [1.5, -2.0]


# -- declaration --------------------------------------------------------------


def test_the_declaration_names_the_vector_extension_not_the_type() -> None:
    """One `CREATE EXTENSION vector` provides both types.

    If this said `halfvec`, the not-installed error would tell the reader to
    install an extension that does not exist.
    """
    column = Halfvec(1536)
    assert column.extension == "vector"
    assert column.type_name == "halfvec"
    assert column.sql == "halfvec(1536)"


def test_halfvec_and_vector_of_one_dimension_are_distinct_types() -> None:
    """Their SQL, plan-cache shape and fingerprint OID must all differ.

    A shared shape value would give two different columns one plan-cache entry;
    a shared fingerprint would make a `vector` -> `halfvec` migration invisible.
    """
    from wreath.orm.types import Vector

    half, full = Halfvec(3), Vector(3)
    assert half.sql != full.sql
    assert half.shape_value != full.shape_value
    assert half.fingerprint_oid != full.fingerprint_oid


@pytest.mark.parametrize("bad", [0, -1, MAX_HALFVEC_DIM + 1])
def test_an_out_of_range_dimension_is_refused_at_declaration(bad: int) -> None:
    with pytest.raises(DeclarationError, match="out of range"):
        Halfvec(bad)


@pytest.mark.parametrize("bad", [1.0, "3", True, None])
def test_a_non_int_dimension_is_refused_at_declaration(bad: object) -> None:
    with pytest.raises(DeclarationError, match="int dimension"):
        Halfvec(bad)  # type: ignore[arg-type]


def test_coercion_refuses_a_value_binary16_cannot_hold() -> None:
    """The same bound as the codec, but named at assignment.

    Two checks rather than one, deliberately: coercion is where a model assignment
    fails with the column in scope, and the codec is the backstop for a value that
    reached the wire another way.
    """
    column = Halfvec(2)
    assert column.coerce([1.5, -2.25]) == [1.5, -2.25]
    assert column.coerce((1.5, -2.25)) == [1.5, -2.25], "a tuple is accepted, as a list"
    assert column.coerce([1, 2]) == [1.0, 2.0], "ints widen rather than being refused"
    with pytest.raises(ValueError, match="binary16"):
        column.coerce([1.0, 70000.0])
    with pytest.raises(ValueError, match="finite"):
        column.coerce([1.0, math.inf])
    with pytest.raises(ValueError, match="exactly 2 values"):
        column.coerce([1.0])


@pytest.mark.parametrize("bad", ["[1.0,2.0]", 1.5, None, {"a": 1}, b"\x00\x01"])
def test_coercion_refuses_a_value_that_is_not_a_sequence(bad: object) -> None:
    """`wreath mutant` found this branch unasserted.

    A `str` is the one that matters: it is iterable and of the right length often
    enough that a codec reached with one would encode its characters.
    """
    with pytest.raises(TypeError, match="list or tuple"):
        Halfvec(2).coerce(bad)


@pytest.mark.parametrize("bad", [True, False, "1.0", None, [1.0]])
def test_coercion_refuses_a_non_numeric_element(bad: object) -> None:
    """Also unasserted until mutant said so. `bool` is deliberate.

    `True` is an `int` in Python, so a plain numeric check accepts it and stores
    1.0 -- which is a silently wrong embedding rather than an error.
    """
    with pytest.raises(TypeError, match="float"):
        Halfvec(2).coerce([1.0, bad])


def test_registering_one_oid_under_two_kinds_is_refused() -> None:
    """One OID must never mean two wire formats.

    Both twins hold an OID-keyed table, and this is the collision it exists to
    see: `vector` and `halfvec` differ only in element width, so decoding one as
    the other yields a plausible-looking vector of the wrong length.
    """
    from wreath.orm.types import EXT_KIND_VECTOR

    with pytest.raises(ValueError, match="already registered"):
        pure._register_extension_type("vector", HALFVEC_OID, EXT_KIND_VECTOR)
    with pytest.raises(ValueError, match="already registered|codec kind"):
        native._register_extension_type("vector", HALFVEC_OID, EXT_KIND_VECTOR)
