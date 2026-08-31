from __future__ import annotations

import pytest

from wreath.orm import and_, not_, or_, where_fields
from wreath.orm import compiler as compiler_module
from wreath.orm.compiler import (
    _collect_binds,
    compile_count,
    compile_delete_many,
    compile_insert_many,
    compile_rebind,
    compile_select,
    compile_update_many,
    shape_of,
)
from wreath.orm.errors import DeclarationError, ORMError
from wreath.orm.expressions import InExpr, ValueExpr
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text
from wreath.queries import Placeholder

from .conftest import FakeDatabase, Membership, Post, User

USERS = '"public"."users" AS "t0"'


def test_returning_multi_insert_compiles_every_row_and_unloaded_column(
    registry: Registry,
) -> None:
    spec = registry.spec_for(User)
    mask = (1 << 1) | (1 << 2)

    plan = compile_insert_many(registry, spec, mask, 2)

    assert plan.sql == (
        'INSERT INTO "public"."users" ("email", "name") '
        'VALUES ($1, $2), ($3, $4) RETURNING "id", "created_at"'
    )
    assert [column.python_name for column in plan.columns] == ["email", "name"]
    assert [column.python_name for column in plan.returning] == ["id", "created_at"]


def test_update_many_unnests_fixed_shape_arrays_and_returns_the_complete_key(
    registry: Registry,
) -> None:
    spec = registry.spec_for(Membership)
    mask = 1 << 2

    plan = compile_update_many(registry, spec, mask, 2)

    assert plan.sql == (
        'UPDATE "public"."memberships" AS "wreath_target" '
        'SET "role" = "wreath_batch"."role" '
        "FROM UNNEST($1::text[], $2::bigint[], $3::bigint[]) "
        'AS "wreath_batch" ("role", "org_id", "user_id") '
        'WHERE "wreath_target"."org_id" = "wreath_batch"."org_id" AND '
        '"wreath_target"."user_id" = "wreath_batch"."user_id" '
        'RETURNING "wreath_target"."org_id", "wreath_target"."user_id"'
    )
    assert [column.python_name for column in plan.key_columns] == ["org_id", "user_id"]
    assert plan.returning == plan.key_columns


def test_delete_many_unnests_fixed_shape_arrays_and_returns_the_complete_key(
    registry: Registry,
) -> None:
    spec = registry.spec_for(Membership)

    plan = compile_delete_many(registry, spec, 2)

    assert plan.sql == (
        'DELETE FROM "public"."memberships" AS "wreath_target" '
        "USING UNNEST($1::bigint[], $2::bigint[]) "
        'AS "wreath_batch" ("org_id", "user_id") '
        'WHERE "wreath_target"."org_id" = "wreath_batch"."org_id" AND '
        '"wreath_target"."user_id" = "wreath_batch"."user_id" '
        'RETURNING "wreath_target"."org_id", "wreath_target"."user_id"'
    )
    assert plan.returning == plan.key_columns


@pytest.mark.parametrize(
    "compile_plan,args",
    [
        (compile_insert_many, (0, 0)),
        (compile_update_many, (1 << 2, 0)),
        (compile_delete_many, (0,)),
    ],
)
def test_many_write_compilers_refuse_a_non_positive_row_count(
    registry: Registry, compile_plan, args: tuple[int, ...]
) -> None:
    with pytest.raises(ValueError, match="row count must be at least 1"):
        compile_plan(registry, registry.spec_for(Membership), *args)


def test_many_write_compilers_refuse_an_unchunked_parameter_overflow(
    registry: Registry,
) -> None:
    with pytest.raises(ValueError, match=r"row count must be at most 65535"):
        compile_delete_many(registry, registry.spec_for(Membership), 65536)


def test_unnest_write_plan_is_independent_of_row_count(registry: Registry) -> None:
    spec = registry.spec_for(Membership)

    assert compile_update_many(registry, spec, 1 << 2, 2) is compile_update_many(
        registry, spec, 1 << 2, 200
    )
    assert compile_delete_many(registry, spec, 2) is compile_delete_many(registry, spec, 200)


def test_an_in_expression_rebinds_a_placeholder_among_fixed_values() -> None:
    marker = Placeholder("user_id", Int64)
    fixed = ValueExpr(9, Int64)
    expression = InExpr("IN", User.id, (marker, fixed))
    found: list[Placeholder] = []

    rebind = compile_rebind(expression, Placeholder, found)

    assert rebind is not None
    rebound = rebind({"user_id": 7})
    assert [value.value for value in rebound.values] == [7, 9]
    assert found == [marker]


