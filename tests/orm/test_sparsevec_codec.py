from __future__ import annotations

import contextlib
import struct

import pytest

from wreath import _pgdriver as pure
from wreath.orm.errors import DeclarationError
from wreath.orm.types import (
    EXT_KIND_SPARSEVEC,
    MAX_SPARSEVEC_DIM,
    MAX_SPARSEVEC_NNZ,
    Sparsevec,
    SparseVector,
)
from wreath.postgres import ProtocolError

native = pytest.importorskip("wreath._native._postgres")

#: A plausible extension-assigned OID, distinct from the ones the `vector` and
#: `halfvec` suites register: one OID must never mean two wire formats.
SPARSEVEC_OID = 987656


@pytest.fixture(scope="module", autouse=True)
def _registered() -> None:
    """Register the test OID with both codecs, as startup resolution would."""
    pure._register_extension_type("sparsevec", SPARSEVEC_OID, EXT_KIND_SPARSEVEC)
    native._register_extension_type("sparsevec", SPARSEVEC_OID, EXT_KIND_SPARSEVEC)


def _sparsevec_send(value: SparseVector) -> bytes:
    """pgvector's `sparsevec_send` frame, built from the format, not from the codec.

    The `- 1` is pgvector's own "Convert 1-based numbering (SQL) to 0-based (C)",
    written here so the wire's numbering is stated by the test rather than
    inherited from whichever twin is under examination. Header, indices and
    values are packed as three separate calls, unlike `_encode_sparsevec`'s single
    format string, so one wrong format cannot satisfy both sides.
    """
    nnz = len(value.indices)
    return (
        struct.pack(">iii", value.dim, nnz, 0)
        + struct.pack(f">{nnz}i", *[index - 1 for index in value.indices])
        + struct.pack(f">{nnz}f", *value.values)
    )


def _narrowed(value: SparseVector) -> SparseVector:
    """`value` with every element put through IEEE-754 binary32, as the wire does.

    `sparsevec` stores float4s, so a decoded value equals the original only where
    every element was already representable. The stdlib's packer, not the other
    twin, says what the rest become.
    """
    return SparseVector(
        value.dim,
        {
            index: struct.unpack(">f", struct.pack(">f", number))[0]
            for index, number in zip(value.indices, value.values, strict=True)
        },
    )


def _both_twins_frame(value: SparseVector) -> bytes:
    """Assert both twins encode to pgvector's frame and decode it back to `value`.

    Each arm is asserted against the anchor, never against the other arm.
    """
    expected = _sparsevec_send(value)
    assert pure._encode_binary(value, SPARSEVEC_OID) == expected
    assert native._encode_binary(value, SPARSEVEC_OID) == expected
    assert pure._decode_value(SPARSEVEC_OID, 1, expected) == _narrowed(value)
    assert native._decode_value(SPARSEVEC_OID, 1, expected) == _narrowed(value)
    return expected


def _both_twins_text(value: SparseVector, expected: bytes) -> bytes:
    """Assert both twins print `expected` and read it back as `value`.

    `expected` is written down at the call site rather than taken from a twin.
    pgvector's documented text form is `'{1:1.5,3:3.5}/5'` -- brace-delimited
    1-based `index:value` pairs, then a slash and the dimension -- and unlike the
    dense types' text form, that spelling is exact enough to pin the bytes.
    """
    assert pure._encode_text(value, SPARSEVEC_OID) == expected
    assert native._encode_text(value, SPARSEVEC_OID) == expected
    assert pure._decode_value(SPARSEVEC_OID, 0, expected) == value
    assert native._decode_value(SPARSEVEC_OID, 0, expected) == value
    return expected


def test_elements_are_stored_sorted_by_index_whatever_order_they_arrive() -> None:
    sparse = SparseVector(9, {7: 0.5, 2: 1.5, 4: -1.0})
    assert sparse.indices == (2, 4, 7)
    assert sparse.values == (1.5, -1.0, 0.5)


def test_an_explicit_zero_is_dropped_rather_than_stored() -> None:
    assert SparseVector(5, {1: 1.0, 2: 0.0}).to_dict() == {1: 1.0}
    assert len(SparseVector(5, {2: 0.0})) == 0


def test_len_is_the_stored_count_not_the_dimension() -> None:
    assert len(SparseVector(1_000_000, {5: 1.0, 900: 2.0})) == 2


def test_from_dense_keeps_the_dimension_the_sequence_had() -> None:
    dense = [0.0, 1.5, 0.0, 0.0, 3.5]
    sparse = SparseVector.from_dense(dense)
    assert sparse.dim == 5
    assert sparse.to_dict() == {2: 1.5, 5: 3.5}


def test_equality_is_by_dimension_and_elements() -> None:
    assert SparseVector(5, {1: 1.5}) == SparseVector(5, {1: 1.5, 3: 0.0})
    assert SparseVector(5, {1: 1.5}) != SparseVector(6, {1: 1.5})
    assert SparseVector(5, {1: 1.5}) != SparseVector(5, {2: 1.5})


