"""The operator console: what it refuses, and what it will not omit.

Migrated from `tests/thesis/test_platform_admin_contract.py`. Most of these are
about the three defects the hand-written version of this console always has:
reading across tenants unbound, impersonating beyond the user, and destroying
without confirmation.
"""

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


# --- it is not the tenant-facing admin --------------------------------------


def test_building_the_console_without_an_authorizer_is_refused() -> None:
    """The refusal `scim_router` and `wreath.admin` already make.

    A console over every customer's data must not have a default that works.
    """
    with pytest.raises(PlatformError, match="authorize="):
        PlatformAdmin(directory=DIRECTORY)


def test_a_public_access_rule_is_refused_by_name() -> None:
    with pytest.raises(PlatformError, match="Access.public"):
        PlatformAdmin(directory=DIRECTORY, authorize=Access.public())


def test_the_action_vocabulary_is_disjoint_from_the_crud_one() -> None:
    """**A tenant admin must not reach this by holding `admin`.**

    Organisation roles are namespaced `<org>:<role>` precisely so an admin of
    one tenant is never an admin of another, and a console evaluated against
    that same vocabulary would put `acme:admin` one policy mistake away from
    every customer's data.
    """
    crud_actions = {"list", "retrieve", "create", "update", "delete"}
    assert set(PLATFORM_ACTIONS).isdisjoint(crud_actions)
    assert all(action.startswith("platform:") for action in PLATFORM_ACTIONS)


def test_an_organisation_scoped_principal_is_refused_whatever_roles_it_holds() -> None:
    """A principal carrying an organisation is a *customer's* principal."""
    with pytest.raises(PlatformError, match="organisation-scoped"):
        _admin().require_operator({"organizations": ["acme"], "roles": ["admin"]})


def test_an_operator_principal_with_no_organisation_is_admitted() -> None:
    """So the refusal above is not passing for free."""
    _admin().require_operator({"roles": ["platform-operator"]})


# --- the overview, composed -------------------------------------------------


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
    """`metrics.collect` already skips a subsystem that raises, for this reason.

    The console is most needed when something is broken; a page that refuses to
    render because the queue is down is missing at exactly the wrong moment.
    """
    rows = tenant_overview([ACME], sources={"jobs": _raises, "quota": lambda t: 1.0})
    assert rows[0].unavailable == ("jobs",)
    assert rows[0].quota_used == 1.0


def test_an_unavailable_source_is_named_rather_than_shown_as_zero() -> None:
    """**A gap must not read like a low number.**

    A row whose dead-letter count is missing because the queue is down looks
    identical to a healthy one unless the console says so, and an operator makes
    a decision on that -- the same reason `wreath doctor trace` prints what it
    did not search.
    """
    rows = tenant_overview([ACME], sources={"jobs": _raises})
    assert "jobs" in rows[0].render_unavailable()
    assert "incomplete rather than low" in rows[0].render_unavailable()


def test_a_healthy_row_says_nothing_about_availability() -> None:
    rows = tenant_overview([ACME], sources={"jobs": lambda t: 0})
    assert rows[0].unavailable == ()
    assert rows[0].render_unavailable() == ""


# --- reading a tenant's data ------------------------------------------------


def test_inspecting_a_tenant_binds_that_tenants_context() -> None:
    """**The console is not exempt from the boundary.**

    The obvious implementation queries with the application's own role and a
    schema interpolated from the URL, which is the cross-tenant read
    `wreath.tenancy` exists to prevent -- reintroduced by the tool built to
    supervise it.
    """
    from wreath.tenancy import current_tenant

    with _admin().inspect("acme") as scope:
        assert scope.tenant.key == "acme"
        assert current_tenant().key == "acme"


def test_the_binding_is_released_when_the_inspection_ends() -> None:
    """An operator console that leaves a tenant bound would have every later
    query in that task silently scoped to whichever customer was looked at last."""
    from wreath.tenancy import TenantNotBound, current_tenant

    with _admin().inspect("acme"):
        pass
    with pytest.raises(TenantNotBound):
        current_tenant()


def test_a_second_tenant_cannot_be_bound_inside_one_inspection() -> None:
    """One binding per transaction, so a join across two customers is
    unexpressible rather than merely discouraged."""
    admin = _admin()
    with admin.inspect("acme") as scope, pytest.raises(PlatformError, match="one binding"):
        scope.bind_tenant("globex")


def test_inspecting_an_unknown_tenant_refuses() -> None:
    from wreath.tenancy import UnknownTenant

    with pytest.raises(UnknownTenant), _admin().inspect("ghost"):
        pass


# --- impersonation ----------------------------------------------------------


def test_impersonation_cannot_exceed_what_the_user_held() -> None:
    """**The one law: composition never grants.**

    `principal.narrow` holds it for delegation generally; here it is arithmetic
    -- an intersection, never a union -- so no policy set can make it false.
    """
    delegated = impersonate(
        operator="ops-1", user="u-9", scope=("read", "write"), ttl=900,
        user_permissions=("read",))
    assert delegated.permitted <= delegated.of_user
    assert delegated.permitted == frozenset({"read"})


def test_impersonating_a_user_who_holds_nothing_grants_nothing() -> None:
    """The degenerate case, which is where a union would show."""
    delegated = impersonate(
        operator="ops-1", user="u-9", scope=("write",), ttl=60, user_permissions=())
    assert delegated.permitted == frozenset()


def test_impersonation_has_no_default_ttl() -> None:
    """A session that does not end is an account, not an impersonation."""
    with pytest.raises(PlatformError, match="ttl="):
        impersonate(operator="ops-1", user="u-9", scope=("read",))