def test_where_fields_builds_plain_runtime_equalities(registry: Registry) -> None:
    query = User.select().where(*where_fields(User, {"email": "a@b.c", "name": "Ada"}))
    compiled = compile_select(registry, query)
    assert '"t0"."email" = $1 AND "t0"."name" = $2' in compiled.sql
    assert compiled.bind_values == ("a@b.c", "Ada")


def test_where_fields_refuses_a_legacy_lookup_language() -> None:
    with pytest.raises(ValueError, match="does not interpret lookup path"):
        where_fields(User, {"email__icontains": "ada"})


def test_where_fields_refuses_an_unknown_column() -> None:
    with pytest.raises(ValueError, match="has no column"):
        where_fields(User, {"nickname": "Ada"})


def test_select_all_columns_is_explicit_never_star(registry: Registry) -> None:
    compiled = compile_select(registry, User.select())
    assert compiled.sql == (
        f'SELECT "t0"."id", "t0"."email", "t0"."name", "t0"."created_at" FROM {USERS}'
    )
    assert "*" not in compiled.sql


def test_projection_keeps_declaration_order_and_binds_no_values(registry: Registry) -> None:
    compiled = compile_select(
        registry, User.select(User.id, User.email).where(User.email == "a@b.c")
    )
    assert compiled.sql == (f'SELECT "t0"."id", "t0"."email" FROM {USERS} WHERE "t0"."email" = $1')
    assert compiled.bind_values == ("a@b.c",)
    assert compiled.bind_oids == (Text.oid,)


def test_a_value_never_appears_in_sql_or_the_shape_key(registry: Registry) -> None:
    first = compile_select(registry, User.select(User.id).where(User.id == 42))
    second = compile_select(registry, User.select(User.id).where(User.id == 99))
    assert "42" not in first.sql
    assert first.sql == second.sql
    assert first.shape_key == second.shape_key
    assert first.bind_values == (42,)
    assert second.bind_values == (99,)


def test_duplicate_projected_columns_collapse_to_the_first(registry: Registry) -> None:
    compiled = compile_select(registry, User.select(User.email, User.id, User.email))
    assert compiled.sql.startswith('SELECT "t0"."email", "t0"."id" FROM')


def test_the_primary_key_is_selected_even_when_omitted(registry: Registry) -> None:
    # Identity cannot be built without it, so it is added and marked internal.
    compiled = compile_select(registry, User.select(User.email))
    assert compiled.sql.startswith('SELECT "t0"."email", "t0"."id" FROM')
    assert [item.python_name for item in compiled.projected_columns] == ["email"]
    assert [item.python_name for item in compiled.selected_columns] == ["email", "id"]


def test_composite_primary_keys_are_added_in_declaration_order(registry: Registry) -> None:
    compiled = compile_select(registry, Membership.select(Membership.role))
    assert compiled.sql.startswith('SELECT "t0"."role", "t0"."org_id", "t0"."user_id" FROM')


def test_predicates_and_ordering_and_bounds(registry: Registry) -> None:
    compiled = compile_select(
        registry,
        User.select(User.id)
        .where(User.name == "A")
        .order_by(User.id.desc(), User.email)
        .limit(10)
        .offset(20),
    )
    assert compiled.sql == (
        f'SELECT "t0"."id" FROM {USERS} WHERE "t0"."name" = $1 '
        'ORDER BY "t0"."id" DESC, "t0"."email" ASC LIMIT $2 OFFSET $3'
    )
    assert compiled.bind_values == ("A", 10, 20)


def test_limits_are_bound_so_the_shape_survives_a_changing_page_size(
    registry: Registry,
) -> None:
    first = compile_select(registry, User.select(User.id).limit(10))
    second = compile_select(registry, User.select(User.id).limit(500))
    assert first.shape_key == second.shape_key
    assert first.bind_values == (10,) and second.bind_values == (500,)


def test_boolean_combinators(registry: Registry) -> None:
    compiled = compile_select(
        registry,
        User.select(User.id).where((User.name == "A") & (User.email == "b@c.d")),
    )
    assert compiled.sql.endswith('WHERE ("t0"."name" = $1 AND "t0"."email" = $2)')

    compiled = compile_select(
        registry, User.select(User.id).where(or_(User.name == "A", User.name == "B"))
    )
    assert compiled.sql.endswith('WHERE ("t0"."name" = $1 OR "t0"."name" = $2)')

    compiled = compile_select(registry, User.select(User.id).where(not_(User.name == "A")))
    assert compiled.sql.endswith('WHERE NOT ("t0"."name" = $1)')


