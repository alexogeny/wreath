"""The directory and the resolution in front of it.

Migrated from `tests/thesis/test_tenancy_contract.py` as `wreath.tenancy`
landed: the contracts are unchanged, the setup is now real.
"""

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
    """`TenantContext` is placement alone, so status has nowhere else to live."""
    assert ACME.key == "acme"
    assert ACME.status is TenantStatus.ACTIVE
    assert ACME.context().schema == "tenant_acme"


def test_a_tenant_key_that_is_not_an_identifier_is_refused_at_construction() -> None:
    """The key reaches SQL as a schema and a role name.

    Refused where the row is written, not where it is interpolated: a directory
    row is written once and read on every request.
    """
    with pytest.raises(TenancyError, match="character"):
        Tenant(key="acme; drop schema public", schema="x", role="x")


def test_a_tenant_schema_that_is_not_an_identifier_is_refused_too() -> None:
    """Separately from the key, because they are separate fields.

    A check that validated only the key would pass a directory row whose schema
    somebody typed by hand.
    """
    with pytest.raises(TenancyError, match="character"):
        Tenant(key="acme", schema="public; drop table users", role="x")


def test_a_directory_lookup_that_misses_refuses_rather_than_falling_back() -> None:
    """There is no default tenant, and the message says why."""
    with pytest.raises(UnknownTenant, match="no default tenant"):
        InMemoryTenantDirectory([]).resolve("ghost")


def test_a_suspended_tenant_cannot_be_bound_at_all() -> None:
    """Enforced at the bind, not at the route.

    On a route it is one forgotten decorator away from not happening.
    """
    suspended = Tenant(key="acme", schema="tenant_acme", status=TenantStatus.SUSPENDED)
    with pytest.raises(TenantSuspended, match="suspended"):
        suspended.require_bindable()


def test_a_tenant_that_has_not_been_migrated_is_not_servable() -> None:
    """`PROVISIONING` is a state, not a synonym for active.

    Asserting the distinct message rather than only the type: both refusals
    below name the tenant, so a test that checked the name would pass on
    whichever branch fired.
    """
    provisioning = Tenant(key="new", schema="tenant_new", status=TenantStatus.PROVISIONING)
    with pytest.raises(TenantNotReady, match="still provisioning"):
        provisioning.require_bindable()


def test_a_retired_tenant_is_refused_with_its_own_message() -> None:
    retired = Tenant(key="old", schema="tenant_old", status=TenantStatus.RETIRED)
    with pytest.raises(TenantNotReady, match="retired"):
        retired.require_bindable()


# --- resolution -------------------------------------------------------------


def test_tenant_resolution_has_no_default_source() -> None:
    """Where the name comes from is a deployment decision, never a guess."""
    with pytest.raises(TenancyError, match="source="):
        Tenancy(directory=InMemoryTenantDirectory([ACME]))


def test_a_resolved_name_is_looked_up_and_never_used_as_a_schema() -> None:
    """The header names a *tenant*; the directory names a *schema*.

    Collapsing the two is the whole vulnerability, so the fixture deliberately
    gives the tenant a schema that is nothing like its key: a resolution that
    returned `tenant_acme` here would be one that derived rather than looked up.
    """
    directory = InMemoryTenantDirectory([Tenant(key="acme", schema="t_7f3a", role="r_7f3a")])
    tenancy = Tenancy(directory=directory, source=TenantHeader("X-Tenant"))
    assert tenancy.resolve_name("acme").schema == "t_7f3a"


def test_a_request_naming_no_tenant_is_refused_naming_the_source() -> None:
    """So the operator reading the 400 knows where it was looked for."""
    tenancy = Tenancy(directory=InMemoryTenantDirectory([ACME]),
                      source=TenantHeader("X-Tenant"))
    with pytest.raises(UnknownTenant, match="X-Tenant"):
        tenancy.resolve_request(_Request())


def test_the_host_source_requires_its_suffix_and_refuses_the_apex() -> None:
    """`www` must not become a customer, and the apex must not resolve at all."""
    tenancy = Tenancy(directory=InMemoryTenantDirectory([ACME]),
                      source=TenantHostLabel("example.com"))
    assert tenancy.resolve_request(_Request({"host": "acme.example.com"})).key == "acme"
    with pytest.raises(UnknownTenant):
        tenancy.resolve_request(_Request({"host": "example.com"}))


