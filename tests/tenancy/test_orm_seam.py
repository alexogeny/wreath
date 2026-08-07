"""The declarative seam: `FromORM(tenant=FromTenant())`.

The single most load-bearing item on the tenancy checklist, because it is the
one that decides which spelling is the easy one. Before it, a tenant-bound
session could only be built by hand in a handler body, so `Annotated[Session,
FromORM("main")]` -- the spelling the guides teach -- produced an *unbound*
session against a tenant registry. The framework's own convenience was the
unsafe path.

Migrated from `tests/thesis/test_tenancy_contract.py`.
"""

from __future__ import annotations

import pytest

from wreath.orm import SchemaMode
from wreath.orm.registry import Registry
from wreath.orm.session import FromORM, Session, compile_session_binding
from wreath.tenancy import FromTenant, Tenant, TenantNotBound, tenant_scope

ACME = Tenant(key="acme", schema="tenant_acme", role="tenant_acme")


def _isolated() -> Registry:
    return Registry(None, (), schema_mode=SchemaMode.isolated(
        central="central", isolation="role"), validate_schema="off")


def _single() -> Registry:
    return Registry(None, (), schema_mode=SchemaMode.single("public"),
                    validate_schema="off")


def test_from_orm_carries_a_tenant_marker() -> None:
    assert FromORM("main", tenant=FromTenant()).tenant is not None


def test_a_bare_from_orm_against_an_isolated_registry_is_refused_at_compile() -> None:
    """At route-compile time, where a person is looking.

    `Session` would refuse it per request anyway -- at which point the
    application is already serving, and the first tenant request is a 500.
    """
    with pytest.raises(TypeError, match="tenant-isolated"):
        compile_session_binding({"main": _isolated()}, FromORM("main"))


def test_the_refusal_names_the_spelling_that_fixes_it() -> None:
    """A refusal that says only "needs a tenant" leaves the reader nowhere."""
    with pytest.raises(TypeError, match=r"FromORM\(tenant=FromTenant\(\)\)"):
        compile_session_binding({"main": _isolated()}, FromORM("main"))


def test_a_tenant_marker_against_an_isolated_registry_compiles() -> None:
    name, registry = compile_session_binding(
        {"main": _isolated()}, FromORM("main", tenant=FromTenant()))
    assert name == "main"
    assert registry.schema_mode.kind == "isolated"


def test_a_single_schema_registry_needs_no_tenant_marker() -> None:
    """The overwhelmingly common case must not have to say anything.

    A check that demanded a tenant everywhere would make every single-tenant
    application carry tenancy vocabulary it does not use, which is how the
    vocabulary gets ignored.
    """
    name, _ = compile_session_binding({"main": _single()}, FromORM("main"))
    assert name == "main"


def test_the_marker_resolves_the_bound_tenant_to_a_context() -> None:
    with tenant_scope(ACME):
        context = FromTenant().resolve(request=None)
    assert context.schema == "tenant_acme"
    assert context.role == "tenant_acme"


def test_the_marker_refuses_when_no_tenant_is_bound() -> None:
    """A session that exists without a tenant is the state this makes
    unreachable, so the refusal happens before the session is constructed."""
    with pytest.raises(TenantNotBound, match="no tenant is bound"):
        FromTenant().resolve(request=None)


def test_an_isolated_session_built_with_a_bound_tenant_carries_it() -> None:
    """End to end through the real `Session` constructor.

    `Session` already refused an isolated registry with no context; this is the
    other half -- that the context the marker produced is the one it accepts.
    """
    with tenant_scope(ACME):
        session = Session(_isolated(), "read", tenant=FromTenant().resolve(None))
    assert session._tenant.schema == "tenant_acme"
