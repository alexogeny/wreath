"""Pure/native parity for the ORM query cache key (`shape_of`).

The key is a dict key for the compiled-SQL cache, so the native builder must
produce bytes identical to the pure reference for every query shape -- a
mismatch would silently split the cache. `_shape_of_pure` stays the reference.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath._native import _core
from wreath.orm import and_, not_, or_
from wreath.orm.compiler import _collect_binds_native, _collect_binds_pure, _shape_of_pure

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
    assert _core.orm_shape(registry, query) == _shape_of_pure(registry, query)


@native_only
@pytest.mark.parametrize("index", range(len(_queries())))
def test_native_binds_match_pure(registry: Any, index: int) -> None:
    # The native traversal collects the same value nodes, so encoded binds and
    # oids are byte-identical to the pure walk in the same placeholder order.
    query = _queries()[index]
    assert _collect_binds_native(query) == _collect_binds_pure(query)


@native_only
def test_native_keys_are_distinct_across_shapes(registry: Any) -> None:
    # Different queries must not collide (the pure reference guarantees this;
    # the native builder must preserve it).
    keys = [_core.orm_shape(registry, query) for query in _queries()]
    # in_([]) vs in_([1..]) differ by count; all keys unique except intentional.
    assert len(set(keys)) == len(keys)


def test_facade_selects_native_when_available(registry: Any) -> None:
    """The native builder is what actually runs, not merely what is installed.

    This used to assert `shape_of is _core.orm_shape`. Identity stopped being
    available once `shape_of` gained a fallback for nodes the extension cannot
    key, but the property the test existed for -- that an ordinary query is not
    quietly keyed in Python -- is unchanged, so it is asserted by observing the
    call instead of by comparing objects.
    """
    from wreath.orm import compiler

    if _core is None or not hasattr(_core, "orm_shape"):
        assert compiler.shape_of is compiler._shape_of_pure
        return

    native: list[int] = []
    pure: list[int] = []
    original_native, original_pure = compiler._shape_of_native, compiler._shape_of_pure
    compiler._shape_of_native = lambda *a: (native.append(1), original_native(*a))[1]
    compiler._shape_of_pure = lambda *a: (pure.append(1), original_pure(*a))[1]
    try:
        compiler.shape_of(registry, User.select().where(User.id == 5))
    finally:
        compiler._shape_of_native, compiler._shape_of_pure = original_native, original_pure
    assert native == [1], "an ordinary query must be keyed by the native builder"
    assert pure == [], "and must not fall back to the pure builder"


@native_only
def test_the_pure_fallback_is_reached_only_for_a_node_c_cannot_key(registry: Any) -> None:
    """`InSubqueryExpr` postdates the extension, so C refuses and Python answers.

    Both halves matter. The refusal is what makes the fallback safe -- a builder
    that dispatched by a *base* class would key a subquery predicate as though
    the subquery were not there, and two different subqueries would collide in
    the plan cache. This pins the refusal, so a future C change that starts
    silently accepting the node fails here rather than in production.
    """
    from wreath.orm import compiler
    from wreath.orm.errors import ORMError

    query = Post.select(Post.id).where(
        Post.author_id.in_(User.select(User.id).where(User.email == "a@b.c"))
    )
    with pytest.raises(ORMError, match="cannot key"):
        _core.orm_shape(registry, query)
    assert compiler.shape_of(registry, query) == _shape_of_pure(registry, query)
