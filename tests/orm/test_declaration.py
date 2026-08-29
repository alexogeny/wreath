from __future__ import annotations

import datetime
import uuid

import pytest

from wreath.orm import Mapped, Model, column, relationship
from wreath.orm.errors import (
    DeclarationError,
    UnloadedAttributeError,
    UnloadedRelationshipError,
)
from wreath.orm.expressions import ColumnExpr
from wreath.orm.model import PERSISTENT, TRANSIENT
from wreath.orm.registry import Registry
from wreath.orm.schema import fingerprint_model
from wreath.orm.types import Bool, Int64, Json, Text, Timestamp, Uuid

from .conftest import FakeDatabase, Post, User


def compile_models(*models: type) -> Registry:
    return Registry(FakeDatabase(), list(models), validate_schema="off")


def test_columns_keep_class_body_order() -> None:
    assert [item.python_name for item in User.__wreath_columns__] == [
        "id",
        "email",
        "name",
        "created_at",
    ]
    assert [item.index for item in User.__wreath_columns__] == [0, 1, 2, 3]


def test_inherited_columns_precede_subclass_columns() -> None:
    class Timestamps(Model):
        created_at: Mapped[object] = column(Timestamp, nullable=True)
        updated_at: Mapped[object] = column(Timestamp, nullable=True)

    class Widget(Timestamps, table="widgets"):
        id: Mapped[int] = column(Int64, primary_key=True)
        label: Mapped[str] = column(Text)

    assert [item.python_name for item in Widget.__wreath_columns__] == [
        "created_at",
        "updated_at",
        "id",
        "label",
    ]


def test_a_mixin_column_gets_its_own_index_per_model() -> None:
    # The prototype is shared, so each model must clone it; otherwise two
    # models would fight over one storage index.
    class Keyed(Model):
        id: Mapped[int] = column(Int64, primary_key=True)

    class Left(Keyed, table="left_side"):
        a: Mapped[str] = column(Text)

    class Right(Keyed, table="right_side"):
        b: Mapped[str] = column(Text)
        c: Mapped[str] = column(Text)

    assert Left.__wreath_column_map__["id"] is not Right.__wreath_column_map__["id"]
    assert Left.__wreath_column_map__["id"].owner is Left
    assert Right.__wreath_column_map__["id"].owner is Right


def test_a_mapped_model_cannot_be_subclassed_into_another_table() -> None:
    with pytest.raises(DeclarationError, match="table-less base class"):

        class Admin(User, table="admins"):
            pass


def test_class_access_yields_an_expression_and_instance_access_a_value() -> None:
    assert isinstance(User.id, ColumnExpr)
    assert User.id.column is User.__wreath_column_map__["id"]
    instance = User(id=1, email="a@b.c", name="A")
    assert instance.id == 1


def test_a_model_without_a_primary_key_is_rejected() -> None:
    with pytest.raises(DeclarationError, match="primary-key"):

        class NoKey(Model, table="no_key"):
            name: Mapped[str] = column(Text)


def test_duplicate_python_names_are_rejected() -> None:
    # A duplicate name in one class body is a Python-level overwrite, so the
    # duplicate that matters comes through inheritance.
    class Base(Model):
        name: Mapped[str] = column(Text)

    class Other(Model):
        name: Mapped[str] = column(Text)

    with pytest.raises(DeclarationError, match="twice"):

        class Clash(Base, Other, table="clash"):
            id: Mapped[int] = column(Int64, primary_key=True)


@pytest.mark.parametrize(
    "name",
    [
        "Users",
        "user table",
        "1users",
        "user-table",
        "",
        # `$` matches immediately before a trailing newline, so an anchored
        # `^...$` accepted this and the table name reached DDL carrying it.
        "users\n",
    ],
)
def test_invalid_identifiers_are_rejected(name: str) -> None:
    with pytest.raises(DeclarationError, match="identifier"):

        class Bad(Model, table=name):
            id: Mapped[int] = column(Int64, primary_key=True)


