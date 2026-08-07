"""The two checks that catch the wiring omission before a request does.

Migrated from `tests/thesis/test_tenancy_contract.py`.
"""

from __future__ import annotations

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
    app.orm(database="main", models=[], validate_schema="off",
            schema_mode=SchemaMode.isolated(central="central", isolation="role"))
    return app


# --- preflight --------------------------------------------------------------


def test_preflight_blocks_a_tenant_registry_with_no_middleware() -> None:
    """The omission where every piece is configured and nothing joins them.

    The application starts and serves every request unbound, which is why it
    blocks rather than advising: there is no reading of it that is fine.
    """
    findings = [f for f in preflight(_isolated_app()).findings
                if f.source == TENANCY_PREFLIGHT_SOURCE]
    assert len(findings) == 1
    assert findings[0].severity == "blocking"
    assert "TenancyMiddleware" in findings[0].detail


def test_preflight_is_quiet_once_the_middleware_is_installed() -> None:
    """Otherwise the finding is one everybody learns to ignore."""
    app = _isolated_app()
    app.add_global_middleware(TenancyMiddleware(
        Tenancy(directory=InMemoryTenantDirectory([ACME]),
                source=TenantHeader("X-Tenant"))))
    assert [f for f in preflight(app).findings
            if f.source == TENANCY_PREFLIGHT_SOURCE] == []


def test_preflight_says_nothing_about_a_single_schema_application() -> None:
    """Most applications are not multi-tenant and must hear nothing about it."""
    app = Wreath()
    app.postgres("main", dsn="postgresql://app@127.0.0.1:5432/app")
    app.orm(database="main", models=[], validate_schema="off")
    assert [f for f in preflight(app).findings
            if f.source == TENANCY_PREFLIGHT_SOURCE] == []


# --- the source rule --------------------------------------------------------


def test_a_tenant_schema_literal_is_found_in_application_source() -> None:
    """The one shape that walks past a search path: an already-qualified name.

    The GRANTs make it fail closed -- in production. This makes it fail early,
    where the person who wrote it is still looking at it.
    """
    findings = audit_source_for_tenancy(
        "rows = await session.raw(t'SELECT * FROM tenant_globex.item')")
    assert [f.rule_id for f in findings] == ["tenant-schema-literal"]
    assert "tenant_globex" in findings[0].message


def test_the_finding_is_an_error_because_there_is_no_legitimate_form() -> None:
    """A tenant's own tables are what the search path resolves, and the central
    schema is named by its own name. Naming a *tenant* schema has no correct
    version, so it is not a judgement call and does not warn."""
    findings = audit_source_for_tenancy("SELECT * FROM tenant_globex.item")
    assert findings[0].severity.value == "error"


def test_ordinary_unqualified_sql_produces_no_finding() -> None:
    """A rule that fires on correct code is a rule that gets switched off."""
    assert audit_source_for_tenancy("SELECT * FROM item WHERE id = $1") == []


def test_the_central_schema_is_not_a_finding() -> None:
    """Naming the shared schema is exactly what an application is meant to do."""
    assert audit_source_for_tenancy("SELECT * FROM central.plan") == []


def test_every_qualifying_verb_is_matched_not_only_select() -> None:
    """`UPDATE tenant_x.item` crosses just as `FROM` does, and a rule that read
    only `FROM` would report the read and miss the write."""
    for statement in ("SELECT * FROM tenant_a.x", "UPDATE tenant_b.x SET y = 1",
                      "INSERT INTO tenant_c.x VALUES (1)",
                      "SELECT * FROM x JOIN tenant_d.y ON true"):
        assert len(find_schema_literals(statement)) == 1, statement


# --- the declaration that used to mean nothing ------------------------------


def test_role_isolation_is_no_longer_a_word_nothing_reads() -> None:
    """`SchemaMode.isolated(isolation="role")` accepted `"role"` while the word
    `isolation` appeared nowhere else in the tree. This module is what makes the
    knob mean something."""
    assert ROLE_ISOLATION_IMPLEMENTED is True
    assert SchemaMode.isolated(central="c", isolation="role").isolation == "role"
