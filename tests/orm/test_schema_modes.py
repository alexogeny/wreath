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


def test_isolated_session_execution_is_blocked_until_context_binder_lands() -> None:
    compiled = registry(SchemaMode.isolated(central="wreath_core", isolation="role"))

    with pytest.raises(SessionError, match="schema-context binder"):
        Session(compiled, "read")


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


def test_schema_mode_rejects_invalid_physical_identifiers() -> None:
    with pytest.raises(DeclarationError, match="schema name"):
        SchemaMode.single("not valid")