def test_over_long_identifiers_are_rejected() -> None:
    with pytest.raises(DeclarationError, match="63-byte"):

        class Long(Model, table="t" * 64):
            id: Mapped[int] = column(Int64, primary_key=True)


def test_column_requires_a_pg_type() -> None:
    with pytest.raises(DeclarationError, match="PgType"):
        column(int)  # type: ignore[arg-type]


def test_a_primary_key_cannot_be_nullable() -> None:
    with pytest.raises(DeclarationError, match="nullable"):
        column(Int64, primary_key=True, nullable=True)


def test_references_requires_a_column_expression() -> None:
    with pytest.raises(DeclarationError, match="references="):
        column(Int64, references="users.id")


def test_an_unknown_load_strategy_is_rejected() -> None:
    with pytest.raises(DeclarationError, match="load strategy"):
        relationship("Post", foreign_key="author_id", load="lazy")


def test_duplicate_registration_is_rejected() -> None:
    with pytest.raises(DeclarationError, match="registered twice"):
        compile_models(User, User)


def test_two_models_mapping_one_table_are_rejected() -> None:
    class Shadow(Model, table="users"):
        id: Mapped[int] = column(Int64, primary_key=True)

    with pytest.raises(DeclarationError, match="both map"):
        compile_models(User, Shadow)


def test_constructor_takes_keywords_only() -> None:
    with pytest.raises(TypeError):
        User(1, "a@b.c")  # type: ignore[misc]


def test_unknown_constructor_keys_are_rejected() -> None:
    with pytest.raises(TypeError, match="no column"):
        User(id=1, email="a@b.c", name="A", nickname="nope")


def test_a_missing_non_null_field_without_a_default_is_rejected() -> None:
    with pytest.raises(TypeError, match="not nullable and has no default"):
        User(id=1, email="a@b.c")


def test_a_python_default_is_applied_at_construction() -> None:
    class Flagged(Model, table="flagged"):
        id: Mapped[int] = column(Int64, primary_key=True)
        active: Mapped[bool] = column(Bool, default=True)

    assert Flagged(id=1).active is True


def test_a_callable_default_is_called_per_instance() -> None:
    class Keyed(Model, table="keyed"):
        id: Mapped[object] = column(Uuid, primary_key=True, default=uuid.uuid4)

    assert Keyed().id != Keyed().id


def test_a_server_default_leaves_the_field_unloaded() -> None:
    class Stamped(Model, table="stamped"):
        id: Mapped[int] = column(Int64, primary_key=True)
        created_at: Mapped[object] = column(Timestamp, server_default="now()")

    instance = Stamped(id=1)
    assert not instance._orm_is_loaded(1)
    with pytest.raises(UnloadedAttributeError):
        instance.created_at  # noqa: B018 - the read is the subject


def test_an_omitted_primary_key_stays_unloaded_for_returning() -> None:
    # The database generates it; INSERT ... RETURNING fills it in on flush.
    instance = User(email="a@b.c", name="A")
    assert not instance._orm_is_loaded(0)


def test_a_nullable_field_may_be_omitted() -> None:
    instance = User(id=1, email="a@b.c", name="A")
    assert not instance._orm_is_loaded(3)


def test_reading_an_unloaded_column_raises() -> None:
    instance = User._orm_new()
    with pytest.raises(UnloadedAttributeError, match="was not loaded"):
        instance.email  # noqa: B018 - the read is the subject


def test_reading_an_unloaded_relationship_raises() -> None:
    instance = Post._orm_new()
    with pytest.raises(UnloadedRelationshipError, match="was not loaded"):
        instance.author  # noqa: B018 - the read is the subject


def test_assignment_validates_through_the_column_type() -> None:
    instance = User(id=1, email="a@b.c", name="A")
    with pytest.raises(TypeError, match="expected str"):
        instance.email = 5