def test_the_host_source_ignores_a_port_and_is_case_insensitive() -> None:
    """A `Host` header carries the port a browser connected on."""
    tenancy = Tenancy(directory=InMemoryTenantDirectory([ACME]),
                      source=TenantHostLabel("example.com"))
    assert tenancy.resolve_request(_Request({"host": "ACME.example.com:8443"})).key == "acme"


def test_the_host_source_refuses_a_lookalike_suffix() -> None:
    """`acme.example.com.evil.test` ends in the suffix as a substring and is not
    under it. Matching without the leading dot is how that becomes a tenant."""
    tenancy = Tenancy(directory=InMemoryTenantDirectory([ACME]),
                      source=TenantHostLabel("example.com"))
    with pytest.raises(UnknownTenant):
        tenancy.resolve_request(_Request({"host": "acme.example.com.evil.test"}))


def test_the_session_source_reads_state_the_server_wrote() -> None:
    """The strongest source: the caller cannot name a tenant at all."""
    tenancy = Tenancy(directory=InMemoryTenantDirectory([ACME]),
                      source=TenantSessionClaim("tenant"))
    assert tenancy.resolve_request(_Request(session={"tenant": "acme"})).key == "acme"


def test_resolution_checks_status_as_well_as_existence() -> None:
    """One call, both questions -- a resolve that returned a suspended tenant
    would put the status check back on the caller, which is where it gets
    forgotten."""
    directory = InMemoryTenantDirectory([
        Tenant(key="acme", schema="tenant_acme", status=TenantStatus.SUSPENDED)])
    tenancy = Tenancy(directory=directory, source=TenantHeader("X-Tenant"))
    with pytest.raises(TenantSuspended):
        tenancy.resolve_request(_Request({"X-Tenant": "acme"}))


# --- the ambient binding ----------------------------------------------------


def test_nothing_is_bound_outside_a_scope() -> None:
    with pytest.raises(TenantNotBound):
        current_tenant()


def test_a_scope_binds_and_restores_rather_than_clearing() -> None:
    """A nested scope must not unbind its caller's on exit.

    Clearing instead of restoring is invisible until the first background task
    that opens an inner scope, and then the outer work runs untenanted.
    """
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
    """An organisation is who you act as; a tenant is where the rows live.

    A deployment can have either without the other, so conflating them would be
    right until the first customer with two organisations in one database.
    """
    with tenant_scope(ACME):
        assert cedar_context() == {"tenant": "acme"}
    assert cedar_context() == {}


def test_telemetry_attributes_carry_the_tenant() -> None:
    """"Which tenant is slow" is the first question of every incident."""
    with tenant_scope(ACME):
        assert telemetry_attributes() == {"tenant": "acme"}


# --- propagation ------------------------------------------------------------


def test_work_enqueued_inside_a_scope_inherits_that_tenant() -> None:
    """`enqueue(tenant="")` defaults to empty, and an empty tenant on a worker
    hours later reads the wrong schema with no request left to attribute it to."""
    with tenant_scope(ACME):
        assert check_enqueue_tenant(None) == "acme"
        assert check_enqueue_tenant("") == "acme"


def test_an_explicit_tenant_matching_the_scope_is_accepted() -> None:
    with tenant_scope(ACME):
        assert check_enqueue_tenant("acme") == "acme"


def test_an_explicit_tenant_contradicting_the_scope_is_refused() -> None:
    """One of the two spellings is a bug and this cannot tell which, so it
    refuses rather than resolving it silently in either direction."""
    with tenant_scope(ACME), pytest.raises(TenancyError, match="cannot tell which"):
        check_enqueue_tenant("globex")


def test_outside_a_scope_an_explicit_tenant_is_passed_through() -> None:
    """A worker enqueuing for a tenant it was told about is ordinary."""
    assert check_enqueue_tenant("globex") == "globex"
    assert check_enqueue_tenant(None) == ""
