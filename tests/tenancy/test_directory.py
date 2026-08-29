from __future__ import annotations

import pytest

from wreath.tenancy import (
    InMemoryTenantDirectory,
    Tenancy,
    TenancyError,
    Tenant,
    TenantHeader,
    TenantHostLabel,
    TenantNotBound,
    TenantNotReady,
    TenantSessionClaim,
    TenantStatus,
    TenantSuspended,
    UnknownTenant,
    cedar_context,
    check_enqueue_tenant,
    current_tenant,
    telemetry_attributes,
    tenant_scope,
)


class _Request:
    """Just enough request for a source to read a name out of.

    `headers` is the **raw ASGI list of lowercase byte pairs** and `header()`
    is the accessor, because that is what a real `wreath.Request` is. An earlier
    version of this fake exposed a `dict`, so `TenantHeader` and
    `TenantHostLabel` were written against `headers.get(...)` -- which passed
    every test here and raised `AttributeError` on the first real request. A fake
    that is easier to use than the thing it stands in for tests itself.
    """

    def __init__(self, headers: dict[str, str] | None = None, session: dict | None = None):
        self.headers = [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ]
        self.state = type("State", (), {"session": session})()

    def header(self, name: str | bytes, default: str | None = None) -> str | None:
        wanted = (name.lower() if isinstance(name, str) else name.decode()).lower()
        for key, value in self.headers:
            if key.decode("latin-1") == wanted:
                return value.decode("latin-1")
        return default


ACME = Tenant(key="acme", schema="tenant_acme", role="tenant_acme")


def test_a_tenant_carries_identity_placement_and_lifecycle() -> None:
    assert ACME.key == "acme"
    assert ACME.status is TenantStatus.ACTIVE
    assert ACME.context().schema == "tenant_acme"


def test_a_tenant_key_that_is_not_an_identifier_is_refused_at_construction() -> None:
    with pytest.raises(TenancyError, match="character"):
        Tenant(key="acme; drop schema public", schema="x", role="x")


def test_a_tenant_schema_that_is_not_an_identifier_is_refused_too() -> None:
    with pytest.raises(TenancyError, match="character"):
        Tenant(key="acme", schema="public; drop table users", role="x")


def test_a_directory_lookup_that_misses_refuses_rather_than_falling_back() -> None:
    with pytest.raises(UnknownTenant, match="no default tenant"):
        InMemoryTenantDirectory([]).resolve("ghost")


def test_a_suspended_tenant_cannot_be_bound_at_all() -> None:
    suspended = Tenant(key="acme", schema="tenant_acme", status=TenantStatus.SUSPENDED)
    with pytest.raises(TenantSuspended, match="suspended"):
        suspended.require_bindable()


def test_a_tenant_that_has_not_been_migrated_is_not_servable() -> None:
    provisioning = Tenant(key="new", schema="tenant_new", status=TenantStatus.PROVISIONING)
    with pytest.raises(TenantNotReady, match="still provisioning"):
        provisioning.require_bindable()


def test_a_retired_tenant_is_refused_with_its_own_message() -> None:
    retired = Tenant(key="old", schema="tenant_old", status=TenantStatus.RETIRED)
    with pytest.raises(TenantNotReady, match="retired"):
        retired.require_bindable()


def test_tenant_resolution_has_no_default_source() -> None:
    with pytest.raises(TenancyError, match="source="):
        Tenancy(directory=InMemoryTenantDirectory([ACME]))


def test_a_resolved_name_is_looked_up_and_never_used_as_a_schema() -> None:
    directory = InMemoryTenantDirectory([Tenant(key="acme", schema="t_7f3a", role="r_7f3a")])
    tenancy = Tenancy(directory=directory, source=TenantHeader("X-Tenant"))
    assert tenancy.resolve_name("acme").schema == "t_7f3a"


def test_a_request_naming_no_tenant_is_refused_naming_the_source() -> None:
    tenancy = Tenancy(directory=InMemoryTenantDirectory([ACME]), source=TenantHeader("X-Tenant"))
    with pytest.raises(UnknownTenant, match="X-Tenant"):
        tenancy.resolve_request(_Request())


