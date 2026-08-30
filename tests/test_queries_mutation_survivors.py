from __future__ import annotations

from typing import Any, TypeVar

import pytest

from tests.orm.conftest import FakeDatabase, Membership, Post, User
from wreath.orm import DeclarationError
from wreath.orm.query import Select
from wreath.orm.registry import Registry
from wreath.queries import (
    BoundFusion,
    Fusion,
    Param,
    Queries,
    QueryDeclaration,
    _declared_model,
    fuse,
    query,
)


def test_param_accepts_a_valid_identifier_at_runtime() -> None:
    parameter = Param("runtime_name")

    assert parameter.name == "runtime_name"


@pytest.mark.parametrize("name", [7, None, "", "not-valid", "two words"])
def test_param_refuses_every_non_identifier_name(name: Any) -> None:
    with pytest.raises(DeclarationError, match="must be a Python identifier"):
        Param(name)


def test_one_without_a_limit_compiles_with_the_two_row_sentinel() -> None:
    class Dynamic(Queries[User]):
        selected = query(User.name == Param("name")).one()

    assert Dynamic.selected._execution_select.limit_ == 2


def test_one_reduces_a_loose_limit_to_the_two_row_sentinel() -> None:
    class Dynamic(Queries[User]):
        selected = query().limit(8).one()

    assert Dynamic.selected._execution_select.limit_ == 2


@pytest.mark.parametrize(("limit", "expected"), [(1, 1), (2, 2)])
def test_one_preserves_a_limit_at_or_below_the_sentinel(limit: int, expected: int) -> None:
    class Dynamic(Queries[User]):
        selected = query().limit(limit).one()

    assert Dynamic.selected._execution_select.limit_ == expected


def test_many_query_does_not_acquire_the_single_row_sentinel() -> None:
    class Dynamic(Queries[User]):
        selected = query()

    assert Dynamic.selected._execution_select.limit_ is None


def test_an_unclaimed_declaration_cannot_bind_even_without_parameters() -> None:
    with pytest.raises(DeclarationError, match="only usable as an attribute"):
        query().bind()


def test_an_unclaimed_declaration_cannot_use_the_validated_binding_path() -> None:
    with pytest.raises(DeclarationError, match="only usable as an attribute"):
        query()._bind_validated({})


def test_a_parameterless_bound_declaration_reuses_its_compiled_select() -> None:
    class Dynamic(Queries[User]):
        selected = query()

    assert Dynamic.selected.bind() is Dynamic.selected._select


def test_an_unclaimed_declaration_cannot_compile() -> None:
    declaration = query()

    with pytest.raises(DeclarationError, match="only usable as an attribute"):
        declaration._compile(SimpleRegistry(), {})


class SimpleRegistry:
    def cached_prepared_plan(self, _declaration: Any) -> None:
        return None


def test_compile_refuses_a_missing_execution_select_independently() -> None:
    class RegistryThatMustNotBeRead:
        def cached_prepared_plan(self, _declaration: Any) -> None:
            pytest.fail("an unresolved declaration consulted the registry")

    declaration = QueryDeclaration((), execution_select=None, value_program=lambda _values: ())

    with pytest.raises(DeclarationError, match="only usable as an attribute"):
        declaration._compile(RegistryThatMustNotBeRead(), {})


def test_compile_refuses_a_missing_value_program_independently() -> None:
    class Dynamic(Queries[User]):
        selected = query()

    declaration = Dynamic.selected
    declaration._value_program = None

    with pytest.raises(DeclarationError, match="only usable as an attribute"):
        declaration._compile(SimpleRegistry(), {})


def test_query_descriptor_returns_the_declaration_on_the_class() -> None:
    class Dynamic(Queries[User]):
        selected = query()

    assert isinstance(Dynamic.selected, QueryDeclaration)


def test_static_orderings_are_not_rebuilt_during_value_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dynamic(Queries[User]):
        selected = query(User.name == Param("name")).order_by(User.id)

    monkeypatch.setattr(Select, "rebound_orderings", pytest.fail)

    Dynamic.selected.bind(name="Ada")


