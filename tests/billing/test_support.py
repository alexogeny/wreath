from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from wreath._agents.approvals import InMemoryApprovalStore
from wreath._agents.chat_approvals import ChatApprovalFlow
from wreath.auth import Identity
from wreath.authorization import AuthorizationDecision, human
from wreath.billing.queries import InvoicePage
from wreath.billing.support import (
    BillingAuditEvent,
    BillingSupport,
    MoneyMovementDisabled,
    SupportAccess,
    SupportAccessDisabled,
    SupportMoneyMovement,
)
from wreath.chat import AgentRequest, ChatContext, ChatCorrelation, ChatOps
from wreath.payments import Money, PaymentSnapshot, PaymentState, Refund, RefundState
from wreath.subscriptions import SubscriptionSnapshot, SubscriptionState


def context(*, permissions: frozenset[str] = frozenset({"billing.refund"})) -> ChatContext:
    identity = Identity(
        "user-7",
        permissions=permissions,
        claims={"second_factor_at": 100.0},
    )
    return ChatContext(
        provider="slack",
        installation="install-1",
        tenant="acme",
        actor="U7",
        conversation="C1",
        delivery_id="D1",
        native={},
        identity=identity,
        principal=human(identity),
    )


@dataclass
class Reader:
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def subscription(self, subject: str) -> SubscriptionSnapshot | None:
        self.calls.append((subject, "subscription"))
        return SubscriptionSnapshot(
            "stripe",
            "sub_1",
            subject,
            "pro",
            SubscriptionState.ACTIVE,
            "active",
        )

    async def payment(self, subject: str, payment: str) -> PaymentSnapshot | None:
        self.calls.append((subject, payment))
        return PaymentSnapshot(
            "stripe",
            payment,
            subject,
            "order-41",
            Money("USD", 2_900),
            PaymentState.SUCCEEDED,
            merchant_account="acct_acme",
        )

    async def invoices(self, subject: str, **options: Any) -> InvoicePage:
        self.calls.append((subject, "invoices"))
        return InvoicePage((), None)

    async def command(self, subject: str, operation: str, key: str) -> None:
        self.calls.append((subject, f"{operation}:{key}"))


@dataclass
class Backend:
    refunds: list[dict[str, Any]] = field(default_factory=list)

    async def _refund_projected(
        self,
        payment: PaymentSnapshot,
        **options: Any,
    ) -> Refund:
        options["subject"] = payment.subject
        options["payment"] = payment.id
        self.refunds.append(options)
        return Refund("stripe", "re_1", RefundState.SUCCEEDED, options["amount"])


def test_money_movement_is_absent_by_default() -> None:
    support = BillingSupport(
        billing=Backend(),
        reader=Reader(),
        subject_for=lambda identity, tenant: f"organization:{tenant}",
    )

    with pytest.raises(MoneyMovementDisabled, match="disabled by default"):
        _ = support.propose_refund

    with pytest.raises(SupportAccessDisabled, match="disabled by default"):
        _ = support.payment


async def test_support_reads_are_subject_scoped_and_require_permission_and_cedar() -> None:
    authorized: list[Any] = []

    async def authorize(current: Any, requirement: Any) -> AuthorizationDecision:
        authorized.append(requirement)
        return AuthorizationDecision(True, "cedar permit")

    reader = Reader()
    support = BillingSupport(
        billing=Backend(),
        reader=reader,
        subject_for=lambda identity, tenant: f"organization:{tenant}",
        access=SupportAccess(authorize),
    )

    current = context(permissions=frozenset({"billing.read"}))
    payment = await support.payment(current, "pi_1")

    assert payment is not None
    assert payment.subject == "organization:acme"
    subscription = await support.subscription(current)
    invoices = await support.invoices(current, limit=10)
    command = await support.command(current, "refund", "support-refund-1")

    assert subscription is not None
    assert subscription.subject == "organization:acme"
    assert invoices == InvoicePage((), None)
    assert command is None
    assert reader.calls == [
        ("organization:acme", "pi_1"),
        ("organization:acme", "subscription"),
        ("organization:acme", "invoices"),
        ("organization:acme", "refund:support-refund-1"),
    ]
    assert authorized[0].action == "billing.read"
    assert "organization:acme" in authorized[0].resource.id

    with pytest.raises(PermissionError, match="billing.read permission"):
        await support.payment(context(permissions=frozenset()), "pi_1")


