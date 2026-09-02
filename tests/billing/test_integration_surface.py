from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from wreath import Wreath
from wreath.agents import ChatApprovalFlow, PostgresApprovalStore
from wreath.authorization import AuthorizationDecision
from wreath.billing import (
    Billing,
    BillingCapabilities,
    BillingOperations,
    BillingSupport,
    DeploymentMerchant,
    HostedRedirect,
    MoneyMovementDisabled,
    PostgresBillingLedger,
    PostgresBillingQueries,
    PostgresSubscriptionEntitlements,
    StripeReconciliation,
    SupportMoneyMovement,
)
from wreath.chat import ChatOps
from wreath.metrics import collect
from wreath.subscriptions import Plan, PlanCatalog


class Backend:
    provider = "stripe"
    capabilities = BillingCapabilities(
        hosted_checkout=True,
        hosted_portal=True,
        subscriptions=True,
        refunds=True,
    )


class Jobs:
    def __init__(self) -> None:
        self.tasks: dict[str, Any] = {}

    def task(self, name: str, **options: Any) -> Any:
        def register(handler: Any) -> Any:
            self.tasks[name] = handler
            return handler

        return register

    async def enqueue(self, task: str, *args: Any, **options: Any) -> int:
        return 1


@asynccontextmanager
async def sessions() -> Any:
    yield object()


def configured() -> Billing:
    return Billing(
        "commerce",
        backend=Backend(),
        catalog=PlanCatalog(Plan("pro", "price_pro", frozenset({"export"}))),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
        ledger=PostgresBillingLedger(schema="commerce"),
    )


def test_billing_exposes_one_integrated_read_entitlement_operations_and_support_surface() -> None:
    billing = configured()
    queries = billing.queries(sessions)
    entitlements = billing.entitlements(queries, subject_for=lambda identity: identity.id)
    support = billing.support(
        reader=queries,
        subject_for=lambda identity, tenant: f"organization:{tenant}",
    )

    assert isinstance(queries, PostgresBillingQueries)
    assert isinstance(entitlements, PostgresSubscriptionEntitlements)
    assert isinstance(billing.operations, BillingOperations)
    assert billing.counter_sources == (billing.operations,)
    assert isinstance(support, BillingSupport)
    with pytest.raises(MoneyMovementDisabled, match="disabled by default"):
        _ = support.propose_refund


def test_application_metrics_discovers_registered_billing_operations() -> None:
    app = Wreath()
    billing = app.billing(
        "commerce",
        backend=Backend(),
        catalog=PlanCatalog(Plan("pro", "price_pro")),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
        ledger=PostgresBillingLedger(),
    )

    billing.operations.outcome_unknown()
    reading = next(item for item in collect(app) if item.subsystem == "billing")

    assert reading.instance == "commerce"
    assert reading.values["unknown_outcomes"] == 1


def test_application_discovers_the_durable_human_approval_schema() -> None:
    app = Wreath()
    billing = app.billing(
        "commerce",
        backend=Backend(),
        catalog=PlanCatalog(Plan("pro", "price_pro")),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
        ledger=PostgresBillingLedger(),
    )
    store = PostgresApprovalStore(sessions)
    approvals = ChatApprovalFlow(ChatOps(name="support"), store)

    async def authorize(context: Any, requirement: Any) -> AuthorizationDecision:
        return AuthorizationDecision(True, "cedar permit")

    async def audit(event: Any) -> None:
        return None

    billing.support(
        reader=billing.queries(sessions),
        subject_for=lambda identity, tenant: f"organization:{tenant}",
        money=SupportMoneyMovement(approvals, authorize, audit),
    )

    assert tuple(component.name for component in app.schema_components()) == (
        "billing-ledger",
        "agent-approvals",
    )


def test_stripe_reconciliation_uses_the_billing_ledger_for_projection_and_cursor_state() -> None:
    billing = configured()
    jobs = Jobs()
    assert billing.preflight() == (
        "bind the Stripe webhook projection to a durable webhook source",
        "configure durable Stripe reconciliation",
    )

    async def retrieve_page(**options: Any) -> Any:
        raise AssertionError("declaration performed provider I/O")

    reconciliation = billing.reconciliation(
        jobs=jobs,
        session_factory=sessions,
        retrieve_page=retrieve_page,
        cron=None,
    )

    assert isinstance(reconciliation, StripeReconciliation)
    assert reconciliation._ledger is billing.ledger
    assert reconciliation._state is billing.ledger
    assert reconciliation._operations is billing.operations
    assert set(jobs.tasks) == {"billing_commerce_reconcile"}
    assert billing.preflight() == (
        "bind the Stripe webhook projection to a durable webhook source",
    )
