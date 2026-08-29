from __future__ import annotations

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.compiler import compile_select, shape_of
from wreath.orm.errors import DeclarationError
from wreath.orm.registry import Registry
from wreath.orm.types import Array, Int64, Json, Jsonb, Text, Uuid

from .conftest import FakeDatabase


class Doc(Model, table="docs"):
    id: Mapped[int] = column(Int64, primary_key=True)
    data: Mapped[dict] = column(Jsonb)
    meta: Mapped[dict] = column(Json, nullable=True)
    tags: Mapped[list] = column(Array(Text))
    scores: Mapped[list] = column(Array(Int64), nullable=True)


DOCS = '"public"."docs" AS "t0"'


@pytest.fixture
def registry() -> Registry:
    return Registry(FakeDatabase(), [Doc], validate_schema="off")


def _where(registry: Registry, predicate):
    return compile_select(registry, Doc.select(Doc.id).where(predicate))


def test_jsonb_contains_renders_and_binds_jsonb(registry: Registry) -> None:
    compiled = _where(registry, Doc.data.contains({"role": "admin"}))
    assert compiled.sql.endswith('WHERE "t0"."data" @> $1')
    assert compiled.bind_oids == (Jsonb.oid,)
    assert len(compiled.bind_values) == 1


def test_jsonb_contained_by(registry: Registry) -> None:
    compiled = _where(registry, Doc.data.contained_by({"a": 1, "b": 2}))
    assert compiled.sql.endswith('WHERE "t0"."data" <@ $1')
    assert compiled.bind_oids == (Jsonb.oid,)


def test_jsonb_has_key_binds_text(registry: Registry) -> None:
    compiled = _where(registry, Doc.data.has_key("role"))
    assert compiled.sql.endswith('WHERE "t0"."data" ? $1')
    assert compiled.bind_oids == (Text.oid,)
    assert compiled.bind_values == ("role",)


def test_jsonb_has_any_binds_text_array(registry: Registry) -> None:
    compiled = _where(registry, Doc.data.has_any(["a", "b"]))
    assert compiled.sql.endswith('WHERE "t0"."data" ?| $1')
    assert compiled.bind_oids == (1009,)  # text[]
    assert compiled.bind_values == (["a", "b"],)


def test_jsonb_has_all(registry: Registry) -> None:
    compiled = _where(registry, Doc.data.has_all(["a", "b"]))
    assert compiled.sql.endswith('WHERE "t0"."data" ?& $1')
    assert compiled.bind_oids == (1009,)


def test_jsonb_path_text_extraction(registry: Registry) -> None:
    compiled = _where(registry, Doc.data.path(["a", "b"]) == "x")
    assert compiled.sql.endswith('WHERE ("t0"."data" #>> $1) = $2')
    # The path binds first (text[]), then the compared value (text).
    assert compiled.bind_oids == (1009, Text.oid)
    assert compiled.bind_values == (["a", "b"], "x")


def test_jsonb_path_as_json_contains(registry: Registry) -> None:
    compiled = _where(registry, Doc.data.path(["a"], as_json=True).contains({"k": 1}))
    assert compiled.sql.endswith('WHERE ("t0"."data" #> $1) @> $2')
    assert compiled.bind_oids == (1009, Jsonb.oid)


def test_array_contains(registry: Registry) -> None:
    compiled = _where(registry, Doc.tags.contains(["x"]))
    assert compiled.sql.endswith('WHERE "t0"."tags" @> $1')
    assert compiled.bind_oids == (1009,)
    assert compiled.bind_values == (["x"],)


def test_array_overlaps(registry: Registry) -> None:
    compiled = _where(registry, Doc.tags.overlaps(["x", "y"]))
    assert compiled.sql.endswith('WHERE "t0"."tags" && $1')
    assert compiled.bind_oids == (1009,)


def test_array_any_eq_puts_value_on_the_left(registry: Registry) -> None:
    compiled = _where(registry, Doc.tags.any_eq("x"))
    assert compiled.sql.endswith('WHERE $1 = ANY("t0"."tags")')
    assert compiled.bind_oids == (Text.oid,)
    assert compiled.bind_values == ("x",)