async def test_refund_executes_only_from_the_human_approval_callback() -> None:
    chat = ChatOps(name="support")
    store = InMemoryApprovalStore(max_entries=8, clock=lambda: 100.0)
    approvals = ChatApprovalFlow(chat, store)
    authorized: list[tuple[Any, Any]] = []
    audited: list[BillingAuditEvent] = []

    async def authorize(current: Any, requirement: Any) -> AuthorizationDecision:
        authorized.append((current, requirement))
        return AuthorizationDecision(True, "cedar permit")

    async def audit(event: BillingAuditEvent) -> None:
        audited.append(event)

    backend = Backend()
    support = BillingSupport(
        billing=backend,
        reader=Reader(),
        subject_for=lambda identity, tenant: f"organization:{tenant}",
        money=SupportMoneyMovement(
            approvals=approvals,
            authorize=authorize,
            audit=audit,
            ttl=60,
        ),
    )
    current = context()

    proposal = await support.propose_refund(
        current,
        payment="pi_1",
        reference="support-refund-41",
        amount=Money("USD", 100),
    )

    assert backend.refunds == []
    assert proposal.visibility == "ephemeral"
    action = proposal.blocks[1]["elements"][0]["action_id"]
    current.action = action
    reply = await chat._dispatch(kind="action", name=action, context=current)

    assert reply is not None
    assert "re_1" in reply.content
    assert backend.refunds == [
        {
            "subject": "organization:acme",
            "payment": "pi_1",
            "reference": "support-refund-41",
            "amount": Money("USD", 100),
        }
    ]
    assert authorized[0][1].action == "billing.refund"
    assert audited == [
        BillingAuditEvent(
            action="billing.refund",
            actor="user-7",
            subject="organization:acme",
            resource=authorized[0][1].resource.id,
            approval_id=action.rsplit(":", 1)[-1],
        )
    ]


async def test_refund_requires_permission_and_cedar_after_human_approval() -> None:
    chat = ChatOps(name="support")
    approvals = ChatApprovalFlow(
        chat,
        InMemoryApprovalStore(max_entries=8, clock=lambda: 100.0),
    )

    async def allow(current: Any, requirement: Any) -> AuthorizationDecision:
        return AuthorizationDecision(True, "cedar permit")

    async def audit(event: BillingAuditEvent) -> None:
        raise AssertionError("denied refund reached audit")

    backend = Backend()
    support = BillingSupport(
        billing=backend,
        reader=Reader(),
        subject_for=lambda identity, tenant: f"organization:{tenant}",
        money=SupportMoneyMovement(approvals, allow, audit),
    )
    current = context()
    proposal = await support.propose_refund(
        current,
        payment="pi_1",
        reference="support-refund-42",
        amount=Money("USD", 100),
    )
    action = proposal.blocks[1]["elements"][0]["action_id"]
    current.identity = Identity(
        "user-7",
        permissions=frozenset(),
        claims={"second_factor_at": 100.0},
    )
    current.principal = human(current.identity)
    current.action = action

    reply = await chat._dispatch(kind="action", name=action, context=current)

    assert reply is None
    assert backend.refunds == []