def test_impersonation_has_no_default_scope() -> None:
    """`principal.narrow` refuses a defaulted scope for the same reason: an
    unscoped delegation is the user's whole authority handed over."""
    with pytest.raises(PlatformError, match="scope="):
        impersonate(operator="ops-1", user="u-9", ttl=900)


def test_impersonation_cannot_nest() -> None:
    """A chain whose effective permissions nobody can compute."""
    with pytest.raises(PlatformError, match="already impersonating"):
        impersonate(operator="ops-1", user="u-9", scope=("read",), ttl=900, nested=True)


def test_impersonation_writes_an_audit_entry_in_the_same_transaction() -> None:
    """Recorded by the grant, not beside it: an audit row that is a second
    statement is one a crash between them makes invisible."""
    delegated = impersonate(operator="ops-1", user="u-9", scope=("read",), ttl=900)
    assert delegated.audit_entry.actor == "ops-1"
    assert delegated.audit_entry.subject == "u-9"
    assert delegated.audit_entry.transactional is True


def test_an_impersonated_request_is_visibly_impersonated_to_a_policy() -> None:
    """Support reading an account is ordinary; support deleting one on the
    customer's behalf is not, and a policy can only tell them apart if the fact
    reaches it."""
    delegated = impersonate(operator="ops-1", user="u-9", scope=("read",), ttl=900)
    assert delegated.cedar_context()["impersonated_by"] == "ops-1"


# --- operator actions -------------------------------------------------------


def test_suspending_stops_queued_work_as_well_as_requests() -> None:
    """**Half a suspension is worse than none.**

    A tenant whose requests are refused while its jobs keep draining is one
    still sending email, still calling webhooks, still spending quota -- after
    somebody was told it was stopped.
    """
    result = suspend_tenant("acme", operator="ops-1", reason="abuse report 41")
    assert result.requests_refused and result.jobs_paused


def test_an_action_needs_an_operator_and_a_reason() -> None:
    with pytest.raises(PlatformError, match="operator="):
        suspend_tenant("acme", reason="abuse")
    with pytest.raises(PlatformError, match="reason="):
        suspend_tenant("acme", operator="ops-1")


def test_deprovisioning_requires_the_tenant_name_typed_back() -> None:
    """Typing the name is the only confirmation that survives becoming a habit;
    `privacy.erase` recomputes its plan and refuses on a moved digest for the
    same reason."""
    with pytest.raises(PlatformError, match="irreversible"):
        deprovision_tenant("acme", confirm="", operator="ops-1", reason="churn")
    with pytest.raises(PlatformError, match="irreversible"):
        deprovision_tenant("acme", confirm="globex", operator="ops-1", reason="churn")
    assert deprovision_tenant(
        "acme", confirm="acme", operator="ops-1", reason="churn") == "acme"


def test_retrying_a_dead_letter_goes_through_the_queue():
    """A console that re-runs the handler in-process loses the fence, the lease
    and the retry accounting, and the job's own recording never happens."""
    seen: list[str] = []
    result = retry_dead_letter(job_id="j-1", requeue=seen.append)
    assert result["requeued"] is True
    assert result["in_process"] is False
    assert seen == ["j-1"]


# --- bulk -------------------------------------------------------------------


def test_a_bulk_action_reports_per_key_rather_than_one_verdict() -> None:
    """The property `apply_fleet` already established: a run over a thousand
    rows has no atomic answer, and a shape that reports one has to lie about the
    400th."""
    outcome = bulk("suspend", keys=("a", "b"), operator="ops-1", reason="abuse")
    assert set(outcome.per_key) == {"a", "b"}
    assert outcome.applied == ("a", "b")


def test_one_key_failing_does_not_stop_the_rest() -> None:
    """Stopping would leave the remainder untouched with no record of which."""
    def apply(key: str) -> str:
        if key == "b":
            raise RuntimeError("locked")
        return "applied"

    outcome = bulk("suspend", keys=("a", "b", "c"), operator="ops-1", reason="abuse",
                   apply=apply)
    assert outcome.applied == ("a", "c")
    assert outcome.failed == ("b",)


def test_a_partially_applied_bulk_run_converges_when_re_run() -> None:
    """Skipped-rather-than-refused, so finishing a stopped run is re-running it."""
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
    """An unbounded bulk action is a console that can take the fleet down with
    one checkbox."""
    keys = tuple(str(n) for n in range(BULK_CEILING + 1))
    with pytest.raises(PlatformError, match="ceiling"):
        bulk("suspend", keys=keys, operator="ops-1", reason="abuse")


# --- the surface ------------------------------------------------------------


def test_the_console_ships_no_javascript_so_its_csp_needs_no_nonce() -> None:
    """`wreath.admin` already holds this; a second console that did not would be
    the deployment's weakest CSP."""
    assert "default-src 'none'" in CONTENT_SECURITY_POLICY
    assert "script-src" not in CONTENT_SECURITY_POLICY


def test_a_write_operation_requires_a_csrf_verifier() -> None:
    """`CSRFMiddleware` is header-only and an HTML form cannot carry a header.

    `wreath.admin` requires `csrf=` before generating a write; the console with
    the larger blast radius cannot require less.
    """
    with pytest.raises(PlatformError, match="csrf="):
        _admin(operations=("list", "suspend")).router("/ops")


def test_a_read_only_console_needs_no_csrf_verifier() -> None:
    """It generates no form, so there is nothing to protect."""
    assert _admin().router("/ops")["operations"] == ("list", "inspect")


def test_constructing_the_console_registers_nothing_on_an_application() -> None:
    """Three explicit steps, as the admin has: construct, register, include.

    Asserted as "construction is inert" rather than "a fresh app has no /ops
    route", because the second is true of an application that never heard of
    this module.
    """
    from wreath.app import Wreath

    app = Wreath()
    before = len(app._routes)
    _admin()
    assert len(app._routes) == before
