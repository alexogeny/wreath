from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from wreath import _sparsevec
from wreath._sparsevec import MAX_SPARSEVEC_DIM, MAX_SPARSEVEC_NNZ, SparseVector
from wreath.orm.errors import DeclarationError
from wreath.orm.types import (
    Array,
    Int64,
    Numeric,
    Text,
    TextArray,
    Timestamp,
    TimestampTz,
    TsVector,
    Vector,
    bind_extension_oid,
)


def test_timestamp_refuses_a_non_datetime() -> None:
    with pytest.raises(TypeError, match="datetime"):
        Timestamp.coerce("2026-07-30T00:00:00")


def test_timestamp_refuses_an_aware_datetime() -> None:
    aware = datetime.datetime(2026, 7, 30, tzinfo=datetime.UTC)
    with pytest.raises(TypeError, match="naive"):
        Timestamp.coerce(aware)


def test_timestamp_accepts_a_naive_datetime() -> None:
    naive = datetime.datetime(2026, 7, 30, 12, 0)
    assert Timestamp.coerce(naive) == naive


def test_timestamptz_refuses_a_non_datetime() -> None:
    with pytest.raises(TypeError, match="datetime"):
        TimestampTz.coerce(1753833600)


def test_timestamptz_refuses_a_naive_datetime() -> None:
    with pytest.raises(TypeError, match="aware"):
        TimestampTz.coerce(datetime.datetime(2026, 7, 30, 12, 0))


def test_timestamptz_accepts_an_aware_datetime() -> None:
    aware = datetime.datetime(2026, 7, 30, 12, 0, tzinfo=datetime.UTC)
    assert TimestampTz.coerce(aware) == aware


@pytest.mark.parametrize("value", [Decimal("1.25"), Decimal("0"), 7, -3])
def test_numeric_accepts_decimal_and_int(value: object) -> None:
    assert Numeric.coerce(value) == value


@pytest.mark.parametrize("value", [1.25, "1.25", None, True])
def test_numeric_refuses_float_str_and_bool(value: object) -> None:
    with pytest.raises(TypeError, match="Decimal or int"):
        Numeric.coerce(value)


def test_array_requires_a_pgtype_element() -> None:
    with pytest.raises(TypeError, match="PgType element"):
        Array("text")  # type: ignore[arg-type]


def test_array_refuses_to_nest() -> None:
    with pytest.raises(TypeError, match="nested arrays"):
        Array(TextArray)


def test_array_refuses_an_element_with_no_array_type() -> None:
    from wreath.orm.types import PgType

    exotic = PgType("citext", 16385, "citext", lambda value: value)
    with pytest.raises(TypeError, match="no array type"):
        Array(exotic)


def test_array_refuses_a_value_that_is_not_a_sequence() -> None:
    with pytest.raises(TypeError, match="list or tuple"):
        Array(Int64).coerce("not a list")


def test_array_elements_are_not_nullable_by_default() -> None:
    with pytest.raises(TypeError, match="not nullable"):
        Array(Int64).coerce([1, None, 3])


def test_nullable_array_elements_survive_both_wire_directions() -> None:
    column = Array(Int64, nullable_elements=True)
    assert column.coerce([1, None, 3]) == [1, None, 3]
    assert column.to_wire([1, None, 3]) == [1, None, 3]
    assert column.from_wire([1, None, 3]) == [1, None, 3]


def test_a_jsonb_array_round_trips_none_elements_through_its_codec() -> None:
    column = Array(Text, nullable_elements=True)
    assert column.to_wire(["a", None]) == ["a", None]
    assert column.from_wire(["a", None]) == ["a", None]


@pytest.mark.parametrize("bad", [1.0, "1536", None, True, False])
def test_vector_refuses_a_non_int_dimension(bad: object) -> None:
    with pytest.raises(DeclarationError, match="int dimension"):
        Vector(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, 16001])
def test_vector_refuses_an_out_of_range_dimension(bad: int) -> None:
    with pytest.raises(DeclarationError, match="out of range"):
        Vector(bad)


@pytest.mark.parametrize("bad", ["[1,2,3]", 1.5, None, {"a": 1}])
def test_vector_refuses_a_value_that_is_not_a_sequence(bad: object) -> None:
    with pytest.raises(TypeError, match="list or tuple"):
        Vector(3).coerce(bad)