def test_an_index_outside_the_dimension_is_refused() -> None:
    with pytest.raises(ValueError, match="1-based"):
        SparseVector(3, {4: 1.0})
    with pytest.raises(ValueError, match="1-based"):
        SparseVector(3, {0: 1.0})


def test_nan_and_infinity_are_refused_as_they_are_for_vector() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="neither NaN nor infinity"):
            SparseVector(3, {1: bad})


def test_a_bool_is_not_a_number_here_either() -> None:
    with pytest.raises(TypeError, match="must be int or float"):
        SparseVector(3, {1: True})


def test_more_non_zeros_than_pgvector_allows_is_refused() -> None:
    too_many = {index: 1.0 for index in range(1, MAX_SPARSEVEC_NNZ + 2)}
    with pytest.raises(ValueError, match="at most 16000 non-zero"):
        SparseVector(MAX_SPARSEVEC_NNZ + 1, too_many)


def test_the_published_frame_for_a_two_element_sparsevec() -> None:
    assert _both_twins_frame(SparseVector(5, {1: 1.5, 3: 3.5})) == bytes.fromhex(
        "00000005000000020000000000000000000000023fc0000040600000"
    )


def test_header_is_dimension_then_count_then_two_reserved_words() -> None:
    wire = _both_twins_frame(SparseVector(5, {1: 1.5, 3: 3.5}))
    assert struct.unpack_from("!iii", wire, 0) == (5, 2, 0)


def test_the_binary_indices_are_zero_based_and_the_value_is_one_based() -> None:
    wire = _both_twins_frame(SparseVector(5, {1: 1.5, 3: 3.5}))
    assert struct.unpack_from("!ii", wire, 12) == (0, 2)


def test_values_follow_all_of_the_indices_rather_than_interleaving() -> None:
    wire = _both_twins_frame(SparseVector(5, {1: 1.5, 3: 3.5}))
    assert struct.unpack_from("!ff", wire, 20) == (1.5, 3.5)
    assert len(wire) == 12 + 2 * 4 + 2 * 4


def test_an_empty_sparsevec_is_just_a_header() -> None:
    assert _both_twins_frame(SparseVector(9)) == struct.pack("!iii", 9, 0, 0)


def test_a_huge_dimension_costs_nothing_which_is_the_point_of_the_type() -> None:
    wire = _both_twins_frame(SparseVector(MAX_SPARSEVEC_DIM, {1: 1.0, 999_999_999: 2.0}))
    assert len(wire) == 28
    assert wire == bytes.fromhex("3b9aca000000000200000000000000003b9ac9fe3f80000040000000")


def test_elements_round_trip_through_the_binary_form() -> None:
    sparse = SparseVector(1000, {1: 1.5, 17: -0.25, 999: 8.0})
    wire = _both_twins_frame(sparse)
    # Every element is a dyadic rational, so binary32 holds it exactly and the
    # decoded value must equal the original outright.
    assert wire == bytes.fromhex(
        "000003e800000003000000000000000000000010000003e63fc00000be80000041000000"
    )
    assert pure._decode_value(SPARSEVEC_OID, 1, wire) == sparse
    assert native._decode_value(SPARSEVEC_OID, 1, wire) == sparse


def test_float4_rounding_is_the_same_rounding_vector_gets() -> None:
    wire = _both_twins_frame(SparseVector(3, {2: 0.1}))
    assert wire == bytes.fromhex("000000030000000100000000000000013dcccccd")
    for codec in (pure, native):
        assert codec._decode_value(SPARSEVEC_OID, 1, wire).to_dict() == {2: 0.10000000149011612}


def test_the_text_form_is_braced_elements_over_the_dimension() -> None:
    _both_twins_text(SparseVector(5, {1: 1.5, 3: 3.5}), b"{1:1.5,3:3.5}/5")


def test_an_empty_value_still_names_its_dimension_in_text() -> None:
    _both_twins_text(SparseVector(9), b"{}/9")


def test_the_text_form_round_trips() -> None:
    sparse = SparseVector(1000, {1: 1.5, 17: -0.25, 999: 8.0})
    _both_twins_text(sparse, b"{1:1.5,17:-0.25,999:8.0}/1000")


def test_text_indices_are_the_one_based_ones_the_value_holds() -> None:
    for codec in (pure, native):
        decoded = codec._decode_value(SPARSEVEC_OID, 0, b"{1:1.5,3:3.5}/5")
        assert decoded.indices == (1, 3)
        assert decoded.values == (1.5, 3.5)
        assert decoded.dim == 5


def test_a_list_is_refused_because_it_is_the_dense_type() -> None:
    for codec in (pure, native):
        with pytest.raises(TypeError, match="requires a SparseVector"):
            codec._encode_binary([1.0, 2.0], SPARSEVEC_OID)


def test_a_dict_is_refused_because_it_names_no_dimension() -> None:
    for codec in (pure, native):
        with pytest.raises(TypeError, match="names no dimension"):
            codec._encode_binary({1: 1.5}, SPARSEVEC_OID)


