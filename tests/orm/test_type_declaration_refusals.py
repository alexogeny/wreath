"""Every declaration-time refusal in `orm/types.py`, exercised.

`wreath mutant --path src/wreath/orm/types.py --tests tests/orm` reported 29
survivors and 20 unreached controls, and they had one thing in common: they were
all *validation*. The types themselves were well covered -- round trips, wire
formats, parity between twins -- and the branches that refuse a bad declaration or
a bad value were not, so removing any of them left the suite green.

That is the worst place to have no coverage, because these messages are the ones a
person meets first. A refusal that stops refusing does not break a round trip; it
accepts a `Vector` of strings, or a `TsVector` over a column name that is not a
string, and the failure surfaces much later as something else.

Each test here names the refusal it pins rather than grouping by type, so a
message that changes shows up against the property it was protecting.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

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

# -- timestamps: naive and aware are different types, not a formatting choice ---


def test_timestamp_refuses_a_non_datetime() -> None:
    with pytest.raises(TypeError, match="datetime"):
        Timestamp.coerce("2026-07-30T00:00:00")


def test_timestamp_refuses_an_aware_datetime() -> None:
    """`timestamp` has no zone, so accepting an aware value would silently drop it.

    That is the whole failure this refusal exists for: the value inserts fine and
    reads back shifted, with nothing anywhere recording which zone was discarded.
    """
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
    """The mirror image, and the more dangerous direction.

    A naive value accepted here is interpreted in the server's `TimeZone`, so the
    same code inserts different instants on two machines.
    """
    with pytest.raises(TypeError, match="aware"):
        TimestampTz.coerce(datetime.datetime(2026, 7, 30, 12, 0))


def test_timestamptz_accepts_an_aware_datetime() -> None:
    aware = datetime.datetime(2026, 7, 30, 12, 0, tzinfo=datetime.UTC)
    assert TimestampTz.coerce(aware) == aware


# -- numeric: Decimal or int, never float --------------------------------------


@pytest.mark.parametrize("value", [Decimal("1.25"), Decimal("0"), 7, -3])
def test_numeric_accepts_decimal_and_int(value: object) -> None:
    assert Numeric.coerce(value) == value


@pytest.mark.parametrize("value", [1.25, "1.25", None, True])
def test_numeric_refuses_float_str_and_bool(value: object) -> None:
    """`float` is refused deliberately, and it is the interesting one.

    `numeric` is exact and binary floating point is not, so accepting `0.1` here
    would store a value that is not the one written -- in the one column type
    people choose *because* they need exactness, usually for money.
    """
    with pytest.raises(TypeError, match="Decimal or int"):
        Numeric.coerce(value)


# -- Array(): the declaration refusals -----------------------------------------


def test_array_requires_a_pgtype_element() -> None:
    with pytest.raises(TypeError, match="PgType element"):
        Array("text")  # type: ignore[arg-type]


def test_array_refuses_to_nest() -> None:
    """PostgreSQL's own arrays are not nested, they are multidimensional.

    `text[][]` is not a different type from `text[]` in the catalog, so accepting a
    nested declaration would produce a type that reads back as something else.
    """
    with pytest.raises(TypeError, match="nested arrays"):
        Array(TextArray)


def test_array_refuses_an_element_with_no_array_type() -> None:
    """Reachable only through a hand-made `PgType`, which is a supported thing to make.

    Every built-in scalar here has an array OID, and the one non-scalar
    (`TextArray`) is caught by the nested-array check first -- so this branch is
    unreachable from the shipped types alone. `PgType` is public, though: a
    declaration for an extension type or a type wreath does not ship yet lands
    exactly here, and the message has to name the element rather than failing later
    with an array OID of `None`.
    """
    from wreath.orm.types import PgType

    exotic = PgType("citext", 16385, "citext", lambda value: value)
    with pytest.raises(TypeError, match="no array type"):
        Array(exotic)


def test_array_refuses_a_value_that_is_not_a_sequence() -> None:
    with pytest.raises(TypeError, match="list or tuple"):
        Array(Int64).coerce("not a list")


def test_array_elements_are_not_nullable_by_default() -> None:
    """A `NULL` element is a different declaration, not an accident to tolerate."""
    with pytest.raises(TypeError, match="not nullable"):
        Array(Int64).coerce([1, None, 3])


def test_nullable_array_elements_survive_both_wire_directions() -> None:
    """The `None if item is None else ...` conditionals in `to_wire`/`from_wire`.

    Both survived mutation because nothing passed a `None` element through an array
    that allows one -- so a mutant that ran the element codec on `None` was never
    noticed.
    """
    column = Array(Int64, nullable_elements=True)
    assert column.coerce([1, None, 3]) == [1, None, 3]
    assert column.to_wire([1, None, 3]) == [1, None, 3]
    assert column.from_wire([1, None, 3]) == [1, None, 3]


def test_a_jsonb_array_round_trips_none_elements_through_its_codec() -> None:
    """The same conditionals where the element codec actually transforms.

    `Int64`'s wire conversion is the identity, so it cannot show that `None` skips
    the codec rather than being passed to it. `Jsonb` encodes, so it can.
    """
    column = Array(Text, nullable_elements=True)
    assert column.to_wire(["a", None]) == ["a", None]
    assert column.from_wire(["a", None]) == ["a", None]


# -- Vector(): declaration and value refusals ----------------------------------


@pytest.mark.parametrize("bad", [1.0, "1536", None, True, False])
def test_vector_refuses_a_non_int_dimension(bad: object) -> None:
    """`True` is in here on purpose: it is an `int`, and `Vector(True)` is not a
    one-dimensional vector anybody meant to declare."""
    with pytest.raises(DeclarationError, match="int dimension"):
        Vector(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, 16001])
def test_vector_refuses_an_out_of_range_dimension(bad: int) -> None:
    with pytest.raises(DeclarationError, match="out of range"):
        Vector(bad)


@pytest.mark.parametrize("bad", ["[1,2,3]", 1.5, None, {"a": 1}])
def test_vector_refuses_a_value_that_is_not_a_sequence(bad: object) -> None:
    """A `str` is iterable and of a plausible length, which is why this matters."""
    with pytest.raises(TypeError, match="list or tuple"):
        Vector(3).coerce(bad)


@pytest.mark.parametrize("bad", [True, "1.0", None, [1.0]])
def test_vector_refuses_a_non_numeric_element(bad: object) -> None:
    with pytest.raises(TypeError, match="float"):
        Vector(2).coerce([1.0, bad])


# -- TsVector(): every declaration refusal -------------------------------------


@pytest.mark.parametrize("bad", [None, 1, ["english"]])
def test_tsvector_refuses_a_non_string_configuration(bad: object) -> None:
    with pytest.raises(DeclarationError, match="text-search configuration"):
        TsVector(bad, sources=("title",))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["english\n", "en glish", "english;DROP", "", "eng-lish"])
def test_tsvector_refuses_a_configuration_that_is_not_an_identifier(bad: str) -> None:
    """The configuration is interpolated into `to_tsvector('...', ...)`.

    So the pattern is the boundary, and the newline case is the one that matters:
    `^...$` in a non-fullmatch dialect would accept `"english\\n"` and put a
    newline inside a SQL literal.
    """
    with pytest.raises(DeclarationError, match="text-search configuration"):
        TsVector(bad, sources=("title",))


def test_tsvector_refuses_a_bare_string_of_sources() -> None:
    """`sources="title"` would otherwise iterate into five one-letter columns."""
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
    """A column counted twice is weighted twice, silently."""
    with pytest.raises(DeclarationError, match="same column twice"):
        TsVector("english", sources=("title", "body", "title"))


def test_tsvector_accepts_a_valid_declaration() -> None:
    column = TsVector("english", sources=("title", "body"))
    assert column.config == "english"
    assert column.sources == ("title", "body")


def test_a_tsvector_column_refuses_to_be_written() -> None:
    """It is `GENERATED ALWAYS AS ... STORED`, so PostgreSQL computes it."""
    with pytest.raises(TypeError, match="generated"):
        TsVector("english", sources=("title",)).coerce("anything")


# -- bind_extension_oid(): the OID guard ---------------------------------------


@pytest.mark.parametrize("bad", [0, -1, -999])
def test_binding_an_invalid_oid_is_refused(bad: int) -> None:
    """Zero is the dangerous one: it means "unspecified" on the wire.

    So a type bound to 0 would look resolved and frame parameters against no type
    at all, which is why this is a refusal rather than a no-op.
    """
    with pytest.raises(ValueError, match="invalid OID"):
        bind_extension_oid("vector", bad)


def test_binding_a_name_no_type_declared_binds_nothing() -> None:
    """Returns 0 rather than raising, and registers no codec.

    The `if bound:` guard: a name nothing declared must not reach
    `register_extension_codec`, or a typo in a resolution query would install a
    codec for an OID no column uses.
    """
    assert bind_extension_oid("nosuchtype", 987999) == 0


def test_require_oid_returns_the_oid_once_the_type_is_resolved() -> None:
    """The success half of `require_oid`, which nothing asserted.

    Only the *refusal* was covered, so a mutant that made the guard always fire --
    raising even for a resolved type -- survived. `require_oid` is documented as
    callable wherever the OID decides something, so the non-raising path is part of
    its contract and not just an internal fall-through.

    The OID is assigned on the instance rather than through `bind_extension_oid`,
    deliberately. That function writes *process-global* codec state keyed on the
    type name, and `tests/orm/test_vector_codec.py` has already bound `vector` to a
    different number by the time this runs -- so calling it here raised, and only in
    a full-directory run. `require_oid` reads `self.oid` and nothing else, so the
    narrower setup is also the more honest one.

    **The assignment has to be undone, and avoiding `bind_extension_oid` is not
    enough on its own.** `ExtensionType.__init__` appends *every* instance to the
    process-global `_DECLARED_EXTENSION_TYPES`, permanently -- so this throwaway
    stays a `vector` entry carrying a foreign OID for the rest of the interpreter,
    and the next `bind_extension_oid("vector", real_oid)` refuses: 987001 is
    neither 0 nor the OID the server assigned. That made every test in
    `test_vector_queries.py` error at setup, but only once a live `pgvector`
    supplied a real OID to disagree with -- so it stayed hidden for as long as
    nobody ran `tests/orm` with `WREATH_TEST_POSTGRES_DSN` set. Restoring 0 leaves
    the entry harmless, because 0 means "unresolved" and binding accepts it.
    """
    column = Vector(4)
    try:
        column.oid = 987001
        assert column.require_oid("a test") == 987001
    finally:
        column.oid = 0


def test_a_none_element_stays_null_through_both_wire_directions() -> None:
    """Why `wreath mutant` reports `Array`'s two `None if item is None` conditionals
    as survivors, and why that is correct rather than a coverage gap.

    They are **redundant**, not untested. `PgType.to_wire` and `PgType.from_wire`
    already return `None` unchanged before consulting the element codec, so
    `element.to_wire(None)` is `None` too and both arms of the conditional produce
    the same answer for every element type there is. No test can distinguish them
    because no behaviour distinguishes them.

    This asserts the property the conditionals were written to protect -- a `NULL`
    element stays `NULL` rather than becoming the JSON literal `null` -- with an
    element codec that genuinely transforms, so the guarantee is checked at the
    layer that actually provides it.
    """
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
    """The refusal half of `require_oid`, asserted directly on the method.

    `to_wire` reaches it only after its own `if self.oid == 0`, so a mutant that
    stopped `require_oid` from raising left the suite green: the caller's guard
    still fired, and the value went on to be framed against OID 0 -- which means
    "unspecified" on the wire, not "invalid". Asserting the method itself is what
    pins the refusal to the place that owns it.
    """
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


# -- SparseVector: the value class, whose validation lives in Python only -------
#
# `_sparsevec.py` deliberately does not restate its bounds in C -- two copies of a
# check are two chances to disagree about what pgvector accepts -- which makes this
# module the *only* place a bad sparse value is refused. A mutant sweep found every
# one of these branches unreached: the codec tests build valid values and round
# trip them, and `test_sparsevec_live.py` covers the bounds but skips without a
# DSN, so an unguarded build reported the refusals as untested.


def test_a_sparse_vector_refuses_a_non_int_dimension() -> None:
    """`dim.__class__ is not int`, which is also what rejects a bool.

    `True` is an `int` by inheritance, so `isinstance` would accept it; comparing
    `__class__` is what makes `SparseVector(True)` a refusal rather than a
    one-dimensional vector.
    """
    for bad in (1.0, "5", None, True, False, Decimal(5)):
        with pytest.raises(TypeError, match="dimension must be int"):
            SparseVector(bad, {1: 1.0})


@pytest.mark.parametrize("dim", [0, -1, MAX_SPARSEVEC_DIM + 1])
def test_a_sparse_vector_refuses_a_dimension_out_of_pgvectors_range(dim: int) -> None:
    with pytest.raises(ValueError, match="out of range"):
        SparseVector(dim, {1: 1.0})


def test_the_largest_dimension_pgvector_allows_is_accepted() -> None:
    """The other side of the bound, which is what makes it a bound.

    Without this, widening `MAX_SPARSEVEC_DIM` past anything reachable leaves the
    suite green -- the refusal test above only pins that *something* is refused.
    `tests/orm/test_sparsevec_live.py` proves this number is pgvector's own.
    """
    value = SparseVector(MAX_SPARSEVEC_DIM, {MAX_SPARSEVEC_DIM: 1.0})
    assert value.dim == MAX_SPARSEVEC_DIM
    assert value.indices == (MAX_SPARSEVEC_DIM,)


def test_a_sparse_vector_refuses_a_non_int_index() -> None:
    with pytest.raises(TypeError, match="must be int"):
        SparseVector(5, {"1": 1.0})


@pytest.mark.parametrize("index", [0, -1, 6])
def test_a_sparse_vector_refuses_an_index_outside_its_dimension(index: int) -> None:
    """Indices are 1-based, so 0 is out of range and `dim` itself is not."""
    with pytest.raises(ValueError, match="1-based"):
        SparseVector(5, {index: 1.0})


def test_the_first_and_last_index_are_both_inside_the_range() -> None:
    """The 1-based boundary from both ends, since an off-by-one hides at exactly these."""
    assert SparseVector(5, {1: 1.0}).indices == (1,)
    assert SparseVector(5, {5: 1.0}).indices == (5,)


def test_more_non_zero_elements_than_pgvector_stores_is_refused() -> None:
    dense = dict.fromkeys(range(1, MAX_SPARSEVEC_NNZ + 2), 1.0)
    with pytest.raises(ValueError, match="at most 16000 non-zero"):
        SparseVector(MAX_SPARSEVEC_NNZ + 1, dense)


def test_exactly_as_many_non_zero_elements_as_pgvector_stores_is_accepted() -> None:
    """The bound's accepting side, without which it can be widened undetectably."""
    dense = dict.fromkeys(range(1, MAX_SPARSEVEC_NNZ + 1), 1.0)
    value = SparseVector(MAX_SPARSEVEC_NNZ, dense)
    assert len(value.indices) == MAX_SPARSEVEC_NNZ


def test_elements_may_be_any_mapping_or_pair_sequence_not_only_a_dict() -> None:
    """`elements if isinstance(elements, dict) else dict(elements)` -- both arms.

    A dict is used as given; anything else is converted. Nothing distinguished the
    two paths before this, so a mutant pinning either arm survived, and the
    documented "or an iterable of pairs" half of the signature was never exercised.
    """
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
    """The accepting side of `Sparsevec`'s `value.dim != dim` guard.

    The refusal is covered in `test_sparsevec_codec.py`; forcing that guard to
    fire *always* left the suite green, which means nothing asserted that a
    correctly dimensioned value gets through at all.
    """
    from wreath.orm.types import Sparsevec

    pg_type = Sparsevec(5)
    value = SparseVector(5, {2: 1.0})
    assert pg_type.coerce(value) is value
    with pytest.raises(ValueError, match="dimension 5"):
        pg_type.coerce(SparseVector(4, {2: 1.0}))
