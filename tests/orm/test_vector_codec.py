"""The pgvector binary codec, and the parity contract between its two twins.

`src/wreath/_pure/postgres.py` holds the reference implementation --
`struct.pack` over the whole vector, `struct.unpack` back out --
and `src/wreath/_native/postgres/codec.c` holds a faster twin that walks the
wire buffer once into an exactly sized allocation. Parity is the contract:
without it the two drift the way any untested pair does, and the drift would be
silent, because a vector that decodes to slightly different floats still looks
like a vector.

The wire format is pgvector's own: `uint16 dim`, `uint16 unused`, then `dim`
big-endian float4s. A 1536-dimension embedding is 6,148 bytes.
"""

from __future__ import annotations

import math
import struct

import pytest

from wreath._pure import postgres as pure
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


def _same_binary(values: list[float]) -> bytes:
    expected = pure._encode_binary(values, VECTOR_OID)
    assert native._encode_binary(values, VECTOR_OID) == expected
    assert pure._decode_value(VECTOR_OID, 1, expected) == pytest.approx(values)
    assert native._decode_value(VECTOR_OID, 1, expected) == pure._decode_value(
        VECTOR_OID, 1, expected
    )
    return expected


# -- wire format --------------------------------------------------------------


def test_header_is_dimension_then_two_reserved_bytes() -> None:
    wire = _same_binary([1.0, 2.0, 3.0])
    assert wire[:4] == struct.pack("!HH", 3, 0)
    assert len(wire) == 4 + 3 * 4


def test_a_1536_dimension_embedding_is_6148_bytes() -> None:
    assert len(_same_binary([0.5] * 1536)) == 6148


@pytest.mark.parametrize("dim", [0, 1, 2, 3, 1536, MAX_VECTOR_DIM])
def test_round_trip_at_every_dimension_boundary(dim: int) -> None:
    values = [float(index % 7) - 3.0 for index in range(dim)]
    wire = _same_binary(values)
    assert pure._decode_value(VECTOR_OID, 1, wire) == pytest.approx(values)


def test_float4_narrowing_is_identical_in_both_twins() -> None:
    # 0.1 is not representable in float4, so both twins must land on the *same*
    # rounded value -- "close enough" is exactly the drift parity exists to stop.
    wire = _same_binary([0.1, -0.1, 1e-8, 1e20, math.pi])
    assert native._decode_value(VECTOR_OID, 1, wire) == pure._decode_value(
        VECTOR_OID, 1, wire
    )


def test_int_elements_are_accepted_and_widened() -> None:
    assert _same_binary([1, 2, 3]) == _same_binary([1.0, 2.0, 3.0])


def test_negative_zero_survives_both_twins() -> None:
    wire = _same_binary([-0.0])
    assert math.copysign(1.0, pure._decode_value(VECTOR_OID, 1, wire)[0]) == -1.0
    assert math.copysign(1.0, native._decode_value(VECTOR_OID, 1, wire)[0]) == -1.0


# -- text format --------------------------------------------------------------
#
# Reachable only on a cold statement, before the plan is cached and results turn
# binary. It must land on the same Python value as the binary path: a first call
# that disagreed with every call after it is the exact defect
# `orm/introspection.py` documents for the catalog reads.


def test_text_format_round_trips_through_both_twins() -> None:
    values = [1.0, -2.5, 0.0]
    text = pure._encode_text(values, VECTOR_OID)
    assert native._encode_text(values, VECTOR_OID) == text
    assert pure._decode_value(VECTOR_OID, 0, text) == values
    assert native._decode_value(VECTOR_OID, 0, text) == values


def test_text_and_binary_decode_to_the_same_value() -> None:
    values = [0.25, -0.5, 8.0]
    for backend in (pure, native):
        binary = backend._decode_value(
            VECTOR_OID, 1, backend._encode_binary(values, VECTOR_OID)
        )
        text = backend._decode_value(
            VECTOR_OID, 0, backend._encode_text(values, VECTOR_OID)
        )
        assert binary == text == values


def test_empty_text_vector_decodes_to_an_empty_list() -> None:
    assert pure._decode_value(VECTOR_OID, 0, b"[]") == []
    assert native._decode_value(VECTOR_OID, 0, b"[]") == []


def test_unbracketed_text_is_refused_by_both_twins() -> None:
    for backend in (pure, native):
        with pytest.raises((ValueError, pure.ProtocolError)):
            backend._decode_value(VECTOR_OID, 0, b"1,2,3")


# -- refusals -----------------------------------------------------------------


@pytest.mark.parametrize(
    "wire",
    [
        b"",
        b"\x00",
        b"\x00\x03\x00\x00",                      # dim 3, no payload
        b"\x00\x01\x00\x00\x00\x00\x00",          # one float short a byte
        b"\x00\x01\x00\x01\x00\x00\x00\x00",      # reserved word is not zero
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


# -- coercion -----------------------------------------------------------------


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
