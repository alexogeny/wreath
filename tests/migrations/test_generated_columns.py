"""Generated columns through detect/generate/apply/down.

`GENERATED ALWAYS AS (...) STORED` is the form full-text search needs, because
it is the one that keeps a GIN index correct without a trigger: PostgreSQL
recomputes the column inside the same statement that changed its sources.

Two things about it are structural rather than cosmetic.

* **The expression has to be spelled the way the catalog spells it back.**
  `pg_get_expr` deparses the parse tree, not the text you wrote, so
  `to_tsvector('english', coalesce(title,''))` comes back as
  `to_tsvector('english'::regconfig, COALESCE(title, ''::text))`. If wreath's
  intent and the catalog disagree by one byte, every `detect` run reports drift
  on a column it just created -- forever, with nothing actually wrong.
* **It has to be created after the columns it reads, and dropped before them.**
  Order inside the column block was decided by a content hash, which for this
  model put `search` first and made the whole migration fail on
  `column "title" does not exist`.

These render without a database. `test_catalog_integration.py` and
`tests/orm/test_fulltext.py` are where the two sides meet a real server.
"""

from __future__ import annotations

import importlib
import os
import struct
import uuid
from typing import Any

import pytest

import wreath.migrations as migrations
from wreath.migrations import detect_single, generate_single_plan
from wreath.orm import DeclarationError, Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text, TsVector, Varchar
from wreath.postgres import connect

native: Any = importlib.import_module("wreath._native._postgres")

EMPTY_IMAGE = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"

#: What PostgreSQL 17 deparses the two-source expression back to. Pinned as a
#: literal rather than rebuilt, so a change to the renderer has to be justified
#: against the server rather than against itself.
ENGLISH_TWO = (
    "to_tsvector('english'::regconfig, ((COALESCE(title, ''::text) || ' '::text) "
    "|| COALESCE(body, ''::text)))"
)


class Database:
    name = "main"


class Document(Model, table="documents", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)
    title: Mapped[str] = column(Text)
    body: Mapped[str] = column(Text)
    search: Mapped[bytes] = column(
        TsVector("english", sources=("title", "body")), index="gin"
    )


class Simple(Model, table="documents", schema="app"):
    """The same table analysed under a different configuration."""

    id: Mapped[int] = column(Int64, primary_key=True)
    title: Mapped[str] = column(Text)
    body: Mapped[str] = column(Text)
    search: Mapped[bytes] = column(
        TsVector("simple", sources=("title", "body")), index="gin"
    )


class Titles(Model, table="titles", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)
    title: Mapped[str] = column(Text)
    search: Mapped[bytes] = column(TsVector(sources=("title",)), index="gin")


def _statements(tape: bytes) -> list[tuple[int, str]]:
    offset = 12
    out: list[tuple[int, str]] = []
    for _ in range(struct.unpack_from("<I", tape, 8)[0]):
        flags, length = struct.unpack_from("<II", tape, offset)
        offset += 8
        out.append((flags, tape[offset : offset + length].decode()))
        offset += length
    return out


def _image(*models: type) -> bytes:
    registry = Registry(Database(), list(models), validate_schema="off")
    return migrations._registry_descriptor(registry)


def _sql(desired: bytes, actual: bytes = EMPTY_IMAGE) -> list[tuple[int, str]]:
    plan = native._migration_plan_descriptors(desired, actual)
    return _statements(native._migration_render_sql(plan))


def _forward(*models: type) -> list[str]:
    return [sql for _flags, sql in _sql(_image(*models))]


# -- the expression -----------------------------------------------------------


def test_the_expression_is_rendered_in_postgresqls_normal_form() -> None:
    registry = Registry(Database(), [Document], validate_schema="off")
    spec = registry.spec_for(Document)
    assert spec.by_name["search"].generated_sql == ENGLISH_TWO


