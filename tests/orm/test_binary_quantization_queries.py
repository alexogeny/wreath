"""The two `bit` distances and the three `sparsevec` ones, and what they compile to.

`test_vector_queries.py` covers the four dense-vector operators. These are the
ones that arrived with the other two column types, and the reason they are a
separate file is that they are *type-gated*: `<~>` and `<%>` exist over
PostgreSQL's `bit` and nothing else, and the four dense operators exist over
`vector`, `halfvec` and `sparsevec` and not over `bit`. A method that compiles
against the wrong column type produces SQL PostgreSQL rejects with a message
about an undefined operator, naming neither the column nor the line that wrote
it -- so the refusals here matter as much as the emissions.
"""

from __future__ import annotations

import pytest

from wreath.orm import DeclarationError, Mapped, Model, column
from wreath.orm.compiler import compile_select
from wreath.orm.registry import Registry
from wreath.orm.types import (
    Bit,
    Int64,
    Sparsevec,
    SparseVector,
    Text,
    bind_extension_oid,
    declared_extension_types,
)
from wreath.queries import Param, Queries, query

#: Shared with `tests/orm/test_sparsevec_codec.py`; a process resolves an
#: extension type exactly once, so the number has to agree across suites.
SPARSEVEC_OID = 987656

TERMS = SparseVector(30, {2: 1.0, 17: 0.5})
SIGNATURE = "1010101010101010"


class Database:
    name = "main"


class Document(Model, table="documents", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)
    body: Mapped[str] = column(Text)
    signature: Mapped[str] = column(
        Bit(16), index="hnsw", index_ops="bit_hamming_ops"
    )
    terms: Mapped[SparseVector] = column(
        Sparsevec(30), index="hnsw", index_ops="sparsevec_l2_ops"
    )


class Documents(Queries[Document]):
    nearest = (
        query()
        .order_by(Document.signature.hamming_distance(Param("q")))
        .limit(5)
    )


def _sparsevec_oid() -> int:
    """The OID this process holds for `sparsevec`, binding a plausible one if none.

    Binds unconditionally rather than returning early on the first bound type it
    finds. `bind_extension_oid` walks the types declared *when it is called*, and
    the ones declared in this module may not have existed when some earlier suite
    called it -- an early return would leave this file's columns on OID 0 while
    reporting success. It is idempotent for a repeated identical OID, which is
    what makes binding again the cheap answer rather than a conflict.
    """
    oid = next(
        (
            item.oid
            for item in declared_extension_types()
            if item.type_name == "sparsevec" and item.oid
        ),
        SPARSEVEC_OID,
    )
    bind_extension_oid("sparsevec", oid)
    return oid


@pytest.fixture
def registry() -> Registry:
    _sparsevec_oid()
    return Registry(Database(), [Document], validate_schema="off")


def _sql(registry: Registry, select: object) -> str:
    return compile_select(registry, select).sql


# -- the bit distances --------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "operator"),
    [("hamming_distance", "<~>"), ("jaccard_distance", "<%>")],
)
def test_each_bit_operator_compiles_to_its_symbol(
    registry: Registry, method: str, operator: str
) -> None:
    distance = getattr(Document.signature, method)(SIGNATURE)
    sql = _sql(registry, Document.select().order_by(distance))
    assert f'ORDER BY ("t0"."signature" {operator} $1) ASC' in sql


def test_a_bit_distance_orders_descending_too(registry: Registry) -> None:
    sql = _sql(
        registry,
        Document.select().order_by(
            Document.signature.hamming_distance(SIGNATURE).desc()
        ),
    )
    assert 'ORDER BY ("t0"."signature" <~> $1) DESC' in sql


def test_a_bit_distance_is_a_threshold_on_the_left(registry: Registry) -> None:
    """A hamming distance is a count, so comparing it is the natural filter."""
    compiled = compile_select(
        registry,
        Document.select().where(Document.signature.hamming_distance(SIGNATURE) < 4),
    )
    assert '("t0"."signature" <~> $1) < $2' in compiled.sql


