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


def test_facade_selects_native_when_available() -> None:
    from wreath.orm import compiler

    if _core is not None and hasattr(_core, "orm_shape"):
        assert compiler.shape_of is _core.orm_shape
    else:
        assert compiler.shape_of is compiler._shape_of_pure