def test_the_host_source_requires_its_suffix_and_refuses_the_apex() -> None:
    tenancy = Tenancy(
        directory=InMemoryTenantDirectory([ACME]), source=TenantHostLabel("example.com")
    )
    assert tenancy.resolve_request(_Request({"host": "acme.example.com"})).key == "acme"
    with pytest.raises(UnknownTenant):
        tenancy.resolve_request(_Request({"host": "example.com"}))


def test_the_host_source_ignores_a_port_and_is_case_insensitive() -> None:
    tenancy = Tenancy(
        directory=InMemoryTenantDirectory([ACME]), source=TenantHostLabel("example.com")
    )
    assert tenancy.resolve_request(_Request({"host": "ACME.example.com:8443"})).key == "acme"


def test_the_host_source_refuses_a_lookalike_suffix() -> None:
    tenancy = Tenancy(
        directory=InMemoryTenantDirectory([ACME]), source=TenantHostLabel("example.com")
    )
    with pytest.raises(UnknownTenant):
        tenancy.resolve_request(_Request({"host": "acme.example.com.evil.test"}))


def test_the_session_source_reads_state_the_server_wrote() -> None:
    tenancy = Tenancy(
        directory=InMemoryTenantDirectory([ACME]), source=TenantSessionClaim("tenant")
    )
    assert tenancy.resolve_request(_Request(session={"tenant": "acme"})).key == "acme"


def test_resolution_checks_status_as_well_as_existence() -> None:
    directory = InMemoryTenantDirectory(
        [Tenant(key="acme", schema="tenant_acme", status=TenantStatus.SUSPENDED)]
    )
    tenancy = Tenancy(directory=directory, source=TenantHeader("X-Tenant"))
    with pytest.raises(TenantSuspended):
        tenancy.resolve_request(_Request({"X-Tenant": "acme"}))


def test_nothing_is_bound_outside_a_scope() -> None:
    with pytest.raises(TenantNotBound):
        current_tenant()


def test_a_scope_binds_and_restores_rather_than_clearing() -> None:
    other = Tenant(key="globex", schema="tenant_globex")
    with tenant_scope(ACME):
        with tenant_scope(other):
            assert current_tenant().key == "globex"
        assert current_tenant().key == "acme"


def test_a_scope_refuses_a_tenant_that_may_not_serve() -> None:
    suspended = Tenant(key="acme", schema="tenant_acme", status=TenantStatus.SUSPENDED)
    with pytest.raises(TenantSuspended), tenant_scope(suspended):
        pass  # pragma: no cover - the scope refuses before the body runs


def test_a_scope_given_a_name_needs_a_directory_to_resolve_it_in() -> None:
    with pytest.raises(TenancyError, match="directory"), tenant_scope("acme"):
        pass  # pragma: no cover - refused before the body runs


def test_the_cedar_context_carries_the_tenant_separately_from_organisations() -> None:
    with tenant_scope(ACME):
        assert cedar_context() == {"tenant": "acme"}
    assert cedar_context() == {}


def test_telemetry_attributes_carry_the_tenant() -> None:
    with tenant_scope(ACME):
        assert telemetry_attributes() == {"tenant": "acme"}


def test_work_enqueued_inside_a_scope_inherits_that_tenant() -> None:
    with tenant_scope(ACME):
        assert check_enqueue_tenant(None) == "acme"
        assert check_enqueue_tenant("") == "acme"


def test_an_explicit_tenant_matching_the_scope_is_accepted() -> None:
    with tenant_scope(ACME):
        assert check_enqueue_tenant("acme") == "acme"


def test_an_explicit_tenant_contradicting_the_scope_is_refused() -> None:
    with tenant_scope(ACME), pytest.raises(TenancyError, match="cannot tell which"):
        check_enqueue_tenant("globex")


def test_outside_a_scope_an_explicit_tenant_is_passed_through() -> None:
    assert check_enqueue_tenant("globex") == "globex"
    assert check_enqueue_tenant(None) == ""