def test_a_bit_distance_is_not_a_predicate_on_its_own(registry: Registry) -> None:
    """PostgreSQL would refuse it later, with a message about WHERE's argument."""
    with pytest.raises(TypeError, match="yields a distance"):
        Document.select().where(Document.signature.hamming_distance(SIGNATURE))


def test_the_bit_value_binds_as_a_parameter_not_as_a_literal(
    registry: Registry,
) -> None:
    compiled = compile_select(
        registry,
        Document.select()
        .where(Document.id > 10)
        .order_by(Document.signature.hamming_distance(SIGNATURE))
        .limit(5),
    )
    assert 'WHERE "t0"."id" > $1' in compiled.sql
    assert "<~> $2" in compiled.sql
    assert "LIMIT $3" in compiled.sql
    assert SIGNATURE not in compiled.sql
    assert compiled.bind_values[1] == SIGNATURE
    assert compiled.bind_oids == (20, 1560, 20)


def test_a_declared_query_binds_the_signature_per_call(registry: Registry) -> None:
    declaration = Documents.declarations()["nearest"]
    assert declaration.parameters == ("q",)
    bound = declaration.bind(q=SIGNATURE)
    assert 'ORDER BY ("t0"."signature" <~> $1) ASC' in _sql(registry, bound)


# -- the sparsevec distances --------------------------------------------------


@pytest.mark.parametrize(
    ("method", "operator"),
    [
        ("l2_distance", "<->"),
        ("cosine_distance", "<=>"),
        ("inner_product", "<#>"),
        ("l1_distance", "<+>"),
    ],
)
def test_a_sparsevec_takes_the_dense_operators(
    registry: Registry, method: str, operator: str
) -> None:
    """Same four operators as `vector`; only the operand type differs."""
    distance = getattr(Document.terms, method)(TERMS)
    sql = _sql(registry, Document.select().order_by(distance))
    assert f'ORDER BY ("t0"."terms" {operator} $1) ASC' in sql


def test_the_sparse_value_reaches_the_parameters_as_a_sparse_vector(
    registry: Registry,
) -> None:
    compiled = compile_select(
        registry, Document.select().order_by(Document.terms.l2_distance(TERMS))
    )
    assert compiled.bind_values == (TERMS,)
    assert compiled.bind_oids == (_sparsevec_oid(),)


# -- the type gate ------------------------------------------------------------


def test_a_bit_column_refuses_the_dense_operators() -> None:
    """`bit` has no `<=>`; the refusal names the type it actually is."""
    with pytest.raises(DeclarationError, match="not bit"):
        Document.signature.cosine_distance([1.0])


def test_a_sparsevec_column_refuses_the_bit_operators() -> None:
    with pytest.raises(DeclarationError, match="requires a Bit column"):
        Document.terms.hamming_distance(SIGNATURE)


def test_a_plain_text_column_refuses_both_families() -> None:
    with pytest.raises(DeclarationError, match="requires a Bit column"):
        Document.body.jaccard_distance(SIGNATURE)
    with pytest.raises(DeclarationError, match="Vector, Halfvec or Sparsevec"):
        Document.body.l2_distance([1.0])


def test_the_wrong_dimension_is_refused_before_the_query_is_built(
    registry: Registry,
) -> None:
    with pytest.raises(ValueError, match="dimension 30, got one of dimension 40"):
        _sql(
            registry,
            Document.select().order_by(
                Document.terms.l2_distance(SparseVector(40, {1: 1.0}))
            ),
        )


def test_the_wrong_bit_length_is_refused_before_the_query_is_built(
    registry: Registry,
) -> None:
    with pytest.raises(ValueError, match="exactly 16 bits"):
        _sql(
            registry,
            Document.select().order_by(Document.signature.hamming_distance("101")),
        )
