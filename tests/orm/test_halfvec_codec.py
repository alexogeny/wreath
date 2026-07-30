"""The pgvector `halfvec` binary codec, and the parity contract between its twins.

Same shape as `test_vector_codec.py` one type over: `src/wreath/_pure/postgres.py`
is the reference (`struct.pack` with the `e` format), and
`src/wreath/_native/postgres/codec.c` is the twin (`PyFloat_Pack2` per element).
Parity matters more here than for `vector`, not less: binary16 has three decimal
digits, so two implementations that disagree in the last bit produce vectors that
still *look* like vectors and rank almost the same, which is exactly the drift no
one notices.

The wire format is `vector`'s header -- `uint16 dim`, `uint16 unused` -- followed
by `dim` big-endian float2s. A 1536-dimension embedding is 3,076 bytes against
`vector`'s 6,148, which is the entire reason the type exists.
"""

from __future__ import annotations

import math
import struct

import pytest

from wreath._pure import postgres as pure
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


def _same_binary(values: list[float]) -> bytes:
    """Encode through both twins, assert byte equality, and decode through both."""
    expected = pure._encode_binary(values, HALFVEC_OID)
    assert native._encode_binary(values, HALFVEC_OID) == expected
    assert pure._decode_value(HALFVEC_OID, 1, expected) == native._decode_value(
        HALFVEC_OID, 1, expected
    )
    return expected


# -- wire format --------------------------------------------------------------


def test_header_is_dimension_then_two_reserved_bytes() -> None:
    wire = _same_binary([1.0, 2.0, 3.0])
    assert struct.unpack_from("!HH", wire, 0) == (3, 0)


def test_each_element_is_two_bytes_not_four() -> None:
    """The point of the type. 1536 dimensions must be 3,076 bytes, not 6,148."""
    wire = _same_binary([0.5] * 1536)
    assert len(wire) == 4 + 1536 * 2 == 3076


def test_an_empty_halfvec_is_just_a_header() -> None:
    assert _same_binary([]) == struct.pack("!HH", 0, 0)


def test_exactly_representable_values_round_trip_bit_for_bit() -> None:
    """Powers of two and their halves are exact in binary16, so no tolerance."""
    values = [1.0, 2.0, 0.5, -4.0, 0.25, -0.125, 0.0]
    wire = _same_binary(values)
    assert pure._decode_value(HALFVEC_OID, 1, wire) == values


def test_precision_loss_is_real_and_identical_in_both_twins() -> None:
    """`0.1` is not representable. Both twins must be wrong in the same way.

    This is the test that would catch a twin using a different rounding mode: the
    values stay close enough that an approximate comparison would pass while the
    bits differed.
    """
    wire = _same_binary([0.1])
    decoded = pure._decode_value(HALFVEC_OID, 1, wire)
    assert decoded == [0.0999755859375]
    assert native._decode_value(HALFVEC_OID, 1, wire) == decoded


def test_the_largest_finite_binary16_survives() -> None:
    wire = _same_binary([MAX_HALF_MAGNITUDE, -MAX_HALF_MAGNITUDE])
    assert pure._decode_value(HALFVEC_OID, 1, wire) == [65504.0, -65504.0]


def test_subnormals_round_trip_rather_than_flushing_to_zero() -> None:
    """binary16 subnormals go down to ~6e-8; a twin that flushed them would differ."""
    smallest = 2.0**-24
    wire = _same_binary([smallest])
    assert pure._decode_value(HALFVEC_OID, 1, wire) == [smallest]


# -- refusals -----------------------------------------------------------------


@pytest.mark.parametrize("bad", [70000.0, -70000.0, 65505.0 * 2])
def test_a_value_beyond_binary16_is_refused_by_both_twins(bad: float) -> None:
    """It would round to an infinity, which pgvector rejects on the way in.

    Refused at encode rather than left to the server, so the error names the
    element instead of arriving as an `INSERT` failure naming neither it nor the
    column.
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
    # Both twins refuse, with each one's own type: the pure reference raises
    # `ProtocolError` (its wire-format error), the C twin raises `ValueError`.
    wire = pure._encode_binary([1.0, 2.0, 3.0], HALFVEC_OID)
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
    wire = bytearray(pure._encode_binary([1.0], HALFVEC_OID))
    wire[2] = 0x01
    with pytest.raises((ProtocolError, ValueError)):
        pure._decode_value(HALFVEC_OID, 1, bytes(wire))
    with pytest.raises((ProtocolError, ValueError)):
        native._decode_value(HALFVEC_OID, 1, bytes(wire))


# -- the text form is shared with `vector` ------------------------------------


def test_the_text_form_is_the_same_bracketed_list() -> None:
    """pgvector prints both types identically, so one text codec serves both."""
    assert pure._encode_text([1.5, -2.0], HALFVEC_OID) == b"[1.5,-2.0]"
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
