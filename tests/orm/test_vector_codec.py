from __future__ import annotations

import math
import struct

import pytest

from wreath import _pgdriver as pure
from wreath.orm.errors import ExtensionNotInstalledError
from wreath.orm.types import EXT_KIND_VECTOR, MAX_VECTOR_DIM, ExtensionType, Vector

native = pytest.importorskip("wreath._native._postgres")

#: A plausible extension-assigned OID. Nothing about it is special except that
#: it is above PostgreSQL's built-in range, which is the whole point: it could
#: not have been a `case` in the codec's switch.
VECTOR_OID = 987654


@pytest.fixture(scope="module", autouse=True)
def _registered() -> None:
    """Register the test OID with both codecs, as startup resolution would."""
    pure._register_extension_type("vector", VECTOR_OID, EXT_KIND_VECTOR)
    native._register_extension_type("vector", VECTOR_OID, EXT_KIND_VECTOR)


def _vector_send(values: list[float]) -> bytes:
    """pgvector's `vector_send` frame, built from the format rather than the codec.

    This is the independent expectation every test in this file rests on. It is
    deliberately *not* spelled the way `_encode_vector` is -- header and body are
    packed separately here -- so a single wrong format string cannot satisfy both.
    """
    return struct.pack(">HH", len(values), 0) + struct.pack(f">{len(values)}f", *values)


def _narrowed(values: list[float]) -> list[float]:
    """What IEEE-754 binary32 does to `values`, per the stdlib's own packer.

    A decoded `vector` must equal this exactly. `pytest.approx` would pass on a
    twin that rounded differently, which is the drift worth catching.
    """
    return [struct.unpack(">f", struct.pack(">f", value))[0] for value in values]


def _both_twins_frame(values: list[float]) -> bytes:
    """Assert both twins encode to pgvector's frame and decode it back to IEEE-754.

    Each arm is asserted against the anchor, never against the other arm, so a
    shared mistake has nothing to hide behind.
    """
    expected = _vector_send(values)
    assert pure._encode_binary(values, VECTOR_OID) == expected
    assert native._encode_binary(values, VECTOR_OID) == expected
    assert pure._decode_value(VECTOR_OID, 1, expected) == _narrowed(values)
    assert native._decode_value(VECTOR_OID, 1, expected) == _narrowed(values)
    return expected


def test_the_published_frame_for_one_two_three() -> None:
    assert _both_twins_frame([1.0, 2.0, 3.0]) == bytes.fromhex("000300003f8000004000000040400000")


def test_header_is_dimension_then_two_reserved_bytes() -> None:
    wire = _both_twins_frame([1.0, 2.0, 3.0])
    assert wire[:4] == struct.pack("!HH", 3, 0)
    assert len(wire) == 4 + 3 * 4


def test_a_1536_dimension_embedding_is_6148_bytes() -> None:
    assert len(_both_twins_frame([0.5] * 1536)) == 6148


@pytest.mark.parametrize("dim", [0, 1, 2, 3, 1536, MAX_VECTOR_DIM])
def test_round_trip_at_every_dimension_boundary(dim: int) -> None:
    values = [float(index % 7) - 3.0 for index in range(dim)]
    wire = _both_twins_frame(values)
    # Every element here is a small integer, so binary32 holds it exactly and the
    # decoded list must equal the input outright rather than approximately.
    assert pure._decode_value(VECTOR_OID, 1, wire) == values
    assert native._decode_value(VECTOR_OID, 1, wire) == values


def test_float4_narrowing_lands_where_ieee_754_says() -> None:
    values = [0.1, -0.1, 1e-8, 1e20, math.pi]
    wire = _both_twins_frame(values)
    assert wire == bytes.fromhex("000500003dcccccdbdcccccd322bcc7760ad78ec40490fdb")
    expected = [
        0.10000000149011612,
        -0.10000000149011612,
        9.99999993922529e-09,
        1.0000000200408773e20,
        3.1415927410125732,
    ]
    assert pure._decode_value(VECTOR_OID, 1, wire) == expected
    assert native._decode_value(VECTOR_OID, 1, wire) == expected


def test_int_elements_are_accepted_and_widened() -> None:
    published = bytes.fromhex("000300003f8000004000000040400000")
    assert _both_twins_frame([1, 2, 3]) == published
    assert _both_twins_frame([1.0, 2.0, 3.0]) == published