async def test_refund_requires_cedar_permit_after_human_approval() -> None:
    chat = ChatOps(name="support")
    approvals = ChatApprovalFlow(
        chat,
        InMemoryApprovalStore(max_entries=8, clock=lambda: 100.0),
    )
    audited: list[BillingAuditEvent] = []

    decisions = iter(
        (
            AuthorizationDecision(True, "cedar permit"),
            AuthorizationDecision(False, "no permit policy matched"),
        )
    )

    async def deny_after_proposal(
        current: Any, requirement: Any
    ) -> AuthorizationDecision:
        return next(decisions)

    async def audit(event: BillingAuditEvent) -> None:
        audited.append(event)

    backend = Backend()
    support = BillingSupport(
        billing=backend,
        reader=Reader(),
        subject_for=lambda identity, tenant: f"organization:{tenant}",
        money=SupportMoneyMovement(approvals, deny_after_proposal, audit),
    )
    current = context()
    proposal = await support.propose_refund(
        current,
        payment="pi_1",
        reference="support-refund-43",
        amount=Money("USD", 100),
    )
    action = proposal.blocks[1]["elements"][0]["action_id"]
    current.action = action

    reply = await chat._dispatch(kind="action", name=action, context=current)

    assert reply is None
    assert audited == []
    assert backend.refunds == []


async def test_agent_context_cannot_execute_a_human_refund_approval() -> None:
    chat = ChatOps(name="support")
    approvals = ChatApprovalFlow(
        chat,
        InMemoryApprovalStore(max_entries=8, clock=lambda: 100.0),
    )

    async def authorize(current: Any, requirement: Any) -> AuthorizationDecision:
        return AuthorizationDecision(True, "cedar permit")

    audited: list[BillingAuditEvent] = []

    async def audit(event: BillingAuditEvent) -> None:
        audited.append(event)

    backend = Backend()
    support = BillingSupport(
        billing=backend,
        reader=Reader(),
        subject_for=lambda identity, tenant: f"organization:{tenant}",
        money=SupportMoneyMovement(approvals, authorize, audit),
    )
    current = context()
    proposal = await support.propose_refund(
        current,
        payment="pi_1",
        reference="support-refund-agent",
        amount=Money("USD", 100),
    )
    action = proposal.blocks[1]["elements"][0]["action_id"]
    current.action = action
    current.agent_request = AgentRequest(
        tenant="acme",
        actor="agent-1",
        conversation="C1",
        prompt="refund this payment",
        correlation=ChatCorrelation(interaction_id="D1"),
        principal=current.principal,
    )

    reply = await chat._dispatch(kind="action", name=action, context=current)

    assert reply is None
    assert audited == []
    assert backend.refunds == []


async def test_durable_approval_resource_executes_on_another_support_worker() -> None:
    store = InMemoryApprovalStore(max_entries=8, clock=lambda: 100.0)
    proposing_chat = ChatOps(name="proposing")
    executing_chat = ChatOps(name="executing")
    proposing_approvals = ChatApprovalFlow(proposing_chat, store)
    executing_approvals = ChatApprovalFlow(executing_chat, store)
    audited: list[BillingAuditEvent] = []

    async def authorize(current: Any, requirement: Any) -> AuthorizationDecision:
        return AuthorizationDecision(True, "cedar permit")

    async def audit(event: BillingAuditEvent) -> None:
        audited.append(event)

    backend = Backend()
    proposer = BillingSupport(
        billing=backend,
        reader=Reader(),
        subject_for=lambda identity, tenant: f"organization:{tenant}",
        money=SupportMoneyMovement(proposing_approvals, authorize, audit),
    )
    BillingSupport(
        billing=backend,
        reader=Reader(),
        subject_for=lambda identity, tenant: f"organization:{tenant}",
        money=SupportMoneyMovement(executing_approvals, authorize, audit),
    )
    current = context()
    proposal = await proposer.propose_refund(
        current,
        payment="pi_1",
        reference="support-refund-other-worker",
        amount=Money("USD", 100),
    )
    action = proposal.blocks[1]["elements"][0]["action_id"]
    current.action = action

    reply = await executing_chat._dispatch(kind="action", name=action, context=current)

    assert reply is not None
    assert len(audited) == 1
    assert backend.refunds[0]["reference"] == "support-refund-other-worker"
