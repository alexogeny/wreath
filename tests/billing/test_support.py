from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from wreath._agents.approvals import ApprovalGrant, InMemoryApprovalStore
from wreath._agents.chat_approvals import ChatApprovalFlow
from wreath.auth import Identity
from wreath.authorization import AuthorizationDecision, human
from wreath.billing.ledger import (
    BillingCommand,
    BillingCommandIdentity,
    BillingCommandState,
)
from wreath.billing.queries import InvoicePage
from wreath.billing.support import (
    BillingAuditEvent,
    BillingSupport,
    MoneyMovementDisabled,
    SupportAccess,
    SupportAccessDisabled,
    SupportMoneyMovement,
    _intent_from_grant,
)
from wreath.chat import AgentRequest, ChatContext, ChatCorrelation, ChatOps
from wreath.payments import Money, PaymentSnapshot, PaymentState, Refund, RefundState
from wreath.subscriptions import (
    SubscriptionPayment,
    SubscriptionSnapshot,
    SubscriptionState,
)


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


@dataclass
class ResultReader:
    payment_result: Any = None
    subscription_result: Any = None
    invoice_result: Any = field(default_factory=lambda: InvoicePage((), None))
    command_result: Any = None

    async def payment(self, subject: str, payment: str) -> Any:
        return self.payment_result

    async def subscription(self, subject: str) -> Any:
        return self.subscription_result

    async def invoices(self, subject: str, **options: Any) -> Any:
        return self.invoice_result

    async def command(self, subject: str, operation: str, key: str) -> Any:
        return self.command_result


def readable_support(reader: Any) -> BillingSupport:
    async def authorize(current: Any, requirement: Any) -> AuthorizationDecision:
        return AuthorizationDecision(True, "permit")

    return BillingSupport(
        billing=Backend(),
        reader=reader,
        subject_for=lambda identity, tenant: f"organization:{tenant}",
        access=SupportAccess(authorize),
    )


def test_support_read_properties_refuse_disabled_access() -> None:
    support = BillingSupport(
        billing=Backend(),
        reader=Reader(),
        subject_for=lambda identity, tenant: f"organization:{tenant}",
    )

    for name in ("payment", "subscription", "invoices", "command"):
        with pytest.raises(SupportAccessDisabled, match="disabled by default"):
            getattr(support, name)


@pytest.mark.parametrize(
    ("decision", "message"),
    [
        (object(), "invalid decision"),
        (AuthorizationDecision(False, "policy refused"), "policy refused"),
    ],
)
async def test_support_read_refuses_each_invalid_authorization_decision(
    decision: object, message: str
) -> None:
    async def authorize(current: Any, requirement: Any) -> Any:
        return decision

    support = BillingSupport(
        billing=Backend(),
        reader=Reader(),
        subject_for=lambda identity, tenant: f"organization:{tenant}",
        access=SupportAccess(authorize),
    )

    with pytest.raises(PermissionError, match=message):
        await support.payment(context(permissions=frozenset({"billing.read"})), "pi_1")


async def test_support_read_refuses_an_unlinked_identity() -> None:
    support = readable_support(Reader())
    current = context(permissions=frozenset({"billing.read"}))
    current.identity = None

    with pytest.raises(PermissionError, match="authenticated linked identity"):
        await support.payment(current, "pi_1")


@pytest.mark.parametrize("subject", ["", 1])
async def test_support_read_refuses_each_invalid_subject_mapping(subject: object) -> None:
    async def authorize(current: Any, requirement: Any) -> AuthorizationDecision:
        return AuthorizationDecision(True, "permit")

    support = BillingSupport(
        billing=Backend(),
        reader=Reader(),
        subject_for=lambda identity, tenant: subject,
        access=SupportAccess(authorize),
    )

    with pytest.raises(KeyError, match="no billing subject mapping"):
        await support.payment(context(permissions=frozenset({"billing.read"})), "pi_1")