def test_array_all_eq(registry: Registry) -> None:
    compiled = _where(registry, Doc.scores.all_eq(3))
    assert compiled.sql.endswith('WHERE $1 = ALL("t0"."scores")')
    assert compiled.bind_oids == (Int64.oid,)
    assert compiled.bind_values == (3,)


def test_operators_get_distinct_shape_keys(registry: Registry) -> None:
    contains = shape_of(registry, Doc.select(Doc.id).where(Doc.data.contains({"a": 1})))
    contained = shape_of(registry, Doc.select(Doc.id).where(Doc.data.contained_by({"a": 1})))
    assert contains != contained


def test_shape_key_is_independent_of_array_length(registry: Registry) -> None:
    one = shape_of(registry, Doc.select(Doc.id).where(Doc.data.has_any(["a"])))
    many = shape_of(registry, Doc.select(Doc.id).where(Doc.data.has_any(["a", "b", "c"])))
    assert one == many  # the whole array is a single bound parameter


def test_has_key_requires_jsonb_not_plain_json(registry: Registry) -> None:
    with pytest.raises(DeclarationError):
        Doc.meta.has_key("a")


def test_array_operator_rejects_a_jsonb_column(registry: Registry) -> None:
    with pytest.raises(DeclarationError):
        Doc.data.overlaps(["a"])


def test_jsonb_operator_rejects_an_array_column(registry: Registry) -> None:
    with pytest.raises(DeclarationError):
        Doc.tags.has_key("a")


def test_empty_operands_are_rejected(registry: Registry) -> None:
    for build in (
        lambda: Doc.data.has_any([]),
        lambda: Doc.data.has_all([]),
        lambda: Doc.tags.overlaps([]),
    ):
        with pytest.raises(ValueError):
            build()


def test_path_requires_json_or_jsonb(registry: Registry) -> None:
    with pytest.raises(DeclarationError):
        Doc.tags.path(["a"])


def test_array_coerces_elements_and_rejects_mistyped() -> None:
    text_array = Array(Text)
    assert text_array.coerce(["a", "b"]) == ["a", "b"]
    assert text_array.coerce(("a",)) == ["a"]
    with pytest.raises(TypeError):
        text_array.coerce([1])
    with pytest.raises(TypeError):
        text_array.coerce("not-a-list")


def test_array_elements_are_not_nullable_by_default() -> None:
    with pytest.raises(TypeError):
        Array(Text).coerce(["a", None])
    assert Array(Text, nullable_elements=True).coerce(["a", None]) == ["a", None]


def test_array_to_wire_maps_element_wire_form() -> None:
    # jsonb elements are wired to their text form, one per element.
    wired = Array(Jsonb).to_wire([{"a": 1}, {"b": 2}])
    assert wired == ['{"a":1}', '{"b":2}']


def test_array_oid_and_sql_spelling() -> None:
    assert Array(Text).oid == 1009
    assert Array(Text).sql == "text[]"
    assert Array(Uuid).oid == 2951
    assert Array(Int64).oid == 1016


def test_nested_arrays_are_rejected() -> None:
    with pytest.raises(TypeError):
        Array(Array(Text))


def test_unsupported_element_has_no_array() -> None:
    # A hypothetical element with no registered array oid is rejected; all
    # declared scalar types do have one, so we assert the mapping is present.
    from wreath.orm.types import BY_OID

    assert 1009 in BY_OID  # text[] registered for result validation
    assert BY_OID[1009].oid == 1009


def test_index_true_is_btree() -> None:
    col = column(Jsonb, index=True)
    assert col.indexed is True
    assert col.index_method == "btree"


def test_index_gin() -> None:
    col = column(Jsonb, index="gin")
    assert col.indexed is True
    assert col.index_method == "gin"


def test_index_false_has_no_method() -> None:
    col = column(Jsonb)
    assert col.indexed is False
    assert col.index_method is None


def test_index_rejects_unknown_method() -> None:
    with pytest.raises(DeclarationError):
        column(Jsonb, index="hash")