def test_repeated_where_calls_are_conjunctive(registry: Registry) -> None:
    compiled = compile_select(
        registry, User.select(User.id).where(User.name == "A").where(User.email == "b@c.d")
    )
    assert compiled.sql.endswith('WHERE ("t0"."name" = $1 AND "t0"."email" = $2)')


def test_null_checks_and_membership(registry: Registry) -> None:
    compiled = compile_select(registry, User.select(User.id).where(User.created_at.is_null()))
    assert compiled.sql.endswith('WHERE "t0"."created_at" IS NULL')
    assert compiled.bind_values == ()

    compiled = compile_select(registry, User.select(User.id).where(User.id.in_([1, 2, 3])))
    assert compiled.sql.endswith('WHERE "t0"."id" IN ($1, $2, $3)')
    assert compiled.bind_values == (1, 2, 3)


def test_membership_operand_count_is_part_of_the_shape(registry: Registry) -> None:
    two = compile_select(registry, User.select(User.id).where(User.id.in_([1, 2])))
    three = compile_select(registry, User.select(User.id).where(User.id.in_([1, 2, 3])))
    assert two.shape_key != three.shape_key
    assert two.sql != three.sql


def test_comparing_a_column_to_none_is_rejected() -> None:
    with pytest.raises(TypeError, match="is_null"):
        User.created_at == None  # noqa: E711, B015 - the comparison is the subject


def test_a_predicate_cannot_be_used_as_a_python_boolean() -> None:
    with pytest.raises(TypeError, match="and/or"):
        bool(User.id == 1)


def test_a_mistyped_comparison_is_rejected_at_the_call_site() -> None:
    with pytest.raises(TypeError, match="expected str"):
        User.email == 5  # noqa: B015 - building the node is what must fail


def test_an_out_of_range_comparison_is_rejected_at_the_call_site() -> None:
    with pytest.raises(OverflowError):
        User.id == 2**63  # noqa: B015 - building the node is what must fail


def test_for_update_renders_last(registry: Registry) -> None:
    compiled = compile_select(registry, User.select(User.id).limit(1).for_update())
    assert compiled.sql.endswith("LIMIT $1 FOR UPDATE")


def test_joined_to_one_loads_in_one_statement(registry: Registry) -> None:
    compiled = compile_select(registry, Post.select().include(Post.author.joined()))
    assert compiled.sql == (
        'SELECT "t0"."id", "t0"."author_id", "t0"."title", '
        '"j1"."id", "j1"."email", "j1"."name", "j1"."created_at" '
        'FROM "public"."posts" AS "t0" '
        'LEFT JOIN "public"."users" AS "j1" ON "j1"."id" = "t0"."author_id"'
    )
    assert len(compiled.load_plan.joined) == 1
    assert compiled.load_plan.joined[0].offset == 3


def test_a_collection_cannot_be_joined(registry: Registry) -> None:
    # A join would multiply parent rows; the request is rejected rather than
    # silently answered with a different shape.
    with pytest.raises(ORMError, match="multiply parent rows"):
        compile_select(registry, User.select().include(User.posts.joined()))


def test_selectin_is_planned_not_emitted_inline(registry: Registry) -> None:
    compiled = compile_select(registry, User.select().include(User.posts.selectin()))
    assert "posts" not in compiled.sql
    assert [step.relationship.name for step in compiled.load_plan.selectin] == ["posts"]


def test_a_declared_load_applies_without_an_explicit_include() -> None:
    from wreath.orm import Mapped, Model, column, relationship

    class Author(Model, table="authors"):
        id: Mapped[int] = column(Int64, primary_key=True)
        books = relationship("Book", foreign_key="author_id", load="selectin")

    class Book(Model, table="books"):
        id: Mapped[int] = column(Int64, primary_key=True)
        author_id: Mapped[int] = column(Int64, references=Author.id)

    registry = Registry(FakeDatabase(), [Author, Book], validate_schema="off")
    compiled = compile_select(registry, Author.select())
    assert [step.relationship.name for step in compiled.load_plan.selectin] == ["books"]