def test_a_single_source_is_not_wrapped_in_parentheses() -> None:
    # PostgreSQL parenthesises each `||`, and one source has none to
    # parenthesise. Rebuilding this from the two-source form would add a pair
    # the catalog does not, which is permanent drift.
    registry = Registry(Database(), [Titles], validate_schema="off")
    assert registry.spec_for(Titles).by_name["search"].generated_sql == (
        "to_tsvector('english'::regconfig, COALESCE(title, ''::text))"
    )


def test_a_generated_column_is_created_with_its_expression() -> None:
    assert any(
        f'add column "search" tsvector generated always as ({ENGLISH_TWO}) stored '
        "not null;" in sql
        for sql in _forward(Document)
    )


def test_nothing_about_a_generated_model_falls_back_to_manual() -> None:
    assert not any(flags & 2 for flags, _sql in _sql(_image(Document)))


def test_an_unchanged_generated_column_produces_no_statement() -> None:
    assert _sql(_image(Document), _image(Document)) == []


# -- ordering -----------------------------------------------------------------


def test_the_generated_column_is_added_after_the_columns_it_reads() -> None:
    adds = [sql for sql in _forward(Document) if " add column " in sql]
    positions = {name: index for index, sql in enumerate(adds) for name in
                 ("title", "body", "search") if f'add column "{name}"' in sql}
    assert positions["search"] > positions["title"], adds
    assert positions["search"] > positions["body"], adds


def test_the_generated_column_is_dropped_before_the_columns_it_reads() -> None:
    plan = native._migration_plan_descriptors(_image(Document), EMPTY_IMAGE)
    statements = [
        sql for _flags, sql in _statements(
            native._migration_render_sql(native._migration_reverse_plan(plan))
        )
    ]
    drops = [sql for sql in statements if " drop column " in sql]
    order = [name for sql in drops for name in ("title", "body", "search")
             if f'drop column "{name}"' in sql]
    assert order[0] == "search", drops


def test_down_drops_the_index_and_the_generated_column() -> None:
    plan = native._migration_plan_descriptors(_image(Document), EMPTY_IMAGE)
    reversed_plan = native._migration_reverse_plan(plan)
    statements = _statements(native._migration_render_sql(reversed_plan))
    assert any(sql.startswith("drop index ") for _flags, sql in statements)
    assert any('drop column "search";' in sql for _flags, sql in statements)
    assert not any(flags & 2 for flags, _sql in statements)


# -- drift --------------------------------------------------------------------


def test_a_gin_index_is_created_on_the_generated_column() -> None:
    created = [sql for sql in _forward(Document) if sql.startswith("create index")]
    assert len(created) == 1
    assert 'using gin ("search")' in created[0]


def test_changing_the_configuration_is_surfaced_rather_than_ignored() -> None:
    # `tsvector` has one OID whatever it analyses, so without the expression in
    # the signature this would be a silent no-op.
    statements = _sql(_image(Simple), _image(Document))
    assert statements
    # There is no ALTER that rebuilds a generation expression in place here, so
    # the honest answer is MANUAL rather than a statement that would not do it.
    assert all(flags & 2 for flags, _sql in statements)


def test_changing_the_configuration_moves_the_model_fingerprint() -> None:
    english = Registry(Database(), [Document], validate_schema="off")
    simple = Registry(Database(), [Simple], validate_schema="off")
    assert english.spec_for(Document).fingerprint != simple.spec_for(Simple).fingerprint


def test_an_ordinary_column_signature_is_unchanged_by_this_feature() -> None:
    # Field 5 and field 6 stay empty for a column that is neither generated nor
    # defaulted, so no existing descriptor moves.
    image = _image(Titles)
    assert b"column\x1f25\x1f\x1f1\x1f\x1f\x1f" in image


# -- declaration --------------------------------------------------------------


def test_a_source_must_be_a_declared_column() -> None:
    with pytest.raises(DeclarationError, match="unknown column 'missing'"):

        class Bad(Model, table="bad", schema="app"):
            id: Mapped[int] = column(Int64, primary_key=True)
            search: Mapped[bytes] = column(TsVector(sources=("missing",)))

        Registry(Database(), [Bad], validate_schema="off")