def test_ordering_binding_preserves_items_without_a_binder() -> None:
    class Dynamic(Queries[User]):
        selected = query(User.name == Param("name")).order_by(User.id, User.name)

    declaration = Dynamic.selected
    declaration._ordering_binders = (
        None,
        lambda _values: declaration._select.orderings[1].expression,
    )

    bound = declaration.bind(name="Ada")

    assert bound.orderings[0] is declaration._select.orderings[0]
    assert bound.orderings[1].expression is declaration._select.orderings[1].expression


@pytest.mark.parametrize(("limit", "expected"), [(None, 2), (1, 1), (8, 2)])
def test_single_query_compilation_enforces_the_two_row_sentinel(
    limit: int | None, expected: int
) -> None:
    declaration = query().one() if limit is None else query().limit(limit).one()

    class Dynamic(Queries[User]):
        selected = declaration

    registry = Registry(FakeDatabase(), [User, Post, Membership], validate_schema="off")

    compiled, _execution_select = Dynamic.selected._compile(registry, {})

    assert compiled.sql.endswith(" LIMIT $1")
    assert compiled.bind_values == (expected,)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "2"])
def test_fusion_limit_refuses_non_positive_non_integer_and_boolean_values(value: Any) -> None:
    fusion = Fusion((query(), query()), k=60)

    with pytest.raises(ValueError, match="integer >= 1"):
        fusion.limit(value)


def test_fusion_limit_accepts_a_positive_integer_at_runtime() -> None:
    fusion = Fusion((query(), query()), k=60)

    assert fusion.limit(3).limit_ == 3


def test_fusion_descriptor_returns_the_fusion_on_the_class() -> None:
    class Dynamic(Queries[User]):
        first = query().order_by(User.id).limit(2)
        second = query().order_by(User.name).limit(2)
        merged = fuse(first, second)

    assert isinstance(Dynamic.merged, Fusion)


def test_fusion_descriptor_binds_on_an_instance() -> None:
    class Dynamic(Queries[User]):
        first = query().order_by(User.id).limit(2)
        second = query().order_by(User.name).limit(2)
        merged = fuse(first, second)

    assert isinstance(Dynamic(object()).merged, BoundFusion)


@pytest.mark.parametrize("k", [True, False, -1, 1.5, "1"])
def test_fuse_refuses_invalid_rank_constants(k: Any) -> None:
    with pytest.raises(ValueError, match="integer >= 0"):
        fuse(query(), query(), k=k)


def test_fuse_accepts_zero_and_positive_integer_rank_constants() -> None:
    assert fuse(query(), query(), k=0).k == 0
    assert fuse(query(), query(), k=7).k == 7


def test_declared_model_ignores_a_class_without_generic_bases() -> None:
    class Plain:
        pass

    assert _declared_model(Plain) is None


def test_declared_model_ignores_a_non_queries_generic_base() -> None:
    class Plain(list[int]):
        pass

    assert _declared_model(Plain) is None


def test_declared_model_checks_an_origin_is_a_type_before_subclassing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath.queries as module

    class Plain:
        __orig_bases__ = (object(),)

    monkeypatch.setattr(module, "get_origin", lambda _base: object())

    assert _declared_model(Plain) is None


def test_declared_model_checks_for_arguments_before_reading_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath.queries as module

    class Plain:
        __orig_bases__ = (object(),)
        model = "fallback"

    monkeypatch.setattr(module, "get_origin", lambda _base: Queries)
    monkeypatch.setattr(module, "get_args", lambda _base: ())

    assert _declared_model(Plain) == "fallback"


def test_declared_model_uses_a_concrete_queries_argument() -> None:
    class Dynamic(Queries[User]):
        pass

    assert _declared_model(Dynamic) is User


def test_declared_model_ignores_a_type_variable_argument() -> None:
    ModelType = TypeVar("ModelType")

    class GenericQueries(Queries[ModelType]):
        pass

    assert _declared_model(GenericQueries) is None