def test_an_explicit_include_overrides_a_declared_default() -> None:
    from wreath.orm import Mapped, Model, column, relationship

    class Owner(Model, table="owners"):
        id: Mapped[int] = column(Int64, primary_key=True)

    class Pet(Model, table="pets"):
        id: Mapped[int] = column(Int64, primary_key=True)
        owner_id: Mapped[int] = column(Int64, references=Owner.id)
        owner = relationship(Owner, foreign_key=owner_id, load="selectin")

    registry = Registry(FakeDatabase(), [Owner, Pet], validate_schema="off")
    assert compile_select(registry, Pet.select()).load_plan.selectin
    joined = compile_select(registry, Pet.select().include(Pet.owner.joined()))
    assert joined.load_plan.joined and not joined.load_plan.selectin


def test_duplicate_includes_deduplicate(registry: Registry) -> None:
    compiled = compile_select(
        registry, Post.select().include(Post.author.joined(), Post.author.joined())
    )
    assert compiled.sql.count("LEFT JOIN") == 1


def test_a_query_cannot_mix_models(registry: Registry) -> None:
    with pytest.raises(DeclarationError, match="not a column of"):
        User.select(Post.title)


def test_a_query_cannot_cross_registries() -> None:
    other = Registry(FakeDatabase("other"), [Membership], validate_schema="off")
    with pytest.raises(Exception, match="not registered"):
        compile_select(other, User.select())


def test_shape_keys_differ_across_registries() -> None:
    first = Registry(FakeDatabase("a"), [User, Post], validate_schema="off")
    second = Registry(FakeDatabase("b"), [User, Post, Membership], validate_schema="off")
    query = User.select(User.id)
    assert shape_of(first, query) != shape_of(second, query)


def test_negative_bounds_are_rejected_before_execution() -> None:
    with pytest.raises(ValueError, match="negative"):
        User.select().limit(-1)
    with pytest.raises(ValueError, match="negative"):
        User.select().offset(-5)


@pytest.mark.parametrize("value", [1.5, "10", True, None])
def test_non_integer_bounds_are_rejected(value: object) -> None:
    with pytest.raises(TypeError, match="must be an integer"):
        User.select().limit(value)  # type: ignore[arg-type]


def test_paginate_sets_both_bounds_without_losing_the_query_shape() -> None:
    base = User.select(User.id).where(User.email == "a@b.c").order_by(User.id)

    page = base.paginate(3, 20)

    assert page.limit_ == 20
    assert page.offset_ == 40
    assert page.projection is base.projection
    assert page.predicates is base.predicates
    assert page.orderings is base.orderings


def test_one_shape_compiles_once(registry: Registry) -> None:
    compile_select(registry, User.select(User.id).where(User.id == 1))
    assert registry.cached_plan_count == 1
    compile_select(registry, User.select(User.id).where(User.id == 2))
    assert registry.cached_plan_count == 1


def test_the_plan_cache_is_bounded_and_evicts_the_oldest() -> None:
    registry = Registry(
        FakeDatabase(), [User, Post, Membership], validate_schema="off", query_cache_size=2
    )
    for size in (1, 2, 3):
        compile_select(registry, User.select(User.id).where(User.id.in_(list(range(size)))))
    assert registry.cached_plan_count == 2


def test_the_plan_cache_also_obeys_its_byte_budget() -> None:
    registry = Registry(
        FakeDatabase(),
        [User, Post, Membership],
        validate_schema="off",
        query_cache_bytes=1,
    )
    compile_select(registry, User.select(User.id).where(User.id == 1))
    assert registry.cached_plan_count == 0


def test_a_cache_hit_still_extracts_this_query_s_values(registry: Registry) -> None:
    # Skipping SQL generation must not skip reading the values, and the walk
    # that collects them must agree with the one that emitted placeholders.
    query = User.select(User.id).where(User.name == "A").limit(5)
    fresh = compile_select(registry, query)
    hit = compile_select(registry, User.select(User.id).where(User.name == "B").limit(9))
    assert fresh.sql == hit.sql
    assert hit.bind_values == ("B", 9)


def test_cache_hit_executes_compiled_bind_program(monkeypatch, registry: Registry) -> None:
    compile_select(registry, User.select(User.id).where(User.name == "A").limit(5))

    def fail_collect(*_args, **_kwargs):
        raise AssertionError("cache hit traversed the expression tree")

    walker = (
        "_collect_value_nodes"
        if hasattr(compiler_module, "_collect_value_nodes")
        else "_walk_values"
    )
    monkeypatch.setattr(compiler_module, walker, fail_collect)
    hit = compile_select(registry, User.select(User.id).where(User.name == "B").limit(9))

    assert hit.bind_values == ("B", 9)