def test_support_access_configuration_refuses_invalid_callbacks_and_names() -> None:
    async def authorize(current: Any, requirement: Any) -> AuthorizationDecision:
        return AuthorizationDecision(True, "permit")

    invalid_callbacks: tuple[Any, ...] = (object(), lambda current, requirement: None)
    for invalid in invalid_callbacks:
        with pytest.raises(TypeError, match="authorize must be an async callable"):
            SupportAccess(invalid)
    for option in ("permission", "action"):
        for invalid in ("", 1):
            options: dict[str, Any] = {option: invalid}
            with pytest.raises(ValueError, match=f"billing support {option} must not be empty"):
                SupportAccess(authorize, **options)


def test_money_movement_configuration_refuses_each_invalid_boundary() -> None:
    async def authorize(current: Any, requirement: Any) -> AuthorizationDecision:
        return AuthorizationDecision(True, "permit")

    async def audit(event: BillingAuditEvent) -> None:
        return None

    approvals = ChatApprovalFlow(ChatOps(name="support-config"), InMemoryApprovalStore())
    with pytest.raises(TypeError, match="approvals must be ChatApprovalFlow"):
        SupportMoneyMovement(object(), authorize, audit)
    for option in ("authorize", "audit"):
        for invalid in (object(), lambda value: None):
            options: dict[str, Any] = {option: invalid}
            with pytest.raises(TypeError, match=f"{option} must be an async callable"):
                SupportMoneyMovement(
                    approvals,
                    options.get("authorize", authorize),
                    options.get("audit", audit),
                )
    for ttl in (True, "30", float("inf"), 0, -1):
        with pytest.raises(ValueError, match="ttl must be positive"):
            SupportMoneyMovement(approvals, authorize, audit, ttl=ttl)
    for option in ("permission", "action"):
        for invalid in ("", 1):
            options = {option: invalid}
            with pytest.raises(ValueError, match=f"{option} must not be empty"):
                SupportMoneyMovement(approvals, authorize, audit, **options)


def _grant(resource: str | None) -> ApprovalGrant:
    return ApprovalGrant("approval-1", "acme", "user-7", "billing.refund", resource, 100.0)


@pytest.mark.parametrize(
    "resource",
    [
        None,
        "",
        "not-json",
        "[]",
        json.dumps({"type": "wrong"}),
        json.dumps(
            {
                "type": "wreath.billing.refund.v1",
                "provider": "",
                "payment": "pi_1",
                "subject": "organization:acme",
                "reference": "refund-1",
                "payment_amount": {"currency": "USD", "minor": 100},
                "amount": None,
            }
        ),
        json.dumps(
            {
                "type": "wreath.billing.refund.v1",
                "provider": "stripe",
                "payment": "pi_1",
                "subject": "organization:acme",
                "reference": "refund-1",
                "payment_amount": "USD 100",
                "amount": None,
            }
        ),
        json.dumps(
            {
                "type": "wreath.billing.refund.v1",
                "provider": "stripe",
                "payment": "pi_1",
                "subject": "organization:acme",
                "reference": "refund-1",
                "payment_amount": {"currency": "USD", "minor": 100},
                "amount": 100,
            }
        ),
    ],
)
def test_durable_refund_intent_refuses_each_malformed_boundary(resource: str | None) -> None:
    with pytest.raises(PermissionError, match="invalid durable intent"):
        _intent_from_grant(_grant(resource))


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (None, None),
        ({"currency": "USD", "minor": 25}, Money("USD", 25)),
    ],
)
def test_durable_refund_intent_preserves_optional_amount(
    amount: dict[str, object] | None, expected: Money | None
) -> None:
    resource = json.dumps(
        {
            "type": "wreath.billing.refund.v1",
            "provider": "stripe",
            "payment": "pi_1",
            "subject": "organization:acme",
            "reference": "refund-1",
            "payment_amount": {"currency": "USD", "minor": 100},
            "amount": amount,
        }
    )

    intent = _intent_from_grant(_grant(resource))

    assert intent.amount == expected


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