@pytest.mark.parametrize("bad", [True, "1.0", None, [1.0]])
def test_vector_refuses_a_non_numeric_element(bad: object) -> None:
    with pytest.raises(TypeError, match="float"):
        Vector(2).coerce([1.0, bad])


@pytest.mark.parametrize("bad", [None, 1, ["english"]])
def test_tsvector_refuses_a_non_string_configuration(bad: object) -> None:
    with pytest.raises(DeclarationError, match="text-search configuration"):
        TsVector(bad, sources=("title",))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["english\n", "en glish", "english;DROP", "", "eng-lish"])
def test_tsvector_refuses_a_configuration_that_is_not_an_identifier(bad: str) -> None:
    with pytest.raises(DeclarationError, match="text-search configuration"):
        TsVector(bad, sources=("title",))


def test_tsvector_refuses_a_bare_string_of_sources() -> None:
    with pytest.raises(DeclarationError, match="sequence of column names"):
        TsVector("english", sources="title")


@pytest.mark.parametrize("bad", [None, 7, {"title": 1}])
def test_tsvector_refuses_sources_that_are_not_a_sequence(bad: object) -> None:
    with pytest.raises(DeclarationError, match="sequence of column names"):
        TsVector("english", sources=bad)


def test_tsvector_refuses_an_empty_sources_list() -> None:
    with pytest.raises(DeclarationError, match="at least one column"):
        TsVector("english", sources=())


@pytest.mark.parametrize("bad", ["", None, 3])
def test_tsvector_refuses_a_source_that_is_not_a_column_name(bad: object) -> None:
    with pytest.raises(DeclarationError, match="is not a column name"):
        TsVector("english", sources=("title", bad))


def test_tsvector_refuses_a_repeated_source() -> None:
    with pytest.raises(DeclarationError, match="same column twice"):
        TsVector("english", sources=("title", "body", "title"))


def test_tsvector_accepts_a_valid_declaration() -> None:
    column = TsVector("english", sources=("title", "body"))
    assert column.config == "english"
    assert column.sources == ("title", "body")


def test_a_tsvector_column_refuses_to_be_written() -> None:
    with pytest.raises(TypeError, match="generated"):
        TsVector("english", sources=("title",)).coerce("anything")


@pytest.mark.parametrize("bad", [0, -1, -999])
def test_binding_an_invalid_oid_is_refused(bad: int) -> None:
    with pytest.raises(ValueError, match="invalid OID"):
        bind_extension_oid("vector", bad)


def test_binding_a_name_no_type_declared_binds_nothing() -> None:
    assert bind_extension_oid("nosuchtype", 987999) == 0


def test_require_oid_returns_the_oid_once_the_type_is_resolved() -> None:
    column = Vector(4)
    try:
        column.oid = 987001
        assert column.require_oid("a test") == 987001
    finally:
        column.oid = 0


def test_a_none_element_stays_null_through_both_wire_directions() -> None:
    from wreath.orm.types import Jsonb

    column = Array(Jsonb, nullable_elements=True)
    assert Jsonb.to_wire(None) is None
    assert Jsonb.to_wire({"a": 1}) != {"a": 1}, "Jsonb must actually transform"

    wired = column.to_wire([{"a": 1}, None])
    assert wired[1] is None, "a NULL element must stay NULL, not become b'null'"
    assert wired[0] == Jsonb.to_wire({"a": 1})

    back = column.from_wire([Jsonb.to_wire({"a": 1}), None])
    assert back == [{"a": 1}, None]


def test_an_unbound_extension_type_refuses_to_name_its_oid() -> None:
    from wreath.orm.errors import ExtensionNotInstalledError

    # A freshly declared type is unresolved: `bind_extension_oid` walks the types
    # declared *when it was called*, so this one was not there to be bound. No
    # global state is touched, which is what keeps this independent of suite order.
    column = Vector(8)
    assert column.oid == 0
    with pytest.raises(ExtensionNotInstalledError, match="has no OID yet"):
        column.require_oid("a test")
    with pytest.raises(ExtensionNotInstalledError):
        column.to_wire([0.0] * 8)