def test_negative_zero_survives_both_twins() -> None:
    wire = _both_twins_frame([-0.0])
    assert wire == bytes.fromhex("0001000080000000")
    assert math.copysign(1.0, pure._decode_value(VECTOR_OID, 1, wire)[0]) == -1.0
    assert math.copysign(1.0, native._decode_value(VECTOR_OID, 1, wire)[0]) == -1.0


# Reachable only on a cold statement, before the plan is cached and results turn
# binary. It must land on the same Python value as the binary path: a first call
# that disagreed with every call after it is the exact defect
# `orm/introspection.py` documents for the catalog reads.


def test_text_format_round_trips_through_both_twins() -> None:
    values = [1.0, -2.5, 0.0]
    assert pure._encode_text(values, VECTOR_OID) == b"[1.0,-2.5,0.0]"
    assert native._encode_text(values, VECTOR_OID) == b"[1.0,-2.5,0.0]"
    assert pure._decode_value(VECTOR_OID, 0, b"[1.0,-2.5,0.0]") == values
    assert native._decode_value(VECTOR_OID, 0, b"[1.0,-2.5,0.0]") == values


def test_text_and_binary_decode_to_the_same_value() -> None:
    values = [0.25, -0.5, 8.0]
    for backend in (pure, native):
        binary = backend._decode_value(VECTOR_OID, 1, backend._encode_binary(values, VECTOR_OID))
        text = backend._decode_value(VECTOR_OID, 0, backend._encode_text(values, VECTOR_OID))
        assert binary == text == values


def test_empty_text_vector_decodes_to_an_empty_list() -> None:
    assert pure._decode_value(VECTOR_OID, 0, b"[]") == []
    assert native._decode_value(VECTOR_OID, 0, b"[]") == []


def test_unbracketed_text_is_refused_by_both_twins() -> None:
    for backend in (pure, native):
        with pytest.raises((ValueError, pure.ProtocolError)):
            backend._decode_value(VECTOR_OID, 0, b"1,2,3")


@pytest.mark.parametrize(
    "wire",
    [
        b"",
        b"\x00",
        b"\x00\x03\x00\x00",  # dim 3, no payload
        b"\x00\x01\x00\x00\x00\x00\x00",  # one float short a byte
        b"\x00\x01\x00\x01\x00\x00\x00\x00",  # reserved word is not zero
    ],
)
def test_malformed_binary_is_refused_by_both_twins(wire: bytes) -> None:
    for backend in (pure, native):
        with pytest.raises((ValueError, pure.ProtocolError)):
            backend._decode_value(VECTOR_OID, 1, wire)


def test_a_non_sequence_is_refused_by_both_twins() -> None:
    for backend in (pure, native):
        with pytest.raises(TypeError):
            backend._encode_binary(3.5, VECTOR_OID)


def test_a_non_numeric_element_is_refused_by_both_twins() -> None:
    for backend in (pure, native):
        with pytest.raises((TypeError, struct.error)):
            backend._encode_binary(["not a float"], VECTOR_OID)


def test_coerce_rejects_a_wrong_dimension() -> None:
    with pytest.raises(ValueError, match="exactly 3 values, got 2"):
        Vector(3).coerce([1.0, 2.0])


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_coerce_rejects_nan_and_infinity(bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        Vector(2).coerce([1.0, bad])


def test_coerce_rejects_bool_elements() -> None:
    with pytest.raises(TypeError):
        Vector(1).coerce([True])


def test_coerce_normalises_ints_and_tuples_to_a_list_of_floats() -> None:
    assert Vector(3).coerce((1, 2, 3)) == [1.0, 2.0, 3.0]


@pytest.mark.parametrize("dim", [0, -1, MAX_VECTOR_DIM + 1])
def test_declaring_an_out_of_range_dimension_fails_at_declaration(dim: int) -> None:
    from wreath.orm.errors import DeclarationError

    with pytest.raises(DeclarationError):
        Vector(dim)


def test_binding_before_resolution_names_the_extension() -> None:
    unresolved = ExtensionType(
        "vector", "vector", "vector(2)", lambda value: value, kind=EXT_KIND_VECTOR
    )
    assert unresolved.oid == 0
    with pytest.raises(ExtensionNotInstalledError, match="vector"):
        unresolved.to_wire([1.0, 2.0])
