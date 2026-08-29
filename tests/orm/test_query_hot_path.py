from __future__ import annotations

import dataclasses

import pytest

from wreath.orm.compiler import CompiledQuery, compile_select, shape_of
from wreath.orm.query import Select
from wreath.orm.registry import Registry

from .conftest import FakeDatabase, Membership, Post, User


@pytest.fixture
def registry() -> Registry:
    return Registry(FakeDatabase(), [User, Post, Membership], validate_schema="off")


def test_the_empty_projection_shortcut_builds_what_the_general_path_builds() -> None:
    shortcut = Select.build(User, ())
    general = Select(User, (), (), (), (), None, None, False)
    for field in Select.__slots__:
        assert getattr(shortcut, field) == getattr(general, field), field


def test_selecting_every_column_and_naming_them_are_different_shapes(
    registry: Registry,
) -> None:
    every = shape_of(registry, User.select())
    named = shape_of(registry, User.select(User.id, User.email, User.name, User.created_at))
    assert every != named


def test_a_projection_is_still_checked_when_one_is_given(registry: Registry) -> None:
    with pytest.raises(TypeError, match="takes model columns"):
        User.select("id")


def test_values_do_not_fragment_the_cache(registry: Registry) -> None:
    first = compile_select(registry, User.select().where(User.id == 1))
    second = compile_select(registry, User.select().where(User.id == 2))
    assert first.shape_key == second.shape_key
    assert registry.cached_plan_count == 1
    assert first.sql == second.sql
    assert second.bind_values == (2,)


def test_shapes_do_not_share_a_plan(registry: Registry) -> None:
    compile_select(registry, User.select().where(User.id == 1))
    compile_select(registry, User.select().where(User.name == "A"))
    compile_select(registry, User.select(User.id).where(User.id == 1))
    assert registry.cached_plan_count == 3


def test_the_shortcut_path_and_the_named_path_cache_separately(
    registry: Registry,
) -> None:
    compile_select(registry, User.select())
    compile_select(registry, User.select(User.id))
    assert registry.cached_plan_count == 2


def test_compiled_query_still_refuses_a_field_nobody_declared(
    registry: Registry,
) -> None:
    compiled = compile_select(registry, User.select().where(User.id == 1))
    with pytest.raises(AttributeError):
        compiled.not_a_field = 1  # type: ignore[attr-defined]


def test_compiled_query_carries_every_field_the_session_reads(
    registry: Registry,
) -> None:
    compiled = compile_select(registry, User.select().where(User.id == 1))
    names = {field.name for field in dataclasses.fields(CompiledQuery)}
    assert names == {
        "sql",
        "bind_values",
        "bind_oids",
        "result_model",
        "selected_columns",
        "load_plan",
        "shape_key",
        "projected_columns",
    }
    for name in names:
        getattr(compiled, name)