@pytest.mark.parametrize(
    "query",
    [
        User.select(User.id).where(User.id == 1),
        User.select().where((User.name == "A") | (User.email == "b@c")),
        User.select().where(not_(User.id.in_([1, 2, 3]))),
        User.select().where(and_(User.name == "A", User.created_at.is_null())),
        User.select().where(User.email.like("a%")).limit(3).offset(6),
        Post.select().include(Post.author.joined()).where(Post.title == "t"),
    ],
)
def test_bind_collection_matches_placeholder_order(registry: Registry, query: object) -> None:
    compiled = compile_select(registry, query)  # type: ignore[arg-type]
    values, oids = _collect_binds(query)  # type: ignore[arg-type]
    assert compiled.bind_values == values
    assert compiled.bind_oids == oids
    assert compiled.sql.count("$") == len(values)


def test_registries_do_not_share_cached_plans() -> None:
    first = Registry(FakeDatabase("a"), [User, Post], validate_schema="off")
    second = Registry(FakeDatabase("b"), [User, Post], validate_schema="off")
    compile_select(first, User.select(User.id))
    assert first.cached_plan_count == 1
    assert second.cached_plan_count == 0


def test_count_selects_star_aggregate_not_columns(registry: Registry) -> None:
    sql, values, oids = compile_count(registry, User.select())
    assert sql == f"SELECT COUNT(*) FROM {USERS}"
    assert values == () and oids == ()


def test_count_keeps_predicates_and_binds_their_values(registry: Registry) -> None:
    sql, values, oids = compile_count(registry, User.select().where(User.id == 42))
    assert sql == f'SELECT COUNT(*) FROM {USERS} WHERE "t0"."id" = $1'
    assert values == (42,) and oids == (Int64.oid,)


def test_count_drops_projection_ordering_and_paging(registry: Registry) -> None:
    query = (
        User.select(User.email)
        .where(User.name == "A")
        .order_by(User.id.desc())
        .limit(10)
        .offset(20)
    )
    sql, values, _ = compile_count(registry, query)
    assert sql == f'SELECT COUNT(*) FROM {USERS} WHERE "t0"."name" = $1'
    assert "ORDER BY" not in sql and "LIMIT" not in sql and "OFFSET" not in sql
    assert values == ("A",)


def test_count_emits_inner_join_for_a_relationship_filter(registry: Registry) -> None:
    sql, values, _ = compile_count(registry, Post.select().where(Post.author.email == "x@y.z"))
    assert sql.startswith("SELECT COUNT(*) FROM ")
    assert "JOIN" in sql and '"users"' in sql
    assert values == ("x@y.z",)


def test_count_ignores_a_projected_count_query_never_returns_star(registry: Registry) -> None:
    # A to-one filter join must not multiply the count; the shared prefix rule
    # in _plan_filter_joins means one join even for two columns of the relation.
    sql, _, _ = compile_count(
        registry,
        Post.select().where((Post.author.email == "x") & (Post.author.name == "y")),
    )
    assert sql.count("JOIN") == 1


def test_in_with_a_subquery_renders_one_statement(registry: Registry) -> None:
    compiled = compile_select(
        registry,
        Post.select(Post.id, Post.title).where(
            Post.title == "hi",
            Post.author_id.in_(User.select(User.id).where(User.email == "a@b.c")),
        ),
    )
    assert compiled.sql == (
        'SELECT "t0"."id", "t0"."title" FROM "public"."posts" AS "t0" '
        'WHERE ("t0"."title" = $1 AND "t0"."author_id" IN '
        '(SELECT "t0s"."id" FROM "public"."users" AS "t0s" WHERE "t0s"."email" = $2))'
    )
    # Positional, and in render order: the outer predicate binds before the
    # subquery's because that is the order the text emits them.
    assert compiled.bind_values == ("hi", "a@b.c")


def test_not_in_with_a_subquery_renders_the_negated_form(registry: Registry) -> None:
    compiled = compile_select(
        registry,
        Post.select(Post.id).where(
            Post.author_id.not_in(User.select(User.id).where(User.name == "spam"))
        ),
    )
    assert compiled.sql == (
        'SELECT "t0"."id" FROM "public"."posts" AS "t0" '
        'WHERE "t0"."author_id" NOT IN '
        '(SELECT "t0s"."id" FROM "public"."users" AS "t0s" WHERE "t0s"."name" = $1)'
    )
    assert compiled.bind_values == ("spam",)


