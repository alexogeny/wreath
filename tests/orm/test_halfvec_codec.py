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


def test_the_published_frame_for_one_two_three() -> None:
    assert _both_twins_frame([1.0, 2.0, 3.0]) == bytes.fromhex("000300003c0040004200")


def test_header_is_dimension_then_two_reserved_bytes() -> None:
    wire = _both_twins_frame([1.0, 2.0, 3.0])
    assert struct.unpack_from("!HH", wire, 0) == (3, 0)


def test_each_element_is_two_bytes_not_four() -> None:
    wire = _both_twins_frame([0.5] * 1536)
    assert len(wire) == 4 + 1536 * 2 == 3076


def test_an_empty_halfvec_is_just_a_header() -> None:
    assert _both_twins_frame([]) == struct.pack("!HH", 0, 0)


def test_exactly_representable_values_round_trip_bit_for_bit() -> None:
    values = [1.0, 2.0, 0.5, -4.0, 0.25, -0.125, 0.0]
    wire = _both_twins_frame(values)
    assert wire == bytes.fromhex("000700003c0040003800c4003400b0000000")
    assert pure._decode_value(HALFVEC_OID, 1, wire) == values
    assert native._decode_value(HALFVEC_OID, 1, wire) == values


def test_precision_loss_lands_on_the_binary16_ieee_754_requires() -> None:
    wire = _both_twins_frame([0.1])
    assert wire == bytes.fromhex("000100002e66")
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
    wire = _both_twins_frame([value])
    assert wire == bytes.fromhex("00010000" + pattern)
    assert pure._decode_value(HALFVEC_OID, 1, wire) == [rounded]
    assert native._decode_value(HALFVEC_OID, 1, wire) == [rounded]


def test_the_largest_finite_binary16_survives() -> None:
    wire = _both_twins_frame([MAX_HALF_MAGNITUDE, -MAX_HALF_MAGNITUDE])
    assert wire == bytes.fromhex("000200007bfffbff")
    assert pure._decode_value(HALFVEC_OID, 1, wire) == [65504.0, -65504.0]
    assert native._decode_value(HALFVEC_OID, 1, wire) == [65504.0, -65504.0]


def test_subnormals_round_trip_rather_than_flushing_to_zero() -> None:
    smallest = 2.0**-24
    wire = _both_twins_frame([smallest])
    assert wire == bytes.fromhex("000100000001")
    assert pure._decode_value(HALFVEC_OID, 1, wire) == [smallest]
    assert native._decode_value(HALFVEC_OID, 1, wire) == [smallest]


@pytest.mark.parametrize("bad", [70000.0, -70000.0, 65505.0 * 2])
def test_a_value_beyond_binary16_is_refused_by_both_twins(bad: float) -> None:
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


def test_the_text_form_is_the_same_bracketed_list() -> None:
    assert pure._encode_text([1.5, -2.0], HALFVEC_OID) == b"[1.5,-2.0]"
    assert native._encode_text([1.5, -2.0], HALFVEC_OID) == b"[1.5,-2.0]"
    assert pure._decode_value(HALFVEC_OID, 0, b"[1.5,-2.0]") == [1.5, -2.0]
    assert native._decode_value(HALFVEC_OID, 0, b"[1.5,-2.0]") == [1.5, -2.0]


def test_the_declaration_names_the_vector_extension_not_the_type() -> None:
    column = Halfvec(1536)
    assert column.extension == "vector"
    assert column.type_name == "halfvec"
    assert column.sql == "halfvec(1536)"


def test_halfvec_and_vector_of_one_dimension_are_distinct_types() -> None:
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
    with pytest.raises(TypeError, match="list or tuple"):
        Halfvec(2).coerce(bad)


@pytest.mark.parametrize("bad", [True, False, "1.0", None, [1.0]])
def test_coercion_refuses_a_non_numeric_element(bad: object) -> None:
    with pytest.raises(TypeError, match="float"):
        Halfvec(2).coerce([1.0, bad])


def test_registering_one_oid_under_two_kinds_is_refused() -> None:
    from wreath.orm.types import EXT_KIND_VECTOR

    with pytest.raises(ValueError, match="already registered"):
        pure._register_extension_type("vector", HALFVEC_OID, EXT_KIND_VECTOR)
    with pytest.raises(ValueError, match="already registered|codec kind"):
        native._register_extension_type("vector", HALFVEC_OID, EXT_KIND_VECTOR)
