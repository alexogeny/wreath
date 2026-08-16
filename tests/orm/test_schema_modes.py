"""Logical schema roles compile once into specialized registry metadata."""

from __future__ import annotations

import pytest

from wreath.orm import (
    CENTRAL_SCHEMA,
    TENANT_SCHEMA,
    Mapped,
    Model,
    SchemaMode,
    Session,
    TenantContext,
    column,
    relationship,
)
from wreath.orm.compiler import compile_select
from wreath.orm.errors import DeclarationError, SessionError
from wreath.orm.registry import Registry
from wreath.orm.types import Int64

from .conftest import FakeDatabase


class CentralAccount(Model, table="accounts", schema=CENTRAL_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)


class TenantOrder(Model, table="orders", schema=TENANT_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    account_id: Mapped[int] = column(Int64, references=CentralAccount.id)
    account = relationship(CentralAccount, foreign_key=account_id)


class FixedArchive(Model, table="events", schema="archive"):
    id: Mapped[int] = column(Int64, primary_key=True)


def registry(mode: SchemaMode) -> Registry:
    return Registry(
        FakeDatabase(),
        [CentralAccount, TenantOrder, FixedArchive],
        validate_schema="off",
        schema_mode=mode,
    )


def test_single_schema_resolves_logical_roles_and_keeps_fixed_schema() -> None:
    compiled = registry(SchemaMode.single("app"))

    assert compiled.spec_for(CentralAccount).schema == "app"
    assert compiled.spec_for(TenantOrder).schema == "app"
    assert compiled.spec_for(FixedArchive).schema == "archive"
    assert compiled.spec_for(TenantOrder).sql_namespace == "qualified"


def test_isolated_schema_compiles_shared_unqualified_tenant_sql() -> None:
    compiled = registry(SchemaMode.isolated(central="wreath_core", isolation="role"))

    assert compiled.spec_for(CentralAccount).schema == "wreath_core"
    assert compiled.spec_for(TenantOrder).sql_namespace == "tenant_search_path"
    assert 'FROM "orders" AS "t0"' in compile_select(
        compiled, TenantOrder.select()
    ).sql
    assert 'LEFT JOIN "wreath_core"."accounts"' in compile_select(
        compiled, TenantOrder.select().include(TenantOrder.account.joined())
    ).sql


def test_isolated_session_requires_a_tenant_context() -> None:
    compiled = registry(SchemaMode.isolated(central="wreath_core", isolation="role"))

    with pytest.raises(SessionError, match="needs a tenant context"):
        Session(compiled, "read")


def test_isolated_session_binds_a_tenant_context() -> None:
    compiled = registry(SchemaMode.isolated(central="wreath_core", isolation="role"))

    session = Session(
        compiled, "read", tenant=TenantContext(schema="tenant_9", role="tenant_9_role")
    )
    assert not session.closed


def test_template_fingerprint_ignores_single_deployment_schema() -> None:
    left = registry(SchemaMode.single("app_a"))
    right = registry(SchemaMode.single("app_b"))

    assert left.template_fingerprint == right.template_fingerprint
    assert left.deployment_fingerprint != right.deployment_fingerprint


def test_central_to_tenant_relationship_is_rejected() -> None:
    class TenantItem(Model, table="items", schema=TENANT_SCHEMA):
        id: Mapped[int] = column(Int64, primary_key=True)
        central_id: Mapped[int] = column(Int64)

    class CentralOwner(Model, table="owners", schema=CENTRAL_SCHEMA):
        id: Mapped[int] = column(Int64, primary_key=True)
        items = relationship(TenantItem, foreign_key=TenantItem.central_id)

    with pytest.raises(DeclarationError, match="central.*tenant"):
        Registry(
            FakeDatabase(),
            [CentralOwner, TenantItem],
            validate_schema="off",
            schema_mode=SchemaMode.isolated(central="wreath_core"),
        )


@pytest.mark.parametrize("name", ["not valid", "App", "1app", "app\n"])
def test_schema_mode_rejects_invalid_physical_identifiers(name: str) -> None:
    """`"app\\n"` is here because `$` matches before a trailing newline.

    The anchored `^...$` this validator used accepted it, and a schema name
    reaches every `CREATE SCHEMA` and every qualified reference as text.
    """
    with pytest.raises(DeclarationError, match="schema name"):
        SchemaMode.single(name)


# -- refusals and the fingerprint, from a mutation sweep -----------------------
#
# A sweep over `orm/schema.py` scored 0.53. Every finding was the same shape as the
# `orm/types.py` sweep before it: the happy paths were covered and the *refusals*
# were not, so deleting one left the suite green. Two of them were `unreached` --
# no test executed the `raise` at all.


def test_isolated_refuses_an_isolation_it_does_not_implement() -> None:
    """The `raise` in `SchemaMode.isolated`, which no test reached.

    `isolation` is annotated `Literal["namespace", "role"]`, so a type checker
    catches a literal typo -- but the value routinely arrives from configuration,
    where nothing is checked, and "the annotation says so" is not a runtime
    guarantee. Removing this refusal built a `SchemaMode` claiming an isolation
    strategy the compiler has no branch for.
    """
    with pytest.raises(DeclarationError, match="isolation must be"):
        SchemaMode.isolated(central="wreath_core", isolation="database")


def test_a_schema_name_that_is_not_a_string_is_refused() -> None:
    """The `not isinstance(value, str)` half of the identifier guard.

    The regex half was covered by the parametrised test above; this half was not, and
    a non-string reaching `fullmatch` raises `TypeError` -- the wrong error, naming
    neither the schema nor what was wrong with it.
    """
    for bad in (None, 3, b"app", ["app"]):
        with pytest.raises(DeclarationError, match="schema name"):
            SchemaMode.single(bad)


def test_a_schema_name_past_postgresqls_identifier_limit_is_refused() -> None:
    """The 63-byte ceiling, whose `raise` no test reached.

    PostgreSQL truncates a longer identifier rather than refusing it, so the schema
    wreath then addresses is not the one it created -- every qualified reference
    misses. 64 ASCII characters is the smallest refusal; 63 must still be accepted,
    or the bound could be tightened without any test noticing.
    """
    assert SchemaMode.single("a" * 63).schema == "a" * 63
    with pytest.raises(DeclarationError, match="63-byte"):
        SchemaMode.single("a" * 64)


def test_the_byte_length_check_cannot_disagree_with_the_character_count() -> None:
    """Why there is no multibyte arm above, which is worth stating rather than omitting.

    PostgreSQL's limit really is bytes, but the canonical unquoted-name grammar is
    ASCII, so every name that survives it encodes one byte per character. A
    multibyte name is refused by the grammar first and never reaches the length
    check. Asserting the refusal order keeps that a fact rather than an assumption:
    widen the grammar to allow non-ASCII and this test fails, which is exactly when
    the byte count would start to distinguish the spellings.
    """
    with pytest.raises(DeclarationError, match="plain SQL identifier"):
        SchemaMode.single("é" * 63)
    assert len(("é" * 63).encode("utf-8")) == 126


def test_making_a_table_index_unique_moves_the_model_fingerprint() -> None:
    """`b"ui\\x1f"` versus `b"i\\x1f"`, neither arm of which was pinned.

    The fingerprint is what migration drift detection compares, and the code three
    lines below this marker documents the same hazard for a partial index's
    predicate -- "without this a partial index could have its WHERE edited and no
    fingerprint would move". The `unique` flag had exactly that hole: a plain index
    changed to a unique one is a new constraint on the data, and if the fingerprint
    does not move, nothing tells anyone to build it.
    """
    from wreath.orm.table import index as table_index

    class Plain(Model, table="widgets", schema=CENTRAL_SCHEMA):
        id: Mapped[int] = column(Int64, primary_key=True)
        code: Mapped[int] = column(Int64)
        _by_code = table_index("code")

    class Uniquely(Model, table="widgets", schema=CENTRAL_SCHEMA):
        id: Mapped[int] = column(Int64, primary_key=True)
        code: Mapped[int] = column(Int64)
        _by_code = table_index("code", unique=True)

    def fingerprint_of(model: type) -> bytes:
        registry = Registry(
            FakeDatabase(), [model], validate_schema="off",
            schema_mode=SchemaMode.single("app"),
        )
        return registry.spec_for(model).fingerprint

    assert fingerprint_of(Plain) != fingerprint_of(Uniquely)


def test_qualified_name_drops_the_schema_only_for_a_search_path_tenant() -> None:
    """Both arms of `qualified_name`, which nothing distinguished.

    `test_isolated_schema_compiles_shared_unqualified_tenant_sql` covers the effect
    through `compile_select`, so the compiled SQL was pinned while the property that
    decides it was not -- forcing the branch either way left the suite green. The two
    answers are not interchangeable: a tenant table must be *unqualified* so one
    prepared statement serves every tenant through `search_path`, and a central table
    must stay qualified or it would resolve against whichever tenant is current.
    """
    registry = Registry(
        FakeDatabase(),
        [CentralAccount, TenantOrder],
        validate_schema="off",
        schema_mode=SchemaMode.isolated(central="wreath_core"),
    )
    tenant = registry.spec_for(TenantOrder)
    central = registry.spec_for(CentralAccount)

    assert tenant.sql_namespace == "tenant_search_path"
    assert tenant.qualified_name == "orders"
    assert central.sql_namespace != "tenant_search_path"
    assert central.qualified_name == "wreath_core.accounts"