def test_a_non_text_source_is_refused_with_the_reason() -> None:
    with pytest.raises(DeclarationError, match="only text columns"):

        class Bad(Model, table="bad2", schema="app"):
            id: Mapped[int] = column(Int64, primary_key=True)
            title: Mapped[str] = column(Varchar)
            search: Mapped[bytes] = column(TsVector(sources=("title",)))

        Registry(Database(), [Bad], validate_schema="off")


def test_a_generated_source_is_refused() -> None:
    with pytest.raises(DeclarationError, match="itself a generated column"):

        class Bad(Model, table="bad3", schema="app"):
            id: Mapped[int] = column(Int64, primary_key=True)
            title: Mapped[str] = column(Text)
            first: Mapped[bytes] = column(TsVector(sources=("title",)))
            second: Mapped[bytes] = column(TsVector(sources=("first",)))

        Registry(Database(), [Bad], validate_schema="off")


@pytest.mark.parametrize(
    "config",
    [
        "English",
        "eng lish",
        "english'); drop table t --",
        "",
        3,
        # `$` matches immediately before *one* trailing newline, so the
        # anchored `^...$` this validator used accepted `"english\n"` -- which
        # is rendered into `to_tsvector('...', ...)` as literal text. The other
        # two were already refused and are here as the boundary either side.
        "english\n",
        "english\n\n",
        "\nenglish",
    ],
)
def test_a_hostile_configuration_is_refused_at_declaration(config: Any) -> None:
    with pytest.raises(DeclarationError, match="text-search configuration"):
        TsVector(config, sources=("title",))


def test_sources_must_be_a_sequence_of_names() -> None:
    with pytest.raises(DeclarationError, match="sequence of column names"):
        TsVector(sources="title")


def test_sources_must_not_be_empty() -> None:
    with pytest.raises(DeclarationError, match="at least one column"):
        TsVector(sources=())


def test_a_repeated_source_is_refused() -> None:
    with pytest.raises(DeclarationError, match="same column twice"):
        TsVector(sources=("title", "title"))


# -- against a real server ----------------------------------------------------
#
# The rendering above is only worth anything if PostgreSQL deparses the
# expression back to the byte-identical string. That cannot be asserted without
# a server, and getting it wrong is the failure that never resolves: `detect`
# would report drift on a column it just created, forever.

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")


@pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the generated-column catalog round trip",
)
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_generated_column_round_trips_through_the_catalog() -> None:
    schema = f"wreath_generated_{uuid.uuid4().hex[:12]}"

    class Article(Model, table="articles", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        title: Mapped[str] = column(Text)
        body: Mapped[str] = column(Text)
        search: Mapped[bytes] = column(
            TsVector("english", sources=("title", "body")), index="gin"
        )

    registry = Registry(Database(), [Article], validate_schema="off")
    db = await connect(_DSN)
    try:
        await db.execute(f'CREATE SCHEMA "{schema}"')
        generation = await generate_single_plan(registry, db)
        emitted = _statements(generation.sql.tape)
        assert emitted
        assert not any(flags & 2 for flags, _sql in emitted), emitted
        for _flags, statement in emitted:
            await db.execute(statement)

        # The round trip. If wreath's spelling and pg_get_expr's disagree by one
        # byte, this is False and stays False.
        assert (await detect_single(registry, db)).current
        assert _statements((await generate_single_plan(registry, db)).sql.tape) == []
    finally:
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()


@pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the generated-column catalog round trip",
)
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_single_source_column_round_trips_too() -> None:
    """One source has no parentheses at all; two have three pairs."""
    schema = f"wreath_generated_{uuid.uuid4().hex[:12]}"

    class Heading(Model, table="headings", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        title: Mapped[str] = column(Text)
        search: Mapped[bytes] = column(TsVector("simple", sources=("title",)))

    registry = Registry(Database(), [Heading], validate_schema="off")
    db = await connect(_DSN)
    try:
        await db.execute(f'CREATE SCHEMA "{schema}"')
        for _flags, statement in _statements(
            (await generate_single_plan(registry, db)).sql.tape
        ):
            await db.execute(statement)
        assert (await detect_single(registry, db)).current
    finally:
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()
