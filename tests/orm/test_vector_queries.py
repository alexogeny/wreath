"""The four pgvector distance operators, and what they compile to.

Each is named for what it computes rather than for its symbol, and each yields a
*number*. That is the whole shape of the feature: a distance is an ORDER BY key
(the similarity search) or the left side of a threshold comparison, and it is
never a predicate on its own -- `where()` refuses one, because PostgreSQL would
refuse it later with a message about the argument of WHERE rather than about the
line that wrote it.

The live half asserts on the *plan*: an approximate index only earns its build
cost if the planner actually uses it, and `ORDER BY embedding <=> $1` is the
only shape that lets it.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from wreath.orm import DeclarationError, Mapped, Model, column
from wreath.orm.compiler import compile_select, shape_of
from wreath.orm.registry import Registry
from wreath.orm.types import (
    Int64,
    Text,
    Vector,
    _unbind_extension_oids,
    bind_extension_oid,
    declared_extension_types,
)
from wreath.postgres import connect
from wreath.queries import Param, Queries, query

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

#: Shared with `tests/orm/test_extension_oid.py` and `tests/migrations/test_vector.py`;
#: a process resolves an extension type exactly once.
VECTOR_OID = 987654

QUERY = [0.1, 0.2, 0.3]


class Database:
    name = "main"


class Document(Model, table="documents", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)
    body: Mapped[str] = column(Text)
    embedding: Mapped[list] = column(
        Vector(3), index="hnsw", index_ops="vector_cosine_ops"
    )


class Documents(Queries[Document]):
    nearest = (
        query()
        .order_by(Document.embedding.cosine_distance(Param("q")))
        .limit(5)
    )


def _vector_oid() -> int:
    """The OID this process holds for `vector`, binding every declaration.

    A process resolves an extension type exactly once -- that is the invariant
    `tests/orm/test_extension_oid.py` pins -- so a suite that needs no server
    must not insist on its *own* made-up OID when a live suite has already read
    the real one out of a catalog. Whichever it is, it is consistent within the
    run, which is all these assertions need. Finding one bound instance does
    not mean every instance is bound: another test can assign an OID directly
    to one local type. Run the idempotent binder even in that case so this
    module's model declaration cannot inherit an order-dependent OID 0.
    """
    oid = VECTOR_OID
    for item in declared_extension_types():
        if item.type_name == "vector" and item.oid:
            oid = item.oid
            break
    bind_extension_oid("vector", oid)
    return oid


@pytest.fixture
def registry() -> Registry:
    _vector_oid()
    return Registry(Database(), [Document], validate_schema="off")


def _sql(registry: Registry, select: Any) -> str:
    return compile_select(registry, select).sql


# -- rendering ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "operator"),
    [
        ("l2_distance", "<->"),
        ("cosine_distance", "<=>"),
        ("inner_product", "<#>"),
        ("l1_distance", "<+>"),
    ],
)
def test_each_operator_compiles_to_its_symbol(
    registry: Registry, method: str, operator: str
) -> None:
    distance = getattr(Document.embedding, method)(QUERY)
    sql = _sql(registry, Document.select().order_by(distance))
    assert f'ORDER BY ("t0"."embedding" {operator} $1) ASC' in sql


def test_a_distance_orders_descending_too(registry: Registry) -> None:
    sql = _sql(
        registry,
        Document.select().order_by(Document.embedding.cosine_distance(QUERY).desc()),
    )
    assert 'ORDER BY ("t0"."embedding" <=> $1) DESC' in sql


def test_the_order_by_value_binds_between_the_where_and_the_limit(
    registry: Registry,
) -> None:
    """Placeholder numbering follows emission order, and so must bind order."""
    compiled = compile_select(
        registry,
        Document.select()
        .where(Document.id > 10)
        .order_by(Document.embedding.cosine_distance(QUERY))
        .limit(5),
    )
    assert 'WHERE "t0"."id" > $1' in compiled.sql
    assert '<=> $2' in compiled.sql
    assert "LIMIT $3" in compiled.sql
    assert compiled.bind_oids == (20, _vector_oid(), 20)


def test_a_threshold_comparison_is_a_predicate(registry: Registry) -> None:
    sql = _sql(
        registry,
        Document.select().where(Document.embedding.cosine_distance(QUERY) < 0.3),
    )
    assert 'WHERE ("t0"."embedding" <=> $1) < $2' in sql


def test_ordering_by_a_distance_and_a_column_together(registry: Registry) -> None:
    sql = _sql(
        registry,
        Document.select().order_by(
            Document.embedding.cosine_distance(QUERY), Document.id.desc()
        ),
    )
    assert 'ORDER BY ("t0"."embedding" <=> $1) ASC, "t0"."id" DESC' in sql


# -- the plan-cache key -------------------------------------------------------


def test_two_distances_over_one_column_are_two_plans(registry: Registry) -> None:
    cosine = Document.select().order_by(Document.embedding.cosine_distance(QUERY))
    l2 = Document.select().order_by(Document.embedding.l2_distance(QUERY))
    assert shape_of(registry, cosine) != shape_of(registry, l2)


def test_the_query_vector_is_not_in_the_key(registry: Registry) -> None:
    one = Document.select().order_by(Document.embedding.cosine_distance([1.0, 0.0, 0.0]))
    two = Document.select().order_by(Document.embedding.cosine_distance([0.0, 1.0, 0.0]))
    assert shape_of(registry, one) == shape_of(registry, two)


def test_direction_is_in_the_key(registry: Registry) -> None:
    up = Document.select().order_by(Document.embedding.cosine_distance(QUERY))
    down = Document.select().order_by(Document.embedding.cosine_distance(QUERY).desc())
    assert shape_of(registry, up) != shape_of(registry, down)


def test_a_plain_column_ordering_still_takes_the_native_keyer(
    registry: Registry,
) -> None:
    plain = Document.select().order_by(Document.id)
    assert plain.plain_orderings
    assert shape_of(registry, plain)


# -- declared queries ---------------------------------------------------------


def test_a_declared_query_binds_the_vector_per_call(registry: Registry) -> None:
    declaration = Documents.declarations()["nearest"]
    assert declaration.parameters == ("q",)
    bound = declaration.bind(q=QUERY)
    assert 'ORDER BY ("t0"."embedding" <=> $1) ASC' in _sql(registry, bound)


def test_a_declared_query_keeps_one_shape_across_calls(registry: Registry) -> None:
    declaration = Documents.declarations()["nearest"]
    first = shape_of(registry, declaration.bind(q=[1.0, 0.0, 0.0]))
    second = shape_of(registry, declaration.bind(q=[0.0, 1.0, 0.0]))
    assert first == second


def test_a_declared_query_coerces_its_parameter(registry: Registry) -> None:
    declaration = Documents.declarations()["nearest"]
    with pytest.raises(ValueError, match="exactly 3 values"):
        declaration.bind(q=[1.0, 2.0])


# -- refusals -----------------------------------------------------------------


def test_a_distance_alone_is_not_a_predicate() -> None:
    with pytest.raises(TypeError, match="yields a distance"):
        Document.select().where(Document.embedding.cosine_distance(QUERY))


def test_a_distance_requires_a_vector_column() -> None:
    # Three column types carry these four operators now -- `vector`, `halfvec`
    # and `sparsevec` -- so the refusal names all three. `bit` is the one that
    # does not; see `tests/orm/test_binary_quantization_queries.py`.
    with pytest.raises(DeclarationError, match="Vector, Halfvec or Sparsevec"):
        Document.body.cosine_distance(QUERY)


def test_ordering_by_an_ordinary_comparison_is_refused() -> None:
    with pytest.raises(DeclarationError, match="yields a"):
        Document.select().order_by(Document.id > 3)


def test_a_wrong_length_query_vector_fails_at_the_call_site() -> None:
    with pytest.raises(ValueError, match="exactly 3 values"):
        Document.embedding.cosine_distance([1.0, 2.0])


# -- against a real database --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.database
async def test_ordering_by_a_distance_uses_the_index() -> None:
    """The plan, not the answer.

    An HNSW index costs real time to build; the only thing that repays it is the
    planner choosing it, and it only can for `ORDER BY column <=> constant`. A
    test that asserted on the rows would pass just as happily against a
    sequential scan.
    """
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for vector query plan tests")
    schema = f"wreath_vec_{uuid.uuid4().hex[:12]}"
    db = await connect(_DSN)
    try:
        try:
            await db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:  # noqa: BLE001 - reported as a skip, see below
            pytest.skip("this PostgreSQL has no pgvector; use pgvector/pgvector:pg17")
        await db.execute(f'CREATE SCHEMA "{schema}"')
        await db.execute(
            f'CREATE TABLE "{schema}"."documents" '
            "(id bigint primary key, body text not null, embedding vector(3) not null)"
        )
        await db.execute(
            f'INSERT INTO "{schema}"."documents" '
            "SELECT g, 'body ' || g, "
            "format('[%s,%s,%s]', random(), random(), random())::vector "
            "FROM generate_series(1, 2000) AS g"
        )
        await db.execute(
            f'CREATE INDEX ON "{schema}"."documents" '
            "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
        )
        await db.execute(f'ANALYZE "{schema}"."documents"')
        # EXPLAIN answers one row per plan line, and the interesting line is
        # never the first -- reading only `fetchval` would assert on "Limit".
        rows = await db.fetch(
            f'EXPLAIN (FORMAT TEXT) SELECT id FROM "{schema}"."documents" '
            "ORDER BY embedding <=> '[0.1,0.2,0.3]'::vector LIMIT 5"
        )
        text = "\n".join(str(row[0]) for row in rows)
        # `Order By:` under an Index Scan is the property that matters: it is
        # the plan node only an index that can *answer an ordering* produces,
        # which for a vector column means the HNSW index and nothing else.
        assert "Index Scan" in text, text
        assert "Order By: (embedding <=>" in text, text

        # ... and the shape that cannot use it, so the assertion above is not
        # merely true of every plan this table produces.
        rows = await db.fetch(
            f'EXPLAIN (FORMAT TEXT) SELECT id FROM "{schema}"."documents" '
            "WHERE embedding <=> '[0.1,0.2,0.3]'::vector < 0.5"
        )
        assert "Seq Scan" in "\n".join(str(row[0]) for row in rows)
    finally:
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()


#: One schema per xdist worker; workers sharing one race on CREATE SCHEMA and
#: PostgreSQL reports the race as a catalog unique violation, which reads like
#: anything except a test-isolation bug.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_LIVE_SCHEMA = f"wreath_vector_{_WORKER}"


class LiveDocument(Model, table="documents", schema=_LIVE_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    body: Mapped[str] = column(Text)
    embedding: Mapped[list] = column(Vector(3))


@pytest.fixture
async def live() -> Any:
    """A started registry over a real vector table, or a skip that says why."""
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for live vector tests")
    from wreath.orm.introspection import resolve_extension_types
    from wreath.orm.session import Session as _Session  # noqa: F401 - documents intent
    from wreath.postgres import Database, PoolConfig

    database = Database(
        "vector-live", _DSN, pools={"write": PoolConfig(min_size=1, max_size=3)}
    )
    await database.start()
    connection = await database.acquire("write")
    try:
        try:
            await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:  # noqa: BLE001 - reported as a skip on the next line
            pytest.skip("this PostgreSQL has no pgvector; use pgvector/pgvector:pg17")
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_LIVE_SCHEMA}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{_LIVE_SCHEMA}"')
        await connection.execute(
            f'CREATE TABLE "{_LIVE_SCHEMA}"."documents" '
            "(id bigint PRIMARY KEY, body text NOT NULL, embedding vector(3) NOT NULL)"
        )
        await connection.execute(
            f'INSERT INTO "{_LIVE_SCHEMA}"."documents" (id, body, embedding) VALUES '
            "(1, 'east', '[1,0,0]'), (2, 'north', '[0,1,0]'), (3, 'up', '[0,0,1]')"
        )
    finally:
        await database.release("write", connection)
    built = Registry(database, [LiveDocument], validate_schema="off")
    # The unit tests above bound a made-up OID, which is the correct thing for a
    # suite that needs no server -- but this process now has to hold the real
    # one. Only a test is ever allowed to do this; see the helper's docstring.
    _unbind_extension_oids()
    # The startup step under test: the OID is read from the catalog, not guessed.
    await resolve_extension_types(built)
    try:
        yield built
    finally:
        connection = await database.acquire("write")
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{_LIVE_SCHEMA}" CASCADE')
        finally:
            await database.release("write", connection)
        await database.stop()


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_similarity_search_returns_the_nearest_row_twice(live: Any) -> None:
    """The codec, the OID resolution, and the ORDER BY bind, end to end.

    Run twice on purpose. A statement's first execution exchanges text and every
    one after it exchanges binary, and a shape that works once and fails forever
    after has reached a default code path in this repository before.
    """
    from wreath.orm.session import Session

    session = Session(live, "write")
    try:
        for _ in range(2):
            rows = await session.fetch(
                LiveDocument.select()
                .order_by(LiveDocument.embedding.cosine_distance([0.9, 0.1, 0.0]))
                .limit(2)
            )
            assert [row.id for row in rows] == [1, 2]
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_vector_column_hydrates_as_a_list_of_floats(live: Any) -> None:
    from wreath.orm.session import Session

    session = Session(live, "write")
    try:
        for _ in range(2):  # cold (text) then warm (binary)
            rows = await session.fetch(
                LiveDocument.select().where(LiveDocument.id == 2)
            )
            assert [row.embedding for row in rows] == [[0.0, 1.0, 0.0]]
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_distance_threshold_filters_against_the_server(live: Any) -> None:
    from wreath.orm.session import Session

    session = Session(live, "write")
    try:
        for _ in range(2):
            rows = await session.fetch(
                LiveDocument.select().where(
                    LiveDocument.embedding.cosine_distance([1.0, 0.0, 0.0]) < 0.5
                )
            )
            assert [row.id for row in rows] == [1]
    finally:
        await session.close()