def test_a_nested_subquery_gets_its_own_alias(registry: Registry) -> None:
    compiled = compile_select(
        registry,
        Post.select(Post.id).where(
            Post.author_id.in_(
                User.select(User.id).where(
                    User.id.in_(User.select(User.id).where(User.name == "x"))
                )
            )
        ),
    )
    assert '"t0s"' in compiled.sql
    assert '"t0ss"' in compiled.sql
    assert compiled.bind_values == ("x",)


def test_count_renders_the_subquery_too(registry: Registry) -> None:
    query = Post.select(Post.id).where(
        Post.author_id.in_(User.select(User.id).where(User.email == "a@b.c"))
    )
    sql, values, _ = compile_count(registry, query)
    assert sql == (
        'SELECT COUNT(*) FROM "public"."posts" AS "t0" '
        'WHERE "t0"."author_id" IN '
        '(SELECT "t0s"."id" FROM "public"."users" AS "t0s" WHERE "t0s"."email" = $1)'
    )
    assert values == ("a@b.c",)


def test_the_subquerys_values_stay_out_of_the_shape_key(registry: Registry) -> None:
    def key(email_column: object, operator: str = "in") -> bytes:
        subquery = User.select(User.id).where(email_column)
        predicate = (
            Post.author_id.in_(subquery) if operator == "in" else Post.author_id.not_in(subquery)
        )
        return shape_of(registry, Post.select(Post.id).where(predicate))

    assert key(User.email == "one") == key(User.email == "two")
    assert key(User.email == "one") != key(User.name == "one")
    assert key(User.email == "one") != key(User.email == "one", "not_in")


def test_a_subquery_over_a_different_model_keys_differently(registry: Registry) -> None:
    from .conftest import Membership

    first = shape_of(
        registry,
        Post.select(Post.id).where(Post.author_id.in_(User.select(User.id))),
    )
    second = shape_of(
        registry,
        Post.select(Post.id).where(Post.author_id.in_(Membership.select(Membership.org_id))),
    )
    assert first != second


def test_a_subquery_must_project_exactly_one_column() -> None:
    with pytest.raises(DeclarationError, match="projects every column"):
        Post.author_id.in_(User.select())
    with pytest.raises(DeclarationError, match=r"projects 2 \(id, email\)"):
        Post.author_id.in_(User.select(User.id, User.email))


def test_a_subquery_refuses_ordering_paging_locking_and_loads() -> None:
    with pytest.raises(DeclarationError, match="order_by/limit/offset"):
        Post.author_id.in_(User.select(User.id).limit(5))
    with pytest.raises(DeclarationError, match="order_by/limit/offset"):
        Post.author_id.in_(User.select(User.id).offset(5))
    with pytest.raises(DeclarationError, match="order_by/limit/offset"):
        Post.author_id.in_(User.select(User.id).order_by(User.id))
    with pytest.raises(DeclarationError, match="for_update"):
        Post.author_id.in_(User.select(User.id).for_update())


def test_a_subquery_refuses_a_relationship_traversal() -> None:
    with pytest.raises(DeclarationError, match="through a relationship"):
        User.id.in_(Post.select(Post.author_id).where(Post.author.name == "x"))


def test_not_in_refuses_a_nullable_subquery_column() -> None:
    with pytest.raises(DeclarationError, match="matches nothing at all"):
        Post.author_id.not_in(User.select(User.created_at))
    # `IN` has no such hazard: a NULL simply never matches.
    Post.author_id.in_(User.select(User.created_at))


def test_the_list_form_is_unchanged(registry: Registry) -> None:
    compiled = compile_select(registry, Post.select(Post.id).where(Post.author_id.in_([1, 2, 3])))
    assert compiled.sql.endswith('WHERE "t0"."author_id" IN ($1, $2, $3)')
    assert compiled.bind_values == (1, 2, 3)
    with pytest.raises(ValueError, match="at least one value"):
        Post.author_id.in_([])
    with pytest.raises(TypeError, match="iterable of values, or a Select"):
        Post.author_id.in_(3)


def test_a_predicate_naming_another_models_column_is_still_caught() -> None:
    from wreath.orm.compiler import check_predicate_columns

    good = Post.author_id.in_(User.select(User.id).where(User.email == "a"))
    check_predicate_columns(Post, good)
    with pytest.raises(DeclarationError, match="not a column of User"):
        check_predicate_columns(User, good)
    with pytest.raises(DeclarationError, match="not a column of User"):
        check_predicate_columns(
            Post, Post.author_id.in_(User.select(User.id).where(Post.title == "x"))
        )
