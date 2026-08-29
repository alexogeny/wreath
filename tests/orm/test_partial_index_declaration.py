from __future__ import annotations

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm._index_predicate import quote_identifier, render_predicate
from wreath.orm.errors import DeclarationError
from wreath.orm.registry import Registry
from wreath.orm.table import all_of, eq, index, is_not_null, is_null, one_of
from wreath.orm.types import Bool, Int64, Text, TimestampTz


class _Database:
    name = "partial-index-declaration"


def _columns(model: type) -> dict:
    registry = Registry(_Database(), [model], validate_schema="off")
    return {item.database_name: item for item in registry.spec_for(model).columns}


class Sample(Model, table="samples", schema="decl"):
    id: Mapped[int] = column(Int64, primary_key=True)
    state: Mapped[str] = column(Text)
    tries: Mapped[int] = column(Int64)
    archived: Mapped[bool] = column(Bool)
    seen_at: Mapped[object] = column(TimestampTz, nullable=True)


COLUMNS = _columns(Sample)


def test_the_three_shapes_render_in_postgres_normal_form() -> None:
    assert render_predicate(eq("state", "ready"), COLUMNS, "S") == "(state = 'ready'::text)"
    assert render_predicate(eq("tries", 0), COLUMNS, "S") == "(tries = 0)"
    assert render_predicate(eq("archived", False), COLUMNS, "S") == "(archived = false)"
    assert render_predicate(is_null("state"), COLUMNS, "S") == "(state IS NULL)"
    assert render_predicate(is_not_null("state"), COLUMNS, "S") == "(state IS NOT NULL)"


def test_in_renders_as_any_array_because_that_is_how_postgres_deparses_it() -> None:
    rendered = render_predicate(one_of("state", ["a", "b"]), COLUMNS, "S")
    assert rendered == "(state = ANY (ARRAY['a'::text, 'b'::text]))"


def test_a_conjunction_keeps_each_operand_parenthesised() -> None:
    rendered = render_predicate(all_of(eq("state", "ready"), eq("tries", 0)), COLUMNS, "S")
    assert rendered == "((state = 'ready'::text) AND (tries = 0))"


def test_a_quote_in_a_literal_is_doubled() -> None:
    assert render_predicate(eq("state", "it's"), COLUMNS, "S") == "(state = 'it''s'::text)"


def test_only_reserved_or_unusual_identifiers_are_quoted() -> None:
    assert quote_identifier("state") == "state"
    assert quote_identifier("dedup_key") == "dedup_key"
    # `group` is a reserved word, so PostgreSQL quotes it in its own output.
    assert quote_identifier("group") == '"group"'
    assert quote_identifier("Mixed") == '"Mixed"'
    assert quote_identifier('od"d') == '"od""d"'
    # `$` matches immediately before a trailing newline, so the anchored
    # `^...$` here judged this bare and emitted it unquoted -- the one answer
    # `quote_ident` never gives. Unlike its sibling validators this pattern
    # decides *quoting* rather than acceptance, so being lax emitted broken
    # SQL rather than merely admitting a value nothing could use.
    assert quote_identifier("state\n") == '"state\n"'


def test_a_single_element_in_is_refused_because_postgres_rewrites_it() -> None:
    with pytest.raises(DeclarationError, match="two or more values"):
        one_of("state", ["only"])


def test_one_of_needs_a_list_not_a_bare_string() -> None:
    with pytest.raises(DeclarationError, match="takes a list"):
        one_of("state", "ready")


def test_all_of_does_not_nest() -> None:
    with pytest.raises(DeclarationError, match="does not nest"):
        all_of(all_of(eq("state", "a"), eq("tries", 1)), eq("archived", True))


def test_all_of_needs_two_predicates() -> None:
    with pytest.raises(DeclarationError, match="two or more"):
        all_of(eq("state", "a"))


def test_where_takes_a_predicate_not_a_string() -> None:
    with pytest.raises(DeclarationError, match="takes a predicate"):
        index("id", where="state = 'ready'")


def test_an_unknown_column_is_refused() -> None:
    with pytest.raises(DeclarationError, match="unknown column"):
        render_predicate(eq("nope", "x"), COLUMNS, "Sample")


def test_a_type_whose_normal_form_is_unpredictable_is_refused_by_name() -> None:
    with pytest.raises(DeclarationError, match="timestamptz"):
        render_predicate(eq("seen_at", "2026-01-01"), COLUMNS, "Sample")


def test_a_mistyped_literal_is_refused() -> None:
    with pytest.raises(DeclarationError, match="needs a str"):
        render_predicate(eq("state", 3), COLUMNS, "Sample")
    with pytest.raises(DeclarationError, match="needs an int"):
        render_predicate(eq("tries", "3"), COLUMNS, "Sample")
    with pytest.raises(DeclarationError, match="needs True or False"):
        render_predicate(eq("archived", 1), COLUMNS, "Sample")


def test_a_bool_is_not_accepted_as_an_integer() -> None:
    with pytest.raises(DeclarationError, match="needs an int"):
        render_predicate(eq("tries", True), COLUMNS, "Sample")


def test_one_of_is_text_only_because_array_casts_depend_on_column_width() -> None:
    with pytest.raises(DeclarationError, match="text columns only"):
        render_predicate(one_of("tries", [1, 2]), COLUMNS, "Sample")


def test_a_bad_predicate_fails_while_the_registry_compiles() -> None:
    class Broken(Model, table="broken", schema="decl"):
        id: Mapped[int] = column(Int64, primary_key=True)
        state: Mapped[str] = column(Text)
        _bad = index("id", where=eq("state", 7))

    with pytest.raises(DeclarationError, match="needs a str"):
        Registry(_Database(), [Broken], validate_schema="off")


def test_the_predicate_reaches_the_spec_as_rendered_sql() -> None:
    class Indexed(Model, table="indexed", schema="decl"):
        id: Mapped[int] = column(Int64, primary_key=True)
        state: Mapped[str] = column(Text)
        _claim = index("id", where=eq("state", "ready"))

    registry = Registry(_Database(), [Indexed], validate_schema="off")
    (declared,) = registry.spec_for(Indexed).table_indexes
    assert declared.where_sql == "(state = 'ready'::text)"


def test_a_changed_predicate_moves_the_model_fingerprint() -> None:

    def build(value: str) -> bytes:
        namespace: dict = {}
        exec(  # noqa: S102 - two models differing only in their predicate
            "from wreath.orm import Mapped, Model, column\n"
            "from wreath.orm.table import index, eq\n"
            "from wreath.orm.types import Int64, Text\n"
            "class M(Model, table='fp', schema='decl'):\n"
            "    id: Mapped[int] = column(Int64, primary_key=True)\n"
            "    state: Mapped[str] = column(Text)\n"
            f"    _i = index('id', where=eq('state', {value!r}))\n",
            namespace,
        )
        model = namespace["M"]
        return Registry(_Database(), [model], validate_schema="off").spec_for(model).fingerprint

    assert build("ready") != build("done")