def test_assigning_none_to_a_non_nullable_column_fails_before_sql() -> None:
    instance = User(id=1, email="a@b.c", name="A")
    with pytest.raises(ValueError, match="not nullable"):
        instance.email = None


def test_assigning_none_to_a_nullable_column_is_a_null_not_an_absence() -> None:
    instance = User(id=1, email="a@b.c", name="A", created_at=datetime.datetime(2024, 1, 1))
    instance.created_at = None
    assert instance.created_at is None
    assert instance._orm_is_null(3)
    assert instance._orm_is_loaded(3)


def test_assignment_marks_dirty_only_when_the_value_changes() -> None:
    instance = User._orm_new()
    instance._orm_state = PERSISTENT
    instance._orm_set_loaded(1, "a@b.c")
    instance.email = "a@b.c"
    assert not instance._orm_is_dirty(1)
    instance.email = "other@x.y"
    assert instance._orm_is_dirty(1)


def test_a_transient_object_does_not_accumulate_dirty_fields() -> None:
    instance = User(id=1, email="a@b.c", name="A")
    assert instance._orm_state == TRANSIENT
    assert not instance._orm_has_changes()


def test_primary_key_mutation_on_a_persistent_object_is_rejected() -> None:
    instance = User._orm_new()
    instance._orm_set_loaded(0, 1)
    instance._orm_state = PERSISTENT
    with pytest.raises(DeclarationError, match="primary key"):
        instance.id = 2


def test_primary_key_is_none_while_any_component_is_unloaded() -> None:
    from .conftest import Membership

    instance = Membership._orm_new()
    instance._orm_set_loaded(0, 1)
    assert instance._orm_primary_key() is None
    instance._orm_set_loaded(1, 2)
    assert instance._orm_primary_key() == (1, 2)


def test_json_columns_hold_python_values_and_convert_at_the_wire() -> None:
    class Document(Model, table="documents"):
        id: Mapped[int] = column(Int64, primary_key=True)
        body: Mapped[object] = column(Json)

    instance = Document(id=1, body={"a": [1, 2]})
    assert instance.body == {"a": [1, 2]}
    assert Json.to_wire(instance.body) == '{"a":[1,2]}'
    assert Json.from_wire('{"a":[1,2]}') == {"a": [1, 2]}


def test_an_unencodable_json_value_is_rejected_on_assignment() -> None:
    class Document(Model, table="documents_two"):
        id: Mapped[int] = column(Int64, primary_key=True)
        body: Mapped[object] = column(Json)

    with pytest.raises((TypeError, ValueError)):
        Document(id=1, body=object())


def test_fingerprints_are_stable_across_registries() -> None:
    first = compile_models(User, Post)
    second = compile_models(User, Post)
    assert first.fingerprint == second.fingerprint
    assert first.spec_for(User).fingerprint == second.spec_for(User).fingerprint


def test_fingerprints_are_stable_across_processes() -> None:
    # Recomputing from the same inputs must not depend on PYTHONHASHSEED or
    # object addresses.
    spec = compile_models(User, Post).spec_for(User)
    assert (
        fingerprint_model(spec.schema, spec.table, spec.columns, spec.relationships)
        == spec.fingerprint
    )


def test_a_changed_column_type_changes_the_fingerprint() -> None:
    class V1(Model, table="versioned"):
        id: Mapped[int] = column(Int64, primary_key=True)
        value: Mapped[str] = column(Text)

    class V2(Model, table="versioned"):
        id: Mapped[int] = column(Int64, primary_key=True)
        value: Mapped[int] = column(Int64)

    assert (
        compile_models(V1).spec_for(V1).fingerprint != compile_models(V2).spec_for(V2).fingerprint
    )


