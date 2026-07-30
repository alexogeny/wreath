"""Hybrid retrieval: two declared searches fused into one ranked answer.

Reciprocal-rank fusion scores a row by where it *placed*, not by what either
search scored it -- `sum(1 / (k + rank))` over the searches that returned it.
That is what makes a vector distance and a `ts_rank` comparable at all: the two
numbers live on different scales and no weighting between them survives a change
of embedding model, but a rank is a rank.

The arithmetic is tested directly, on known rank lists, as well as end to end.
A scoring bug hides very comfortably behind a plausible-looking result set --
"the relevant document is near the top" is true of almost any ordering over a
small fixture -- so the ordering `_fused_order` produces is pinned against
hand-computed scores, and the live half then proves the same ordering comes back
from a real PostgreSQL.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from wreath.orm import DeclarationError, Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text, TsVector, Vector
from wreath.queries import Param, Queries, fuse, query
from wreath.queries import _fused_order as fused_order

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

#: One schema per xdist worker; workers sharing one race on `CREATE SCHEMA`.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_hybrid_{_WORKER}"


class Database:
    name = "main"


class Document(Model, table="documents", schema=_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    title: Mapped[str] = column(Text)
    body: Mapped[str] = column(Text)
    embedding: Mapped[list] = column(Vector(3))
    search: Mapped[bytes] = column(
        TsVector("english", sources=("title", "body")), index="gin"
    )


class Documents(Queries[Document]):
    nearest = (
        query()
        .order_by(Document.embedding.cosine_distance(Param("q")))
        .limit(3)
    )
    matching = (
        query(Document.search.matches(Param("terms")))
        .order_by(Document.search.rank(Param("terms")).desc())
        .limit(3)
    )
    hybrid = fuse(nearest, matching).limit(4)


# -- the arithmetic -----------------------------------------------------------


def test_a_single_ranking_comes_back_in_its_own_order() -> None:
    assert fused_order((("a", "b", "c"),), 60) == ["a", "b", "c"]


def test_placing_in_both_beats_placing_first_in_one() -> None:
    """The property the whole primitive exists for, hand-computed.

    `b` is second in each ranking and `a` is first in one of them:
    b = 1/62 + 1/62 = 0.032258, a = 1/61 = 0.016393.
    """
    order = fused_order((("a", "b"), ("c", "b")), 60)
    assert order == ["b", "a", "c"]


def test_k_decides_how_much_a_first_place_is_worth() -> None:
    """`k` is not decoration: it changes the answer, and this pins which way.

    `a` and `q` place first in one ranking each; `b` places third in both. With
    k=0 the reciprocal is raw (a = q = 1, b = 1/3 + 1/3 = 0.667) and a single
    first place wins. With the conventional k=60 the curve is flat enough that
    agreement wins instead (a = q = 1/61 = 0.016393, b = 2/63 = 0.031746).
    """
    rankings = (("a", "p", "b"), ("q", "r", "b"))
    assert fused_order(rankings, 0)[:2] == ["a", "q"]
    assert fused_order(rankings, 60)[0] == "b"


def test_scores_are_summed_over_every_ranking_a_row_appears_in() -> None:
    rankings = (("a", "b"), ("a", "b"), ("b", "a"))
    # a = 1/61 + 1/61 + 1/62 = 0.048918; b = 1/62 + 1/62 + 1/61 = 0.048653.
    assert fused_order(rankings, 60) == ["a", "b"]


def test_a_tie_is_broken_by_identity_so_the_order_is_stable() -> None:
    """Two rows that placed identically must not come back in hash order.

    `b` and `d` both placed first, `a` and `c` both placed second, so the four
    scores are two pairs of exact equals and only the key separates them.
    """
    assert fused_order((("b", "a"), ("d", "c")), 60) == ["b", "d", "a", "c"]


def test_nothing_in_produces_nothing_out() -> None:
    assert fused_order(((), ()), 60) == []


# -- declaring ----------------------------------------------------------------


def test_a_fusion_takes_the_union_of_its_searches_parameters() -> None:
    assert Documents.hybrid.parameters == ("q", "terms")


def test_a_fusion_knows_its_own_name() -> None:
    assert Documents.hybrid.name == "Documents.hybrid"
    assert repr(Documents.hybrid) == "<fusion Documents.hybrid(q, terms)>"


def test_a_fusion_is_discoverable_by_name() -> None:
    assert set(Documents.fusions()) == {"hybrid"}
    assert set(Documents.declarations()) == {"nearest", "matching"}


def test_a_fusion_carries_its_constant() -> None:
    assert Documents.hybrid.k == 60


def test_the_constant_is_configurable() -> None:
    class Other(Queries[Document]):
        nearest = (
            query()
            .order_by(Document.embedding.cosine_distance(Param("q")))
            .limit(3)
        )
        matching = (
            query(Document.search.matches(Param("terms")))
            .order_by(Document.search.rank(Param("terms")).desc())
            .limit(3)
        )
        hybrid = fuse(nearest, matching, k=5)

    assert Other.hybrid.k == 5


def test_a_shared_parameter_is_named_once() -> None:
    class Shared(Queries[Document]):
        by_title = query(Document.title == Param("word")).order_by(Document.id).limit(3)
        by_body = query(Document.body == Param("word")).order_by(Document.id).limit(3)
        either = fuse(by_title, by_body)

    assert Shared.either.parameters == ("word",)


def test_a_fusion_reuses_the_resolved_declaration_rather_than_a_twin() -> None:
    """Two objects with the same shape are two plan-cache entries.

    The registry associates a prepared plan with the declaration *object*, so a
    fusion that resolved its own private copy of `nearest` would compile the
    same SQL a second time and hold a second entry for it forever.
    """
    assert Documents.hybrid._halves[0] is Documents.declarations()["nearest"]
    assert Documents.hybrid._halves[1] is Documents.declarations()["matching"]


def test_a_fusion_can_name_searches_from_another_query_set() -> None:
    class Later(Queries[Document]):
        hybrid = fuse(Documents.nearest, Documents.matching)

    assert Later.hybrid.parameters == ("q", "terms")


# -- refusals -----------------------------------------------------------------


def _bounded() -> Any:
    return query().order_by(Document.id).limit(3)


def test_fusing_one_search_is_refused() -> None:
    with pytest.raises(DeclarationError, match="at least two"):
        fuse(_bounded())


def test_fusing_something_that_is_not_a_declared_query_is_refused() -> None:
    with pytest.raises(DeclarationError, match="declared queries"):
        fuse(_bounded(), Document.select())


def test_an_unbounded_search_is_refused() -> None:
    """The bound is the point: fusion is over two top-N sets, never a scan."""
    with pytest.raises(DeclarationError, match="limit"):

        class Unbounded(Queries[Document]):
            nearest = query().order_by(
                Document.embedding.cosine_distance(Param("q"))
            )
            matching = (
                query(Document.search.matches(Param("terms")))
                .order_by(Document.id)
                .limit(3)
            )
            hybrid = fuse(nearest, matching)


def test_an_unordered_search_is_refused() -> None:
    """A rank comes from row order, and `LIMIT` without `ORDER BY` has none."""
    with pytest.raises(DeclarationError, match="order_by"):

        class Unordered(Queries[Document]):
            anything = query().limit(3)
            matching = (
                query(Document.search.matches(Param("terms")))
                .order_by(Document.id)
                .limit(3)
            )
            hybrid = fuse(anything, matching)


def test_a_single_row_search_cannot_be_fused() -> None:
    with pytest.raises(DeclarationError, match="one object"):

        class Singular(Queries[Document]):
            first = (
                query(Document.id == Param("id")).order_by(Document.id).limit(3).one()
            )
            matching = (
                query(Document.search.matches(Param("terms")))
                .order_by(Document.id)
                .limit(3)
            )
            hybrid = fuse(first, matching)


def test_a_search_written_inline_is_refused() -> None:
    """A half nothing names is invisible to everything that walks a query set.

    `Queries.declarations()` is how the transitional-column scanner and typegen
    reach a query's `Select`, and it can only report attributes. A half written
    inside the `fuse(...)` call is on no class, so it appears in no listing and
    is silently skipped -- refusing it makes the gap structurally impossible
    rather than documented (ADR 0019).
    """
    with pytest.raises(DeclarationError, match="named attribute"):

        class Inline(Queries[Document]):
            hybrid = fuse(
                query().order_by(Document.embedding.cosine_distance(Param("q"))).limit(3),
                query(Document.search.matches(Param("terms")))
                .order_by(Document.id)
                .limit(3),
            )


def test_one_inline_half_beside_one_named_half_is_still_refused() -> None:
    """The refusal is per search, not "at least one of them is named"."""
    with pytest.raises(DeclarationError, match="named attribute"):

        class HalfInline(Queries[Document]):
            matching = (
                query(Document.search.matches(Param("terms")))
                .order_by(Document.id)
                .limit(3)
            )
            hybrid = fuse(matching, query().order_by(Document.id).limit(3))


def test_a_negative_constant_is_refused() -> None:
    with pytest.raises(ValueError, match="k"):
        fuse(_bounded(), _bounded(), k=-1)


def test_a_fusion_over_two_models_is_refused() -> None:
    class Other(Model, table="others", schema=_SCHEMA):
        id: Mapped[int] = column(Int64, primary_key=True)
        body: Mapped[str] = column(Text)

    class Others(Queries[Other]):
        everything = query().order_by(Other.id).limit(3)

    with pytest.raises(DeclarationError, match="one model"):

        class Mixed(Queries[Document]):
            matching = (
                query(Document.search.matches(Param("terms")))
                .order_by(Document.id)
                .limit(3)
            )
            hybrid = fuse(matching, Others.everything)


def test_a_fusion_cannot_be_extended_after_it_is_declared() -> None:
    with pytest.raises(DeclarationError, match="already declared"):
        Documents.hybrid.limit(2)


def test_a_fusion_needs_a_model() -> None:
    with pytest.raises(DeclarationError, match="names no model"):

        class Modelless(Queries):
            hybrid = fuse(_bounded(), _bounded())


def test_a_fusion_limit_must_be_a_positive_integer() -> None:
    with pytest.raises(ValueError, match="limit"):
        fuse(_bounded(), _bounded()).limit(0)


# -- against a real database --------------------------------------------------


_live = pytest.mark.skipif(
    _DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for hybrid search tests"
)

#: Chosen so that neither half's answer is the fused answer.
#:
#: With `q = [1, 0, 0]` the vector half returns `[1, 2, 3]` (cosine distances 0,
#: 0.2, 0.4; row 4 is orthogonal and does not make the top three) and the text
#: half returns `[2, 4]` -- row 2 says "llama" twice, row 4 once, row 1 and 3
#: not at all. Fusing at k=60:
#:
#:   2 = 1/62 + 1/61 = 0.032522     1 = 1/61 = 0.016393
#:   4 = 1/62        = 0.016129     3 = 1/63 = 0.015873
#:
#: so the answer is [2, 1, 4, 3]. Row 2 is promoted over the nearest vector, and
#: row 4 -- which the vector search never returned at all -- outranks row 3.
_ROWS = [
    (1, "Alpaca grooming", "brushing an alpaca", "[1,0,0]"),
    (2, "Llama husbandry", "keeping llamas well", "[0.8,0.6,0]"),
    (3, "Tractor upkeep", "diesel and grease", "[0.6,0.8,0]"),
    (4, "Trailer fittings", "a trailer for llamas", "[0,0,1]"),
]


@pytest.fixture
async def live() -> Any:
    """A started registry over a table with both a vector and a tsvector."""
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for hybrid search tests")
    from wreath.orm.introspection import resolve_extension_types
    from wreath.orm.types import _unbind_extension_oids
    from wreath.postgres import Database as PgDatabase
    from wreath.postgres import PoolConfig

    database = PgDatabase(
        "hybrid-live", _DSN, pools={"write": PoolConfig(min_size=1, max_size=3)}
    )
    await database.start()
    connection = await database.acquire("write")
    try:
        try:
            await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:  # noqa: BLE001 - reported as a skip on the next line
            pytest.skip("this PostgreSQL has no pgvector; use pgvector/pgvector:pg17")
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{_SCHEMA}"')
        await connection.execute(
            f'CREATE TABLE "{_SCHEMA}"."documents" ('
            " id bigint primary key,"
            " title text not null,"
            " body text not null,"
            " embedding vector(3) not null,"
            " search tsvector generated always as ("
            "   to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,''))"
            " ) stored not null)"
        )
        await connection.execute(
            f'CREATE INDEX ON "{_SCHEMA}"."documents" USING gin (search)'
        )
        for identifier, title, body, embedding in _ROWS:
            await connection.execute(
                f'INSERT INTO "{_SCHEMA}"."documents" (id, title, body, embedding) '
                f"VALUES ($1, $2, $3, '{embedding}'::vector)",
                identifier,
                title,
                body,
            )
    finally:
        await database.release("write", connection)
    built = Registry(database, [Document], validate_schema="off")
    # The unit suites bind a made-up OID, which is correct for a suite that
    # needs no server -- but this process now has to hold the real one.
    _unbind_extension_oids()
    await resolve_extension_types(built)
    try:
        yield built
    finally:
        connection = await database.acquire("write")
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        finally:
            await database.release("write", connection)
        await database.stop()


@_live
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_fusion_returns_the_hand_computed_order(live: Any) -> None:
    """Run twice: a statement's first execution is text and every one after is
    binary, and a shape that works once and fails forever after has reached a
    default code path in this repository before."""
    from wreath.orm.session import Session

    session = Session(live, "write")
    try:
        for _ in range(2):
            found = await Documents(session).hybrid(
                q=[1.0, 0.0, 0.0], terms="llamas"
            )
            assert [item.id for item in found] == [2, 1, 4, 3]
    finally:
        await session.close()


@_live
@pytest.mark.asyncio
@pytest.mark.database
async def test_neither_search_alone_gives_that_answer(live: Any) -> None:
    """So the assertion above is about the fusion and not about the fixture."""
    from wreath.orm.session import Session

    session = Session(live, "write")
    try:
        documents = Documents(session)
        assert [item.id for item in await documents.nearest(q=[1.0, 0.0, 0.0])] == [
            1,
            2,
            3,
        ]
        assert [item.id for item in await documents.matching(terms="llamas")] == [2, 4]
    finally:
        await session.close()


@_live
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_fusion_returns_one_object_per_row(live: Any) -> None:
    """A row both searches returned appears once, and is one object."""
    from wreath.orm.session import Session

    session = Session(live, "write")
    try:
        found = await Documents(session).hybrid(q=[1.0, 0.0, 0.0], terms="llamas")
        assert len({id(item) for item in found}) == len(found)
        assert found[0] is (await Documents(session).nearest(q=[0.8, 0.6, 0.0]))[0]
    finally:
        await session.close()


@_live
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_fusion_stops_at_its_limit(live: Any) -> None:
    from wreath.orm.session import Session

    class Short(Queries[Document]):
        nearest = (
            query()
            .order_by(Document.embedding.cosine_distance(Param("q")))
            .limit(3)
        )
        matching = (
            query(Document.search.matches(Param("terms")))
            .order_by(Document.search.rank(Param("terms")).desc())
            .limit(3)
        )
        hybrid = fuse(nearest, matching).limit(2)

    session = Session(live, "write")
    try:
        found = await Short(session).hybrid(q=[1.0, 0.0, 0.0], terms="llamas")
        assert [item.id for item in found] == [2, 1]
    finally:
        await session.close()


@_live
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_missing_parameter_names_the_fusion(live: Any) -> None:
    from wreath.orm.session import Session

    session = Session(live, "write")
    try:
        with pytest.raises(TypeError, match="Documents.hybrid.. is missing"):
            await Documents(session).hybrid(q=[1.0, 0.0, 0.0])
        with pytest.raises(TypeError, match="unexpected parameter"):
            await Documents(session).hybrid(
                q=[1.0, 0.0, 0.0], terms="llamas", extra=1
            )
    finally:
        await session.close()
