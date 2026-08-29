from __future__ import annotations

import pytest

from wreath.orm.compiler import compile_select
from wreath.orm.errors import DeclarationError, ORMError, UnloadedRelationshipError
from wreath.orm.expressions import RelatedColumnExpr
from wreath.orm.registry import Registry

from .conftest import FakeDatabase, Post, User, post_row

pytestmark = pytest.mark.asyncio


async def test_traversing_a_relationship_yields_a_related_column() -> None:
    expression = Post.author.email
    assert isinstance(expression, RelatedColumnExpr)
    assert expression.column.python_name == "email"
    assert [item.python_name for item in expression.path] == ["author"]


async def test_a_related_predicate_emits_an_inner_join(registry: Registry) -> None:
    compiled = compile_select(registry, Post.select().where(Post.author.email == "a@b.c"))
    assert 'INNER JOIN "public"."users"' in compiled.sql
    assert '"w1"."email" = $1' in compiled.sql
    assert compiled.bind_values == ("a@b.c",)


async def test_the_join_is_inner_not_left(registry: Registry) -> None:
    # A post whose author does not match cannot satisfy the predicate, so LEFT
    # would only cost the planner a reordering opportunity.
    compiled = compile_select(registry, Post.select().where(Post.author.name == "A"))
    assert "LEFT JOIN" not in compiled.sql


async def test_filtering_does_not_select_the_related_model(registry: Registry) -> None:
    compiled = compile_select(registry, Post.select().where(Post.author.name == "A"))
    assert [item.database_name for item in compiled.selected_columns] == [
        "id",
        "author_id",
        "title",
    ]
    assert compiled.load_plan.joined == ()


async def test_filtering_leaves_the_relation_unloaded(
    registry: Registry, database: FakeDatabase
) -> None:
    # The rule that attribute access never performs I/O has to survive this.
    from wreath.orm.session import Session

    database.connection.script("posts", [post_row(1, 7)])
    session = Session(registry, "read")
    posts = await session.fetch(Post.select().where(Post.author.name == "A"))
    assert len(posts) == 1
    with pytest.raises(UnloadedRelationshipError):
        _ = posts[0].author


async def test_filtering_and_including_the_same_relation(
    registry: Registry, database: FakeDatabase
) -> None:
    compiled = compile_select(
        registry,
        Post.select().where(Post.author.name == "A").include(Post.author.joined()),
    )
    # One join to load through, one to filter through. They are separate: the
    # load must stay LEFT, and the filter must stay INNER.
    assert "LEFT JOIN" in compiled.sql
    assert "INNER JOIN" in compiled.sql


async def test_two_predicates_on_one_relation_join_once(registry: Registry) -> None:
    compiled = compile_select(
        registry,
        Post.select().where(Post.author.name == "A", Post.author.email == "a@b.c"),
    )
    assert compiled.sql.count("INNER JOIN") == 1


async def test_related_and_local_columns_do_not_share_a_cache_key(
    registry: Registry,
) -> None:
    # Both are "a text column called title"; only the path distinguishes them.
    local = compile_select(registry, Post.select().where(Post.title == "A"))
    related = compile_select(registry, Post.select().where(Post.author.name == "A"))
    assert local.shape_key != related.shape_key


async def test_a_value_still_never_reaches_the_key(registry: Registry) -> None:
    first = compile_select(registry, Post.select().where(Post.author.name == "A"))
    second = compile_select(registry, Post.select().where(Post.author.name == "B"))
    assert first.shape_key == second.shape_key
    assert first.sql == second.sql


async def test_a_to_many_predicate_is_refused(registry: Registry) -> None:
    # Joining a collection would duplicate parents; that needs EXISTS.
    with pytest.raises(ORMError, match="to-many"):
        compile_select(registry, User.select().where(User.posts.title == "x"))


async def test_an_unknown_attribute_on_the_target_raises() -> None:
    with pytest.raises(AttributeError, match="no column or relationship"):
        _ = Post.author.nonexistent


async def test_comparison_operators_survive_the_traversal(registry: Registry) -> None:
    compiled = compile_select(registry, Post.select().where(Post.author.id > 5))
    assert '"w1"."id" > $1' in compiled.sql
    assert compiled.bind_values == (5,)


async def test_in_survives_the_traversal(registry: Registry) -> None:
    compiled = compile_select(registry, Post.select().where(Post.author.name.in_(["A", "B"])))
    assert '"w1"."name" IN (' in compiled.sql
    assert compiled.bind_values == ("A", "B")


async def test_a_related_predicate_combines_with_a_local_one(
    registry: Registry, database: FakeDatabase
) -> None:
    compiled = compile_select(
        registry, Post.select().where(Post.title == "t", Post.author.name == "A")
    )
    assert '"t0"."title" = $1' in compiled.sql
    assert '"w1"."name" = $2' in compiled.sql
    assert compiled.bind_values == ("t", "A")


async def test_a_related_column_cannot_be_projected() -> None:
    with pytest.raises(DeclarationError):
        Post.select(Post.author.name)


async def test_the_join_survives_a_cache_hit(registry: Registry) -> None:
    first = compile_select(registry, Post.select().where(Post.author.name == "A"))
    second = compile_select(registry, Post.select().where(Post.author.name == "B"))
    # The second call takes the cached plan; its SQL must still carry the join.
    assert "INNER JOIN" in second.sql
    assert first.sql == second.sql


async def test_ordering_still_targets_the_base_table(registry: Registry) -> None:
    compiled = compile_select(
        registry, Post.select().where(Post.author.name == "A").order_by(Post.id)
    )
    assert 'ORDER BY "t0"."id"' in compiled.sql


async def test_fetching_through_a_join_returns_models(
    registry: Registry, database: FakeDatabase
) -> None:
    from wreath.orm.session import Session

    database.connection.script("posts", [post_row(1, 7, "t")])
    session = Session(registry, "read")
    posts = await session.fetch(Post.select().where(Post.author.email == "a@b.c").order_by(Post.id))
    assert [item.id for item in posts] == [1]
    assert database.connection.calls[0][1] == ("a@b.c",)
