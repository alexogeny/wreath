from __future__ import annotations

import pytest

from wreath.crud import Access
from wreath.platform import (
    BULK_CEILING,
    CONTENT_SECURITY_POLICY,
    PLATFORM_ACTIONS,
    PlatformAdmin,
    PlatformError,
    bulk,
    deprovision_tenant,
    impersonate,
    retry_dead_letter,
    suspend_tenant,
    tenant_overview,
)
from wreath.tenancy import InMemoryTenantDirectory, Tenant

ACME = Tenant(key="acme", schema="tenant_acme", role="tenant_acme")
GLOBEX = Tenant(key="globex", schema="tenant_globex", role="tenant_globex")
DIRECTORY = InMemoryTenantDirectory([ACME, GLOBEX])
ALLOWED = Access.roles("platform-operator")


def _admin(**kwargs) -> PlatformAdmin:
    return PlatformAdmin(directory=DIRECTORY, authorize=ALLOWED, **kwargs)


def _raises(_tenant) -> int:
    raise RuntimeError("the queue is down")


def test_building_the_console_without_an_authorizer_is_refused() -> None:
    with pytest.raises(PlatformError, match="authorize="):
        PlatformAdmin(directory=DIRECTORY)


def test_a_public_access_rule_is_refused_by_name() -> None:
    with pytest.raises(PlatformError, match="Access.public"):
        PlatformAdmin(directory=DIRECTORY, authorize=Access.public())


def test_the_action_vocabulary_is_disjoint_from_the_crud_one() -> None:
    crud_actions = {"list", "retrieve", "create", "update", "delete"}
    assert set(PLATFORM_ACTIONS).isdisjoint(crud_actions)
    assert all(action.startswith("platform:") for action in PLATFORM_ACTIONS)


def test_an_organisation_scoped_principal_is_refused_whatever_roles_it_holds() -> None:
    with pytest.raises(PlatformError, match="organisation-scoped"):
        _admin().require_operator({"organizations": ["acme"], "roles": ["admin"]})


def test_an_operator_principal_with_no_organisation_is_admitted() -> None:
    _admin().require_operator({"roles": ["platform-operator"]})


def test_the_overview_composes_the_sources_that_already_exist() -> None:
    rows = tenant_overview(
        [ACME, GLOBEX],
        sources={
            "migrations": lambda t: "current",
            "jobs": lambda t: 3,
            "passes": lambda t: 0,
            "quota": lambda t: 41.5,
        },
    )
    assert [row.key for row in rows] == ["acme", "globex"]
    assert rows[0].migration_state == "current"
    assert rows[0].dead_letters == 3
    assert rows[0].quota_used == 41.5


def test_a_source_that_raises_costs_its_column_rather_than_the_page() -> None:
    rows = tenant_overview([ACME], sources={"jobs": _raises, "quota": lambda t: 1.0})
    assert rows[0].unavailable == ("jobs",)
    assert rows[0].quota_used == 1.0


def test_an_unavailable_source_is_named_rather_than_shown_as_zero() -> None:
    rows = tenant_overview([ACME], sources={"jobs": _raises})
    assert "jobs" in rows[0].render_unavailable()
    assert "incomplete rather than low" in rows[0].render_unavailable()


def test_a_healthy_row_says_nothing_about_availability() -> None:
    rows = tenant_overview([ACME], sources={"jobs": lambda t: 0})
    assert rows[0].unavailable == ()
    assert rows[0].render_unavailable() == ""


def test_inspecting_a_tenant_binds_that_tenants_context() -> None:
    from wreath.tenancy import current_tenant

    with _admin().inspect("acme") as scope:
        assert scope.tenant.key == "acme"
        assert current_tenant().key == "acme"


def test_the_binding_is_released_when_the_inspection_ends() -> None:
    from wreath.tenancy import TenantNotBound, current_tenant

    with _admin().inspect("acme"):
        pass
    with pytest.raises(TenantNotBound):
        current_tenant()


def test_a_second_tenant_cannot_be_bound_inside_one_inspection() -> None:
    admin = _admin()
    with admin.inspect("acme") as scope, pytest.raises(PlatformError, match="one binding"):
        scope.bind_tenant("globex")


def test_inspecting_an_unknown_tenant_refuses() -> None:
    from wreath.tenancy import UnknownTenant

    with pytest.raises(UnknownTenant), _admin().inspect("ghost"):
        pass


def test_impersonation_cannot_exceed_what_the_user_held() -> None:
    delegated = impersonate(
        operator="ops-1", user="u-9", scope=("read", "write"), ttl=900, user_permissions=("read",)
    )
    assert delegated.permitted <= delegated.of_user
    assert delegated.permitted == frozenset({"read"})


