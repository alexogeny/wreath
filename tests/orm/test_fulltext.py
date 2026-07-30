"""Full-text search: `matches`, `rank`, and what they compile to.

The shape of the feature is that the two halves are separate calls.
`.matches()` is the predicate and can use the GIN index; `.rank()` is a number
PostgreSQL computes per surviving row and cannot index. Writing a search as one
call would hide which half costs what, so a search filters with `@@` and orders
by the rank of what survived.

`websearch_to_tsquery` is the default parser for one reason, and the live half
is where it is proved: it is the one that does not raise on user input.
`to_tsquery('english', '"')` is a syntax error, which is a 500 when the string
came from a search box; `websearch_to_tsquery` treats the same byte as a
character. The hostile-input cases below are executed, not merely compiled.

Skipped unless ``WREATH_TEST_POSTGRES_DSN`` points at a throwaway database --
but full-text search needs no extension, so unlike the vector suites these run
against any PostgreSQL.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from wreath.orm import DeclarationError, Mapped, Model, column
from wreath.orm.compiler import compile_select, shape_of
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text, TsVector
from wreath.postgres import PostgresError, connect
from wreath.queries import Param, Queries, query

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

#: One schema per xdist worker; see `tests/orm/test_in_subquery_live.py` for why
#: sharing one races on `CREATE SCHEMA`.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_fulltext_{_WORKER}"


class Database:
    name = "main"


class Document(Model, table="documents", schema=_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    title: Mapped[str] = column(Text)
    body: Mapped[str] = column(Text)
    search: Mapped[bytes] = column(
        TsVector("english", sources=("title", "body")), index="gin"
    )


class Plain(Model, table="plain", schema=_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    body: Mapped[str] = column(Text)


class Documents(Queries[Document]):
    matching = query(Document.search.matches(Param("terms"))).order_by(
        Document.search.rank(Param("terms")).desc()
    )


@pytest.fixture(scope="module")
def registry() -> Registry:
    return Registry(Database(), [Document, Plain], validate_schema="off")


def _sql(registry: Registry, select: Any) -> str:
    return compile_select(registry, select).sql


# -- rendering ----------------------------------------------------------------


def test_matches_renders_the_websearch_parser_by_default(registry: Registry) -> None:
    sql = _sql(registry, Document.select().where(Document.search.matches("llamas")))
    assert (
        'WHERE "t0"."search" @@ websearch_to_tsquery(\'english\', $1)' in sql
    ), sql


def test_matches_can_ask_for_the_operator_parser(registry: Registry) -> None:
    sql = _sql(
        registry,
        Document.select().where(
            Document.search.matches("llamas & alpacas", parser="to_tsquery")
        ),
    )
    assert 'WHERE "t0"."search" @@ to_tsquery(\'english\', $1)' in sql


def test_rank_renders_as_a_function_call_over_both_operands(
    registry: Registry,
) -> None:
    sql = _sql(
        registry, Document.select().order_by(Document.search.rank("llamas").desc())
    )
    assert (
        "ORDER BY ts_rank(\"t0\".\"search\", websearch_to_tsquery('english', $1)) DESC"
        in sql
    ), sql


def test_the_configuration_comes_from_the_column_not_the_call(
    registry: Registry,
) -> None:
    """A query analysed under another configuration matches nothing.

    That failure reads as missing data rather than as a mistake, so the
    configuration is never a call argument -- it is whatever the column was
    declared with.
    """

    class Other(Model, table="other", schema=_SCHEMA):
        id: Mapped[int] = column(Int64, primary_key=True)
        body: Mapped[str] = column(Text)
        search: Mapped[bytes] = column(TsVector("simple", sources=("body",)))

    other = Registry(Database(), [Other], validate_schema="off")
    assert "websearch_to_tsquery('simple'," in _sql(
        other, Other.select().where(Other.search.matches("x"))
    )


def test_search_text_is_bound_never_inlined(registry: Registry) -> None:
    compiled = compile_select(
        registry, Document.select().where(Document.search.matches("'; drop table t --"))
    )
    assert "drop table" not in compiled.sql
    assert compiled.bind_values == ("'; drop table t --",)


def test_a_filter_and_an_ordering_bind_in_emission_order(registry: Registry) -> None:
    compiled = compile_select(
        registry,
        Document.select()
        .where(Document.search.matches("llamas"))
        .order_by(Document.search.rank("llamas").desc())
        .limit(5),
    )
    assert compiled.sql.index("$1") < compiled.sql.index("$2") < compiled.sql.index("$3")
    assert compiled.bind_values == ("llamas", "llamas", 5)
    assert compiled.bind_oids == (25, 25, 20)


def test_a_rank_threshold_is_a_predicate(registry: Registry) -> None:
    sql = _sql(
        registry, Document.select().where(Document.search.rank("llamas") > 0.1)
    )
    assert (
        "WHERE ts_rank(\"t0\".\"search\", websearch_to_tsquery('english', $1)) > $2"
        in sql
    ), sql


# -- shape keys ---------------------------------------------------------------


def test_two_parsers_are_two_plans(registry: Registry) -> None:
    websearch = Document.select().where(Document.search.matches("x"))
    tsquery = Document.select().where(Document.search.matches("x", parser="to_tsquery"))
    assert shape_of(registry, websearch) != shape_of(registry, tsquery)


def test_the_search_text_is_not_in_the_plan_cache_key(registry: Registry) -> None:
    first = Document.select().where(Document.search.matches("llamas"))
    second = Document.select().where(Document.search.matches("alpacas"))
    assert shape_of(registry, first) == shape_of(registry, second)


def test_a_rank_ordering_takes_the_pure_keyer(registry: Registry) -> None:
    # The native keyer reads `ordering.expression.column`, which a function call
    # does not have; `plain_orderings` is the flag that routes it.
    ranked = Document.select().order_by(Document.search.rank("x").desc())
    assert not ranked.plain_orderings
    assert shape_of(registry, ranked)


# -- refusals -----------------------------------------------------------------


def test_matches_requires_a_tsvector_column() -> None:
    with pytest.raises(DeclarationError, match="requires a TsVector column"):
        Plain.body.matches("x")


def test_rank_requires_a_tsvector_column() -> None:
    with pytest.raises(DeclarationError, match="requires a TsVector column"):
        Plain.body.rank("x")


def test_an_unknown_parser_is_refused() -> None:
    with pytest.raises(DeclarationError, match="websearch_to_tsquery is the default"):
        Document.search.matches("x", parser="plainto_tsquery")


def test_where_refuses_a_bare_rank() -> None:
    with pytest.raises(TypeError, match="yields a relevance score"):
        Document.select().where(Document.search.rank("x"))


def test_ordering_by_a_match_is_refused() -> None:
    with pytest.raises(DeclarationError, match="orders by a distance or a rank"):
        Document.search.matches("x").desc()


def test_the_renderer_rechecks_the_text_search_configuration() -> None:
    """The compiler's copy of the config check is the one that writes it out.

    Declaration refuses a bad configuration, so this second copy is only ever
    reached by a `pg_type` that did not come through `TsVector(...)` -- which is
    exactly why it is checked with `fullmatch`. `$` matches immediately before a
    trailing newline, so the anchored form here returned `"english\\n"` and put
    it inside `to_tsvector('...', ...)`.
    """
    from types import SimpleNamespace

    from wreath.orm.compiler import _text_search_config
    from wreath.orm.errors import ORMError

    def node(config: Any) -> Any:
        return SimpleNamespace(
            operator="@@ websearch_to_tsquery",
            left=SimpleNamespace(
                column=SimpleNamespace(pg_type=SimpleNamespace(config=config))
            ),
        )

    assert _text_search_config(node("english")) == "english"
    with pytest.raises(ORMError, match="text-search configuration"):
        _text_search_config(node("english\n"))


def test_a_generated_column_cannot_be_assigned() -> None:
    document = Document(id=1, title="a", body="b")
    with pytest.raises(TypeError, match="generated"):
        document.search = b"x"


def test_a_generated_column_cannot_be_constructed() -> None:
    with pytest.raises(TypeError, match="generated"):
        Document(id=1, title="a", body="b", search=b"x")


def test_a_generated_column_is_not_required_by_the_constructor() -> None:
    # It is not nullable and has no default, which for any other column is a
    # TypeError. It stays unloaded, which is what puts it in RETURNING.
    document = Document(id=1, title="a", body="b")
    assert document.title == "a"


# -- against a real server ----------------------------------------------------
#
# Everything below executes. Full-text search needs no extension, so these
# should genuinely run wherever the DSN points.

_live = pytest.mark.skipif(
    _DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for live full-text tests"
)

_ROWS = [
    (1, "Llama husbandry", "llamas graze quietly and spit rarely"),
    (2, "Alpaca husbandry", "alpacas are not llamas, whatever they say"),
    (3, "Tractor maintenance", "diesel, grease, and no llamas at all"),
]


async def _live_schema(connection: Any) -> None:
    await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
    await connection.execute(f'CREATE SCHEMA "{_SCHEMA}"')
    await connection.execute(
        f'CREATE TABLE "{_SCHEMA}"."documents" ('
        " id bigint primary key,"
        " title text not null,"
        " body text not null,"
        " search tsvector generated always as ("
        "   to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,''))"
        " ) stored not null)"
    )
    await connection.execute(
        f'CREATE INDEX ON "{_SCHEMA}"."documents" USING gin (search)'
    )
    for identifier, title, body in _ROWS:
        await connection.execute(
            f'INSERT INTO "{_SCHEMA}"."documents" (id, title, body) '
            "VALUES ($1, $2, $3)",
            identifier,
            title,
            body,
        )


@_live
@pytest.mark.asyncio
@pytest.mark.database
async def test_websearch_survives_hostile_input(registry: Registry) -> None:
    """The reason `websearch_to_tsquery` is the default, executed.

    Each of these is a syntax error to `to_tsquery`. The assertion is that the
    statement *runs* -- how many rows it matches is the parser's business, not
    wreath's.
    """
    connection = await connect(_DSN)
    try:
        await _live_schema(connection)
        hostile_terms = ['"', "&", "!", ":", "'", "llamas & ", "!!!", '"unclosed']
        for hostile in hostile_terms:
            compiled = compile_select(
                registry, Document.select().where(Document.search.matches(hostile))
            )
            rows = await connection.fetch(compiled.sql, *compiled.bind_values)
            assert isinstance(rows, list)
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.close()


@_live
@pytest.mark.asyncio
@pytest.mark.database
async def test_to_tsquery_is_the_one_that_raises(registry: Registry) -> None:
    """The other half of the trade, so the default is a choice and not a habit.

    `llamas &` is what a search box produces the moment somebody types an
    ampersand and a space. `websearch_to_tsquery` matches on `llamas`;
    `to_tsquery` raises `no operand in tsquery`, which reaches the client as a
    500 unless the caller was expecting it.
    """
    connection = await connect(_DSN)
    try:
        await _live_schema(connection)
        strict = compile_select(
            registry,
            Document.select().where(
                Document.search.matches("llamas &", parser="to_tsquery")
            ),
        )
        with pytest.raises(PostgresError, match="no operand in tsquery"):
            await connection.fetch(strict.sql, *strict.bind_values)
        forgiving = compile_select(
            registry,
            Document.select(Document.id).where(Document.search.matches("llamas &")),
        )
        rows = await connection.fetch(forgiving.sql, *forgiving.bind_values)
        assert sorted(row[0] for row in rows) == [1, 2, 3]
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.close()


@_live
@pytest.mark.asyncio
@pytest.mark.database
async def test_matches_finds_the_rows_the_words_are_in(registry: Registry) -> None:
    connection = await connect(_DSN)
    try:
        await _live_schema(connection)
        compiled = compile_select(
            registry,
            Document.select(Document.id).where(Document.search.matches("husbandry")),
        )
        rows = await connection.fetch(compiled.sql, *compiled.bind_values)
        assert sorted(row[0] for row in rows) == [1, 2]
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.close()


@_live
@pytest.mark.asyncio
@pytest.mark.database
async def test_ts_rank_orders_by_relevance(registry: Registry) -> None:
    """The document with `llamas` in the *title* as well outranks the others."""
    connection = await connect(_DSN)
    try:
        await _live_schema(connection)
        compiled = compile_select(
            registry,
            Document.select(Document.id)
            .where(Document.search.matches("llamas"))
            .order_by(Document.search.rank("llamas").desc()),
        )
        rows = await connection.fetch(compiled.sql, *compiled.bind_values)
        assert [row[0] for row in rows][0] == 1
        assert sorted(row[0] for row in rows) == [1, 2, 3]
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.close()


@_live
@pytest.mark.asyncio
@pytest.mark.database
async def test_the_generated_column_stays_correct_after_an_update(
    registry: Registry,
) -> None:
    """The whole reason for GENERATED ... STORED rather than a trigger.

    PostgreSQL recomputes the column inside the statement that changed the
    source, so there is no window where the index disagrees with the row.
    """
    connection = await connect(_DSN)
    try:
        await _live_schema(connection)
        before = compile_select(
            registry,
            Document.select(Document.id).where(Document.search.matches("tractor")),
        )
        assert [row[0] for row in await connection.fetch(
            before.sql, *before.bind_values
        )] == [3]

        await connection.execute(
            f'UPDATE "{_SCHEMA}"."documents" SET title = $1 WHERE id = 3',
            "Combine harvester",
        )

        rows = await connection.fetch(before.sql, *before.bind_values)
        assert rows == []
        after = compile_select(
            registry,
            Document.select(Document.id).where(Document.search.matches("harvester")),
        )
        assert [row[0] for row in await connection.fetch(
            after.sql, *after.bind_values
        )] == [3]
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.close()


@_live
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_declared_query_runs_twice_with_different_terms(
    registry: Registry,
) -> None:
    """Prepared once, reused after -- the shape that has failed here before.

    A declared query binds the same parameter into two positions (the filter and
    the rank), so this also pins that the second execution reuses the plan rather
    than transposing the values.
    """
    connection = await connect(_DSN)
    try:
        await _live_schema(connection)
        declaration = Documents.declarations()["matching"]
        assert declaration.parameters == ("terms",)
        for terms, expected in (("husbandry", [1, 2]), ("tractor", [3])):
            compiled = compile_select(registry, declaration.bind(terms=terms))
            rows = await connection.fetch(compiled.sql, *compiled.bind_values)
            assert sorted(row[0] for row in rows) == expected
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.close()