@pytest.mark.parametrize(
    "projected",
    [
        object(),
        PaymentSnapshot(
            "stripe",
            "pi_1",
            "organization:other",
            "order-1",
            Money("USD", 100),
            PaymentState.SUCCEEDED,
        ),
        PaymentSnapshot(
            "stripe",
            "pi_other",
            "organization:acme",
            "order-1",
            Money("USD", 100),
            PaymentState.SUCCEEDED,
        ),
    ],
)
async def test_support_payment_refuses_each_out_of_scope_projection(projected: object) -> None:
    support = readable_support(ResultReader(payment_result=projected))

    with pytest.raises(ValueError, match="payment outside the requested scope"):
        await support.payment(context(permissions=frozenset({"billing.read"})), "pi_1")


@pytest.mark.parametrize("payment", ["", 1])
async def test_support_payment_refuses_invalid_identifiers_before_reading(
    payment: object,
) -> None:
    support = readable_support(ResultReader())

    with pytest.raises(ValueError, match="billing support payment must not be empty"):
        await support.payment(context(permissions=frozenset({"billing.read"})), payment)


async def test_support_reads_preserve_missing_payment_and_subscription() -> None:
    support = readable_support(ResultReader())
    current = context(permissions=frozenset({"billing.read"}))

    assert await support.payment(current, "pi_missing") is None
    assert await support.subscription(current) is None


@pytest.mark.parametrize(
    "projected",
    [
        object(),
        SubscriptionSnapshot(
            "stripe",
            "sub_1",
            "organization:other",
            "pro",
            SubscriptionState.ACTIVE,
            "active",
        ),
    ],
)
async def test_support_subscription_refuses_each_out_of_scope_projection(
    projected: object,
) -> None:
    support = readable_support(ResultReader(subscription_result=projected))

    with pytest.raises(ValueError, match="subscription outside the requested scope"):
        await support.subscription(context(permissions=frozenset({"billing.read"})))


@pytest.mark.parametrize(
    "projected",
    [
        object(),
        InvoicePage(
            (
                SubscriptionPayment(
                    "stripe",
                    "in_1",
                    "sub_1",
                    "organization:other",
                    datetime(2026, 10, 1, tzinfo=UTC),
                ),
            ),
            None,
        ),
    ],
)
async def test_support_invoices_refuse_each_out_of_scope_projection(projected: object) -> None:
    support = readable_support(ResultReader(invoice_result=projected))

    with pytest.raises(ValueError, match="invoices outside the requested scope"):
        await support.invoices(context(permissions=frozenset({"billing.read"})))


@pytest.mark.parametrize(
    ("operation", "key"),
    [
        ("", "command-1"),
        (1, "command-1"),
        ("refund", ""),
        ("refund", 1),
    ],
)
async def test_support_command_refuses_invalid_identifiers_before_reading(
    operation: object, key: object
) -> None:
    support = readable_support(ResultReader())

    with pytest.raises(ValueError, match="billing support command"):
        await support.command(
            context(permissions=frozenset({"billing.read"})),
            operation,
            key,
        )


@pytest.mark.parametrize(
    "projected",
    [
        object(),
        BillingCommand(
            BillingCommandIdentity(
                "stripe",
                "refund",
                "command-1",
                "a" * 64,
                "organization:other",
            ),
            BillingCommandState.PENDING,
            0,
        ),
    ],
)
async def test_support_command_refuses_each_out_of_scope_projection(projected: object) -> None:
    support = readable_support(ResultReader(command_result=projected))

    with pytest.raises(ValueError, match="command outside the requested scope"):
        await support.command(
            context(permissions=frozenset({"billing.read"})),
            "refund",
            "command-1",
        )


async def test_support_command_preserves_a_valid_projection() -> None:
    command = BillingCommand(
        BillingCommandIdentity(
            "stripe",
            "refund",
            "command-1",
            "a" * 64,
            "organization:acme",
        ),
        BillingCommandState.PENDING,
        0,
    )
    support = readable_support(ResultReader(command_result=command))

    assert (
        await support.command(
            context(permissions=frozenset({"billing.read"})),
            "refund",
            "command-1",
        )
        is command
    )


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