# `_sparsevec.py` deliberately does not restate its bounds in C -- two copies of a
# check are two chances to disagree about what pgvector accepts -- which makes this
# module the *only* place a bad sparse value is refused. A mutant sweep found every
# one of these branches unreached: the codec tests build valid values and round
# trip them, and `test_sparsevec_live.py` covers the bounds but skips without a
# DSN, so an unguarded build reported the refusals as untested.


def test_a_sparse_vector_refuses_a_non_int_dimension() -> None:
    for bad in (1.0, "5", None, True, False, Decimal(5)):
        with pytest.raises(TypeError, match="dimension must be int"):
            SparseVector(bad, {1: 1.0})


@pytest.mark.parametrize("dim", [0, -1, MAX_SPARSEVEC_DIM + 1])
def test_a_sparse_vector_refuses_a_dimension_out_of_pgvectors_range(dim: int) -> None:
    with pytest.raises(ValueError, match="out of range"):
        SparseVector(dim, {1: 1.0})


def test_the_largest_dimension_pgvector_allows_is_accepted() -> None:
    value = SparseVector(MAX_SPARSEVEC_DIM, {MAX_SPARSEVEC_DIM: 1.0})
    assert value.dim == MAX_SPARSEVEC_DIM
    assert value.indices == (MAX_SPARSEVEC_DIM,)


def test_the_sparse_vector_dimension_bound_is_pgvectors_literal_contract() -> None:
    assert _sparsevec.MAX_SPARSEVEC_DIM == 1_000_000_000
    # Execute the owning module during the baseline so its import-time value
    # mutation selects this test rather than relying on collection-time import.
    assert _sparsevec.SparseVector(1).dim == 1


def test_a_sparse_vector_refuses_a_non_int_index() -> None:
    with pytest.raises(TypeError, match="must be int"):
        SparseVector(5, {"1": 1.0})


@pytest.mark.parametrize("index", [0, -1, 6])
def test_a_sparse_vector_refuses_an_index_outside_its_dimension(index: int) -> None:
    with pytest.raises(ValueError, match="1-based"):
        SparseVector(5, {index: 1.0})


def test_the_first_and_last_index_are_both_inside_the_range() -> None:
    assert SparseVector(5, {1: 1.0}).indices == (1,)
    assert SparseVector(5, {5: 1.0}).indices == (5,)


def test_more_non_zero_elements_than_pgvector_stores_is_refused() -> None:
    dense = dict.fromkeys(range(1, MAX_SPARSEVEC_NNZ + 2), 1.0)
    with pytest.raises(ValueError, match="at most 16000 non-zero"):
        SparseVector(MAX_SPARSEVEC_NNZ + 1, dense)


def test_exactly_as_many_non_zero_elements_as_pgvector_stores_is_accepted() -> None:
    dense = dict.fromkeys(range(1, MAX_SPARSEVEC_NNZ + 1), 1.0)
    value = SparseVector(MAX_SPARSEVEC_NNZ, dense)
    assert len(value.indices) == MAX_SPARSEVEC_NNZ


def test_elements_may_be_any_mapping_or_pair_sequence_not_only_a_dict() -> None:
    from collections import OrderedDict

    as_dict = SparseVector(5, {3: 1.5, 1: 0.5})
    as_pairs = SparseVector(5, [(3, 1.5), (1, 0.5)])
    as_generator = SparseVector(5, ((index, value) for index, value in ((3, 1.5), (1, 0.5))))
    as_ordered = SparseVector(5, OrderedDict(((3, 1.5), (1, 0.5))))
    # Ascending by index whichever way it arrived, so the wire order is not the
    # caller's insertion order.
    for built in (as_dict, as_pairs, as_generator, as_ordered):
        assert built.indices == (1, 3)
        assert built.values == (0.5, 1.5)
        assert built == as_dict


def test_a_matching_dimension_passes_the_sparsevec_column_coercion() -> None:
    from wreath.orm.types import Sparsevec

    pg_type = Sparsevec(5)
    value = SparseVector(5, {2: 1.0})
    assert pg_type.coerce(value) is value
    with pytest.raises(ValueError, match="dimension 5"):
        pg_type.coerce(SparseVector(4, {2: 1.0}))