def test_a_truncated_header_is_a_protocol_error_not_an_index_error() -> None:
    # `_pgdriver` raises its own `ProtocolError` for a wire-format fault and
    # `codec.c` raises `ValueError`, exactly as for `vector` and `halfvec`.
    for codec in (pure, native):
        with pytest.raises((ProtocolError, ValueError), match="header is truncated"):
            codec._decode_value(SPARSEVEC_OID, 1, b"\x00\x00\x00\x05")


def test_a_length_that_disagrees_with_the_count_is_refused() -> None:
    wire = struct.pack("!iii", 5, 2, 0) + struct.pack("!ii", 0, 2)
    for codec in (pure, native):
        with pytest.raises((ProtocolError, ValueError), match="element count"):
            codec._decode_value(SPARSEVEC_OID, 1, wire)


def test_a_reserved_word_that_is_not_zero_is_refused() -> None:
    wire = struct.pack("!iii", 5, 0, 1)
    for codec in (pure, native):
        with pytest.raises((ProtocolError, ValueError), match="flags 1"):
            codec._decode_value(SPARSEVEC_OID, 1, wire)


def test_a_wire_index_outside_the_dimension_is_refused() -> None:
    wire = struct.pack("!iii", 3, 1, 0) + struct.pack("!i", 7) + struct.pack("!f", 1.0)
    for codec in (pure, native):
        with pytest.raises((ProtocolError, ValueError), match=r"outside 0\.\.2"):
            codec._decode_value(SPARSEVEC_OID, 1, wire)


def test_a_count_beyond_pgvectors_ceiling_is_refused_before_it_is_a_length() -> None:
    wire = struct.pack("!iii", 100_000, MAX_SPARSEVEC_NNZ + 1, 0)
    for codec in (pure, native):
        with pytest.raises((ProtocolError, ValueError), match="element count"):
            codec._decode_value(SPARSEVEC_OID, 1, wire)


def test_malformed_text_is_refused_rather_than_half_parsed() -> None:
    for bad in (b"{1:1.5}", b"1:1.5/5", b"{1:1.5}5"):
        for codec in (pure, native):
            with pytest.raises((ProtocolError, ValueError), match="text-format"):
                codec._decode_value(SPARSEVEC_OID, 0, bad)


def test_a_text_element_missing_its_value_is_refused() -> None:
    for codec in (pure, native):
        with pytest.raises((ProtocolError, ValueError)):
            codec._decode_value(SPARSEVEC_OID, 0, b"{1}/5")


def test_the_declared_sql_names_the_dimension() -> None:
    assert Sparsevec(30000).sql == "sparsevec(30000)"


def test_the_extension_is_vector_not_sparsevec() -> None:
    assert Sparsevec(30000).extension == "vector"
    assert Sparsevec(30000).type_name == "sparsevec"


def test_a_dimension_mismatch_is_refused_at_assignment() -> None:
    with pytest.raises(ValueError, match="dimension 5, got one of dimension 6"):
        Sparsevec(5).coerce(SparseVector(6, {1: 1.0}))


def test_a_list_is_refused_by_the_column_too() -> None:
    with pytest.raises(TypeError, match="SparseVector"):
        Sparsevec(5).coerce([1.0, 2.0, 3.0, 4.0, 5.0])


def test_a_nonsense_dimension_is_refused_at_declaration() -> None:
    for bad in (0, -1, MAX_SPARSEVEC_DIM + 1):
        with pytest.raises(DeclarationError, match="out of range"):
            Sparsevec(bad)
    with pytest.raises(DeclarationError, match="int dimension"):
        Sparsevec(3.0)


@contextlib.contextmanager
def _fake_oid(column):
    """Lend `column` this suite's invented OID, then take it back.

    Assigning `.oid` directly is the right narrow setup -- these tests are about
    `to_wire` reading it, not about codec registration -- but it cannot be left in
    place. `ExtensionType.__init__` appends *every* instance to the process-global
    `_DECLARED_EXTENSION_TYPES`, permanently, so a throwaway that keeps a made-up
    OID is a `sparsevec` entry that disagrees with the server for the rest of the
    interpreter. The next `bind_extension_oid("sparsevec", real_oid)` then refuses,
    which is how `tests/orm/test_sparsevec_live.py` came to fail five ways in a
    full-directory run while passing on its own. Zero means "unresolved" and
    binding accepts it, so restoring it makes the lingering entry harmless.
    """
    column.oid = SPARSEVEC_OID
    try:
        yield column
    finally:
        column.oid = 0


def test_the_shape_token_is_name_derived_like_every_extension_type() -> None:
    assert Sparsevec(30).shape_value == b"xsparsevec(30)"


def test_a_bound_value_carries_its_oid_without_mutating_the_callers() -> None:
    column = Sparsevec(5)
    with _fake_oid(column):
        mine = SparseVector(5, {1: 1.5})
        bound = column.to_wire(mine)
        assert bound == mine
        assert bound._pg_oid == SPARSEVEC_OID
        assert mine._pg_oid == 0


def test_a_bound_value_is_what_parameter_inference_reads() -> None:
    column = Sparsevec(5)
    with _fake_oid(column):
        assert pure._infer_oid(column.to_wire(SparseVector(5, {1: 1.5}))) == SPARSEVEC_OID