def test_impersonating_a_user_who_holds_nothing_grants_nothing() -> None:
    delegated = impersonate(
        operator="ops-1", user="u-9", scope=("write",), ttl=60, user_permissions=()
    )
    assert delegated.permitted == frozenset()


def test_impersonation_has_no_default_ttl() -> None:
    with pytest.raises(PlatformError, match="ttl="):
        impersonate(operator="ops-1", user="u-9", scope=("read",))


def test_impersonation_has_no_default_scope() -> None:
    with pytest.raises(PlatformError, match="scope="):
        impersonate(operator="ops-1", user="u-9", ttl=900)


def test_impersonation_cannot_nest() -> None:
    with pytest.raises(PlatformError, match="already impersonating"):
        impersonate(operator="ops-1", user="u-9", scope=("read",), ttl=900, nested=True)


def test_impersonation_writes_an_audit_entry_in_the_same_transaction() -> None:
    delegated = impersonate(operator="ops-1", user="u-9", scope=("read",), ttl=900)
    assert delegated.audit_entry.actor == "ops-1"
    assert delegated.audit_entry.subject == "u-9"
    assert delegated.audit_entry.transactional is True


def test_an_impersonated_request_is_visibly_impersonated_to_a_policy() -> None:
    delegated = impersonate(operator="ops-1", user="u-9", scope=("read",), ttl=900)
    assert delegated.cedar_context()["impersonated_by"] == "ops-1"


def test_suspending_stops_queued_work_as_well_as_requests() -> None:
    result = suspend_tenant("acme", operator="ops-1", reason="abuse report 41")
    assert result.requests_refused and result.jobs_paused


def test_an_action_needs_an_operator_and_a_reason() -> None:
    with pytest.raises(PlatformError, match="operator="):
        suspend_tenant("acme", reason="abuse")
    with pytest.raises(PlatformError, match="reason="):
        suspend_tenant("acme", operator="ops-1")


def test_deprovisioning_requires_the_tenant_name_typed_back() -> None:
    with pytest.raises(PlatformError, match="irreversible"):
        deprovision_tenant("acme", confirm="", operator="ops-1", reason="churn")
    with pytest.raises(PlatformError, match="irreversible"):
        deprovision_tenant("acme", confirm="globex", operator="ops-1", reason="churn")
    assert deprovision_tenant("acme", confirm="acme", operator="ops-1", reason="churn") == "acme"


def test_retrying_a_dead_letter_goes_through_the_queue():
    seen: list[str] = []
    result = retry_dead_letter(job_id="j-1", requeue=seen.append)
    assert result["requeued"] is True
    assert result["in_process"] is False
    assert seen == ["j-1"]


def test_a_bulk_action_reports_per_key_rather_than_one_verdict() -> None:
    outcome = bulk("suspend", keys=("a", "b"), operator="ops-1", reason="abuse")
    assert set(outcome.per_key) == {"a", "b"}
    assert outcome.applied == ("a", "b")


def test_one_key_failing_does_not_stop_the_rest() -> None:
    def apply(key: str) -> str:
        if key == "b":
            raise RuntimeError("locked")
        return "applied"

    outcome = bulk("suspend", keys=("a", "b", "c"), operator="ops-1", reason="abuse", apply=apply)
    assert outcome.applied == ("a", "c")
    assert outcome.failed == ("b",)


def test_a_partially_applied_bulk_run_converges_when_re_run() -> None:
    done: set[str] = set()

    def apply(key: str) -> str:
        if key in done:
            return "skipped"
        done.add(key)
        return "applied"

    first = bulk("suspend", keys=("a", "b"), operator="ops-1", reason="x", apply=apply)
    second = bulk("suspend", keys=("a", "b"), operator="ops-1", reason="x", apply=apply)
    assert first.applied == ("a", "b")
    assert second.skipped == ("a", "b")


def test_a_bulk_action_over_the_ceiling_is_refused() -> None:
    keys = tuple(str(n) for n in range(BULK_CEILING + 1))
    with pytest.raises(PlatformError, match="ceiling"):
        bulk("suspend", keys=keys, operator="ops-1", reason="abuse")


def test_the_console_ships_no_javascript_so_its_csp_needs_no_nonce() -> None:
    assert "default-src 'none'" in CONTENT_SECURITY_POLICY
    assert "script-src" not in CONTENT_SECURITY_POLICY


def test_a_write_operation_requires_a_csrf_verifier() -> None:
    with pytest.raises(PlatformError, match="csrf="):
        _admin(operations=("list", "suspend")).router("/ops")


def test_a_read_only_console_needs_no_csrf_verifier() -> None:
    assert _admin().router("/ops")["operations"] == ("list", "inspect")


def test_constructing_the_console_registers_nothing_on_an_application() -> None:
    from wreath.app import Wreath

    app = Wreath()
    before = len(app._routes)
    _admin()
    assert len(app._routes) == before
