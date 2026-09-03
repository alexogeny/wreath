from __future__ import annotations

from typing import Any, cast

from wreath.app import Wreath
from wreath.doctor import preflight
from wreath.hardening import audit_source_for_tenancy
from wreath.orm import SchemaMode
from wreath.tenancy import (
    ROLE_ISOLATION_IMPLEMENTED,
    TENANCY_PREFLIGHT_SOURCE,
    InMemoryTenantDirectory,
    Tenancy,
    TenancyMiddleware,
    Tenant,
    TenantHeader,
    find_schema_literals,
)

ACME = Tenant(key="acme", schema="tenant_acme", role="tenant_acme")


def _isolated_app() -> Wreath:
    app = Wreath()
    app.postgres("main", dsn="postgresql://app@127.0.0.1:5432/app")
    app.orm(
        database="main",
        models=[],
        validate_schema="off",
        schema_mode=SchemaMode.isolated(central="central", isolation="role"),
    )
    return app


def test_preflight_blocks_a_tenant_registry_with_no_middleware() -> None:
    findings = [
        f for f in preflight(_isolated_app()).findings if f.source == TENANCY_PREFLIGHT_SOURCE
    ]
    assert len(findings) == 1
    assert findings[0].severity == "blocking"
    assert "TenancyMiddleware" in findings[0].detail


def test_preflight_is_quiet_once_the_middleware_is_installed() -> None:
    app = _isolated_app()
    app.add_global_middleware(
        cast(Any, TenancyMiddleware(
            Tenancy(
                directory=InMemoryTenantDirectory([ACME]),
                source=TenantHeader("X-Tenant", trusted=True),
            )
        ))
    )
    assert [f for f in preflight(app).findings if f.source == TENANCY_PREFLIGHT_SOURCE] == []


def test_preflight_says_nothing_about_a_single_schema_application() -> None:
    app = Wreath()
    app.postgres("main", dsn="postgresql://app@127.0.0.1:5432/app")
    app.orm(database="main", models=[], validate_schema="off")
    assert [f for f in preflight(app).findings if f.source == TENANCY_PREFLIGHT_SOURCE] == []


def test_a_tenant_schema_literal_is_found_in_application_source() -> None:
    findings = audit_source_for_tenancy(
        "rows = await session.raw(t'SELECT * FROM tenant_globex.item')"
    )
    assert [f.rule_id for f in findings] == ["tenant-schema-literal"]
    assert "tenant_globex" in findings[0].message


def test_the_finding_is_an_error_because_there_is_no_legitimate_form() -> None:
    findings = audit_source_for_tenancy("SELECT * FROM tenant_globex.item")
    assert findings[0].severity.value == "error"


def test_ordinary_unqualified_sql_produces_no_finding() -> None:
    assert audit_source_for_tenancy("SELECT * FROM item WHERE id = $1") == []


def test_the_central_schema_is_not_a_finding() -> None:
    assert audit_source_for_tenancy("SELECT * FROM central.plan") == []


def test_every_qualifying_verb_is_matched_not_only_select() -> None:
    for statement in (
        "SELECT * FROM tenant_a.x",
        "UPDATE tenant_b.x SET y = 1",
        "INSERT INTO tenant_c.x VALUES (1)",
        "SELECT * FROM x JOIN tenant_d.y ON true",
    ):
        assert len(find_schema_literals(statement)) == 1, statement


def test_role_isolation_is_no_longer_a_word_nothing_reads() -> None:
    assert ROLE_ISOLATION_IMPLEMENTED is True
    assert SchemaMode.isolated(central="c", isolation="role").isolation == "role"
