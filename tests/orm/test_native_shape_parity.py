from __future__ import annotations

from typing import Any

import pytest

from wreath._native import _core
from wreath.orm import and_, not_, or_
from wreath.orm.compiler import _collect_binds_native, _collect_binds_walk, _shape_of_walk

from .conftest import Membership, Post, User

native_only = pytest.mark.skipif(
    _core is None or not hasattr(_core, "orm_shape"),
    reason="native orm_shape not built",
)


def _queries() -> list[Any]:
    return [
        User.select(),
        User.select(User.id),
        User.select(User.id, User.email, User.name),
        User.select().where(User.id == 5),
        User.select().where(User.email == "a@b.c"),
        User.select().where(and_(User.id == 5, User.email == "a", User.name == "b")),
        User.select().where(or_(User.id == 1, User.id == 2)),
        User.select().where(not_(User.id == 9)),
        User.select().where(User.id.in_([1, 2, 3, 4, 5])),
        User.select().where(User.id.in_([9])),
        User.select().where(and_(User.id > 3, or_(User.email == "x", User.name == "y"))),
        User.select().where(User.id == 5).order_by(User.created_at),
        User.select().where(User.id == 5).order_by(User.id.desc(), User.email),
        User.select().limit(10),
        User.select().offset(5),
        User.select().limit(10).offset(20),
        User.select().for_update(),
        User.select().where(User.email == "x").order_by(User.id).limit(3).offset(1),
        Post.select().where(Post.author_id == 7),
        Post.select().where(Post.title.in_(["a", "b"])).order_by(Post.id.desc()),
        Membership.select().where(and_(Membership.org_id == 1, Membership.user_id == 2)),
        Membership.select().where(Membership.role == "admin").for_update(),
    ]


@native_only
@pytest.mark.parametrize("index", range(len(_queries())))
def test_native_key_matches_pure(registry: Any, index: int) -> None:
    query = _queries()[index]
    assert _core.orm_shape(registry, query) == _shape_of_walk(registry, query)


@native_only
@pytest.mark.parametrize("index", range(len(_queries())))
def test_native_binds_match_pure(registry: Any, index: int) -> None:
    # The native traversal collects the same value nodes, so encoded binds and
    # oids are byte-identical to the walked shape in the same placeholder order.
    query = _queries()[index]
    assert _collect_binds_native(query) == _collect_binds_walk(query)


@native_only
def test_native_keys_are_distinct_across_shapes(registry: Any) -> None:
    # Different queries must not collide (the Python reference guarantees this;
    # the native builder must preserve it).
    keys = [_core.orm_shape(registry, query) for query in _queries()]
    # in_([]) vs in_([1..]) differ by count; all keys unique except intentional.
    assert len(set(keys)) == len(keys)


def test_facade_selects_native_when_available(registry: Any) -> None:
    from wreath.orm import compiler

    if _core is None or not hasattr(_core, "orm_shape"):
        assert compiler.shape_of is compiler._shape_of_walk
        return

    native: list[int] = []
    pure: list[int] = []
    original_native, original_pure = compiler._shape_of_native, compiler._shape_of_walk
    compiler._shape_of_native = lambda *a: (native.append(1), original_native(*a))[1]
    compiler._shape_of_walk = lambda *a: (pure.append(1), original_pure(*a))[1]
    try:
        compiler.shape_of(registry, User.select().where(User.id == 5))
    finally:
        compiler._shape_of_native, compiler._shape_of_walk = original_native, original_pure
    assert native == [1], "an ordinary query must be keyed by the native builder"
    assert pure == [], "and must not fall back to the walked builder"


@native_only
def test_the_native_builder_keys_a_subquery_identically_to_pure(registry: Any) -> None:
    query = Post.select(Post.id).where(
        Post.author_id.in_(User.select(User.id).where(User.email == "a@b.c"))
    )
    assert _core.orm_shape(registry, query) == _shape_of_walk(registry, query)


@native_only
@pytest.mark.parametrize(
    ("label", "build_other"),
    [
        (
            "a different predicate column",
            lambda: User.select(User.id).where(User.name == "a@b.c"),
        ),
        (
            "a different projected column",
            lambda: User.select(User.email).where(User.email == "a@b.c"),
        ),
    ],
)
def test_two_subqueries_of_different_shape_do_not_share_a_key(
    registry: Any, label: str, build_other: Any
) -> None:
    base = Post.select(Post.id).where(
        Post.author_id.in_(User.select(User.id).where(User.email == "a@b.c"))
    )
    variant = Post.select(Post.id).where(Post.author_id.in_(build_other()))
    assert _core.orm_shape(registry, base) != _core.orm_shape(registry, variant), label
    assert _core.orm_shape(registry, variant) == _shape_of_walk(registry, variant)


@native_only
def test_two_subqueries_differing_only_in_bound_values_share_a_key(registry: Any) -> None:
    one = Post.select(Post.id).where(
        Post.author_id.in_(User.select(User.id).where(User.email == "a@b.c"))
    )
    two = Post.select(Post.id).where(
        Post.author_id.in_(User.select(User.id).where(User.email == "someone@else"))
    )
    assert _core.orm_shape(registry, one) == _core.orm_shape(registry, two)


@native_only
def test_a_subquery_no_longer_falls_back_to_pure(registry: Any) -> None:
    from wreath.orm import compiler

    query = Post.select(Post.id).where(
        Post.author_id.in_(User.select(User.id).where(User.email == "a@b.c"))
    )
    native: list[int] = []
    pure: list[int] = []
    original_native, original_pure = compiler._shape_of_native, compiler._shape_of_walk
    compiler._shape_of_native = lambda *a: (native.append(1), original_native(*a))[1]
    compiler._shape_of_walk = lambda *a: (pure.append(1), original_pure(*a))[1]
    try:
        compiler.shape_of(registry, query)
    finally:
        compiler._shape_of_native, compiler._shape_of_walk = original_native, original_pure
    assert native == [1], "the subquery must be keyed by the native builder"
    assert pure == [], "and must not fall back -- that fallback was the cost"
