"""Declared, named reads: `wreath.queries`.

Runs against the ORM suite's scriptable fake driver (`tests/orm/conftest.py`),
so these exercise the real compiler, plan cache, and session without a database.
"""

from __future__ import annotations

import pytest

# The ORM suite's scriptable driver, reused rather than reinvented; its fixtures
# are scoped to tests/orm/, so the three below rebuild them here.
from tests.orm.conftest import FakeDatabase, Membership, Post, User, user_row
from wreath.orm import DeclarationError, ORMError, Select
from wreath.orm import compiler as orm_compiler
from wreath.orm.compiler import shape_of
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.queries import Param, Queries, query


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def registry(database: FakeDatabase) -> Registry:
    return Registry(database, [User, Post, Membership], validate_schema="off")


@pytest.fixture
def session(registry: Registry) -> Session:
    return Session(registry, "write")


class Users(Queries[User]):
    """The declaration under test, in the shape the guide documents."""

    by_email = query(User.email == Param("email")).one()
    named = query(User.name == Param("name")).order_by(User.id)
    newer_than = query(User.id > Param("id")).order_by(User.id.desc()).limit(5)
    everyone = query().order_by(User.id)
    in_paddock = query(User.name == Param("name"), User.email == Param("email"))


def scripted(session) -> None:
    session.registry.database.connection.script("FROM", [user_row(1), user_row(2)])


# -- the surface ---------------------------------------------------------------


async def test_declared_query_binds_its_parameter(session):
    scripted(session)
    rows = await Users(session).named(name="A")
    assert [row.id for row in rows] == [1, 2]
    sql, args = session.registry.database.connection.calls[-1]
    assert '"name" = $1' in sql
    assert args == ("A",)


async def test_one_returns_a_single_object(session):
    session.registry.database.connection.script("FROM", [user_row(7)])
    found = await Users(session).by_email(email="a@b.c")
    assert found.id == 7


async def test_one_returns_none_on_a_miss(session):
    assert await Users(session).by_email(email="nobody@example.com") is None


async def test_declaration_without_parameters_takes_no_arguments(session):
    scripted(session)
    rows = await Users(session).everyone()
    assert len(rows) == 2
    assert " ORDER BY " in session.registry.database.connection.statements[-1]


async def test_several_parameters_bind_in_declaration_order(session):
    scripted(session)
    await Users(session).in_paddock(name="A", email="a@b.c")
    _sql, args = session.registry.database.connection.calls[-1]
    assert args == ("A", "a@b.c")


async def test_builder_methods_survive_onto_the_statement(session):
    scripted(session)
    await Users(session).newer_than(id=3)
    sql, args = session.registry.database.connection.calls[-1]
    assert " ORDER BY " in sql and " DESC" in sql and " LIMIT " in sql
    assert args == (3, 5)


async def test_the_same_parameter_name_binds_every_site(session):
    class Twice(Queries[User]):
        both = query((User.name == Param("word")) | (User.email == Param("word")))

    scripted(session)
    await Twice(session).both(word="a@b.c")
    _sql, args = session.registry.database.connection.calls[-1]
    assert args == ("a@b.c", "a@b.c")


async def test_calls_do_not_leak_values_into_each_other(session):
    scripted(session)
    users = Users(session)
    await users.named(name="A")
    await users.named(name="B")
    first, second = session.registry.database.connection.calls[-2:]
    assert first[1] == ("A",)
    assert second[1] == ("B",)


async def test_related_column_parameter_joins_and_binds(session):
    class Posts(Queries[Post]):
        by_author_name = query(Post.author.name == Param("name"))

    session.registry.database.connection.script("FROM", [])
    await Posts(session).by_author_name(name="A")
    sql, args = session.registry.database.connection.calls[-1]
    assert "JOIN" in sql
    assert args == ("A",)


async def test_constants_and_parameters_mix_in_one_declaration(session):
    """Only the parameterised part is rebuilt; the constants are declared once."""

    class Mixed(Queries[User]):
        matching = query(
            User.id.in_([1, 2, 3]) & (User.name == Param("name")),
            ~User.created_at.is_null(),
        )

    scripted(session)
    await Mixed(session).matching(name="A")
    sql, args = session.registry.database.connection.calls[-1]
    assert "IN ($1, $2, $3)" in sql and "NOT" in sql
    assert args == (1, 2, 3, "A")


# -- the plan cache -----------------------------------------------------------


async def test_one_shape_per_declaration_however_many_calls(session):
    scripted(session)
    users = Users(session)
    before = session.registry.cached_plan_count
    for value in ("A", "B", "C"):
        await users.named(name=value)
    assert session.registry.cached_plan_count == before + 1