def test_a_changed_column_order_changes_the_fingerprint() -> None:
    class A(Model, table="ordered"):
        id: Mapped[int] = column(Int64, primary_key=True)
        x: Mapped[str] = column(Text)
        y: Mapped[str] = column(Text)

    class B(Model, table="ordered"):
        id: Mapped[int] = column(Int64, primary_key=True)
        y: Mapped[str] = column(Text)
        x: Mapped[str] = column(Text)

    assert compile_models(A).spec_for(A).fingerprint != compile_models(B).spec_for(B).fingerprint


def test_a_lambda_default_is_rejected_as_unfingerprintable() -> None:
    class Bad(Model, table="bad_default"):
        id: Mapped[int] = column(Int64, primary_key=True)
        value: Mapped[int] = column(Int64, default=lambda: 1)

    with pytest.raises(DeclarationError, match="no stable name"):
        compile_models(Bad)


def test_registry_fingerprint_covers_every_model() -> None:
    from .conftest import Membership

    assert (
        compile_models(User, Post).fingerprint != compile_models(User, Post, Membership).fingerprint
    )


def test_a_relationship_target_outside_the_registry_is_rejected() -> None:
    # String targets resolve only inside the registry that owns both models.
    with pytest.raises(DeclarationError, match="does not contain"):
        compile_models(User)


def test_a_foreign_key_pointing_at_a_third_table_is_refused() -> None:

    class Other(Model, table="others"):
        id: Mapped[int] = column(Int64, primary_key=True)

    class Owner(Model, table="owners"):
        id: Mapped[int] = column(Int64, primary_key=True)

    class Pet(Model, table="pets"):
        id: Mapped[int] = column(Int64, primary_key=True)
        owner_id: Mapped[int] = column(Int64, references=Other.id)
        owner = relationship(Owner, foreign_key=owner_id)

    with pytest.raises(DeclarationError) as caught:
        compile_models(Other, Owner, Pet)
    message = str(caught.value)
    # Both sides named: which declaration is wrong is the whole diagnosis.
    assert "Pet.owner" in message and "owner_id" in message
    assert "public.others" in message and "public.owners" in message


def test_a_foreign_key_joins_the_column_it_references_not_the_primary_key() -> None:
    from wreath.orm.compiler import compile_select

    class Country(Model, table="countries"):
        id: Mapped[int] = column(Int64, primary_key=True)
        code: Mapped[str] = column(Text, unique=True)

    class City(Model, table="cities"):
        id: Mapped[int] = column(Int64, primary_key=True)
        country_code: Mapped[str] = column(Text, references=Country.code)
        country = relationship(Country, foreign_key=country_code)

    registry = compile_models(Country, City)
    sql = compile_select(registry, City.select().include(City.country.joined())).sql
    assert 'ON "j1"."code" = "t0"."country_code"' in sql, sql
    assert '"j1"."id" =' not in sql, sql


def test_a_foreign_key_with_no_reference_falls_back_to_the_primary_key() -> None:
    from wreath.orm.compiler import compile_select

    class Owner(Model, table="owners"):
        id: Mapped[int] = column(Int64, primary_key=True)

    class Pet(Model, table="pets"):
        id: Mapped[int] = column(Int64, primary_key=True)
        owner_id: Mapped[int] = column(Int64)  # deliberately no references=
        owner = relationship(Owner, foreign_key=owner_id)

    registry = compile_models(Owner, Pet)
    sql = compile_select(registry, Pet.select().include(Pet.owner.joined())).sql
    assert 'ON "j1"."id" = "t0"."owner_id"' in sql, sql


def test_a_json_column_serializes_the_value_it_holds_at_the_wire_not_at_assignment() -> None:
    class Document(Model, table="documents_mutated"):
        id: Mapped[int] = column(Int64, primary_key=True)
        body: Mapped[object] = column(Json)

    instance = Document(id=1, body={"a": [1, 2]})
    body = instance.body
    assert isinstance(body, dict)
    body["a"].append(3)

    assert Json.to_wire(instance.body) == '{"a":[1,2,3]}'