async def test_a_declaration_derives_its_shape_only_once_per_registry(
    session, monkeypatch
):
    scripted(session)
    calls = 0
    original = orm_compiler.shape_of

    def counted_shape(registry, select):
        nonlocal calls
        calls += 1
        return original(registry, select)

    monkeypatch.setattr(orm_compiler, "shape_of", counted_shape)
    users = Users(session)
    await users.named(name="A")
    await users.named(name="B")
    await users.named(name="C")

    assert calls == 1


def test_a_declaration_keys_identically_to_the_hand_written_query(registry):
    declared = Users.named.bind(name="A")
    handwritten = Select.build(User, ()).where(User.name == "A").order_by(User.id)
    assert shape_of(registry, declared) == shape_of(registry, handwritten)


def test_a_leaked_placeholder_cannot_be_compiled(registry):
    """A declaration bypassed rather than bound must fail, not run half-bound."""
    unbound = Select.build(User, ()).where(User.name == Param("name"))
    with pytest.raises(ORMError):
        shape_of(registry, unbound)


def test_a_bound_select_is_an_ordinary_select(registry):
    bound = Users.named.bind(name="A")
    assert isinstance(bound, Select)
    assert bound.model is User


# -- declaration-time validation ----------------------------------------------


def test_a_foreign_column_fails_at_class_definition():
    with pytest.raises(DeclarationError, match="not a column of User"):

        class Wrong(Queries[User]):
            broken = query(Membership.role == Param("role"))


def test_a_missing_model_parameter_fails_at_class_definition():
    with pytest.raises(DeclarationError, match="Queries"):

        class Untyped(Queries):  # ty: ignore[missing-type-parameter]
            anything = query()


def test_a_non_model_parameter_fails_at_class_definition():
    with pytest.raises(DeclarationError, match="model"):

        class NotAModel(Queries[int]):  # ty: ignore[invalid-type-argument]
            anything = query()


def test_a_parameter_in_order_by_fails_at_class_definition():
    with pytest.raises(DeclarationError, match="order_by"):

        class Ordered(Queries[User]):
            broken = query().order_by(Param("column"))


def test_a_parameter_in_limit_fails_at_class_definition():
    with pytest.raises(DeclarationError, match="limit"):

        class Limited(Queries[User]):
            broken = query().limit(Param("size"))


def test_a_predicate_that_is_not_a_predicate_fails_at_class_definition():
    with pytest.raises(TypeError, match="predicate"):

        class Bad(Queries[User]):
            broken = query("id = 1")


def test_a_parameter_compared_to_a_value_fails_where_it_is_written():
    with pytest.raises(TypeError, match="model column"):
        Param("x") == 5  # noqa: B015 -- the comparison is the assertion


def test_an_abstract_intermediate_needs_no_model():
    class Base(Queries[User]):
        pass

    class Concrete(Base):
        named = query(User.name == Param("name"))

    assert Concrete.model is User


def test_a_declaration_cannot_be_extended_after_the_class_claims_it():
    with pytest.raises(DeclarationError, match="already declared"):
        Users.named.limit(3)


def test_an_unclaimed_declaration_cannot_be_bound():
    with pytest.raises(DeclarationError, match="Queries subclass"):
        query(User.name == Param("name")).bind(name="A")


# -- per-call validation ------------------------------------------------------


async def test_a_missing_parameter_is_reported_by_name(session):
    with pytest.raises(TypeError, match="missing parameter 'name'"):
        await Users(session).named()


async def test_an_unknown_parameter_is_reported_by_name(session):
    with pytest.raises(TypeError, match="unexpected parameter 'colour'"):
        await Users(session).named(name="A", colour="brown")


async def test_a_declaration_without_parameters_rejects_arguments(session):
    with pytest.raises(TypeError, match="takes no parameters"):
        await Users(session).everyone(name="A")


async def test_a_mistyped_value_names_the_parameter(session):
    with pytest.raises(TypeError, match="parameter 'id'"):
        await Users(session).newer_than(id="three")


async def test_count_uses_the_declared_filters(session):
    session.registry.database.connection.script("COUNT", [[4]])
    assert await Users(session).named.count(name="A") == 4
    sql, args = session.registry.database.connection.calls[-1]
    assert sql.startswith("SELECT COUNT(*)")
    assert args == ("A",)


# -- naming -------------------------------------------------------------------


def test_a_declaration_knows_its_own_name():
    assert Users.named.name == "Users.named"
    assert Users.named.parameters == ("name",)
    assert repr(Users.named) == "<query Users.named(name)>"


def test_declarations_are_discoverable_by_name():
    assert set(Users.declarations()) == {
        "by_email",
        "named",
        "newer_than",
        "everyone",
        "in_paddock",
    }


def test_the_bound_query_carries_its_session(session):
    users = Users(session)
    assert users.session is session
    assert repr(users.named) == "<Users.named bound>"
