from __future__ import annotations

import inspect
import json
import math
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .._agents.approvals import ApprovalGrant
from .._agents.chat_approvals import ChatApprovalFlow
from .._auth.cedar_engine import EntityUid
from .._auth.models import AuthorizationDecision, Identity, qualified_identity_value
from .._auth.requirements import PolicyRequirement
from .._capability_map import CapabilityMap
from ..chat import ChatContext, ChatReply
from ..payments import Money, PaymentSnapshot, PaymentState
from ..subscriptions import SubscriptionSnapshot
from .ledger import BillingCommand
from .queries import InvoiceCursor, InvoicePage


class MoneyMovementDisabled(RuntimeError):
    pass


class SupportAccessDisabled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BillingAuditEvent:
    action: str
    actor: str
    subject: str
    resource: str
    approval_id: str


@dataclass(frozen=True, slots=True)
class SupportMoneyMovement:
    approvals: ChatApprovalFlow
    authorize: Callable[[ChatContext, PolicyRequirement], Awaitable[AuthorizationDecision]]
    audit: Callable[[BillingAuditEvent], Awaitable[None]]
    ttl: float = 300.0
    permission: str = "billing.refund"
    action: str = "billing.refund"

    def __post_init__(self) -> None:
        if not isinstance(self.approvals, ChatApprovalFlow):
            raise TypeError("billing money movement approvals must be ChatApprovalFlow")
        for name in ("authorize", "audit"):
            value = getattr(self, name)
            if not callable(value) or not inspect.iscoroutinefunction(value):
                raise TypeError(f"billing money movement {name} must be an async callable")
        if (
            isinstance(self.ttl, bool)
            or not isinstance(self.ttl, (int, float))
            or not math.isfinite(self.ttl)
            or self.ttl <= 0
        ):
            raise ValueError("billing money movement ttl must be positive")
        for name in ("permission", "action"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"billing money movement {name} must not be empty")


@dataclass(frozen=True, slots=True)
class SupportAccess:
    authorize: Callable[[ChatContext, PolicyRequirement], Awaitable[AuthorizationDecision]]
    permission: str = "billing.read"
    action: str = "billing.read"

    def __post_init__(self) -> None:
        if not callable(self.authorize) or not inspect.iscoroutinefunction(self.authorize):
            raise TypeError("billing support authorize must be an async callable")
        for name in ("permission", "action"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"billing support {name} must not be empty")


@dataclass(frozen=True, slots=True)
class _RefundIntent:
    approval_id: str
    tenant: str
    principal_id: str
    subject: str
    payment: PaymentSnapshot
    reference: str
    amount: Money | None
    resource: str


def _refund_resource(payment: PaymentSnapshot, reference: str, amount: Money | None) -> str:
    return json.dumps(
        {
            "amount": None
            if amount is None
            else {"currency": amount.currency, "minor": amount.minor},
            "merchant_account": payment.merchant_account,
            "payment": payment.id,
            "payment_amount": {
                "currency": payment.amount.currency,
                "minor": payment.amount.minor,
            },
            "provider": payment.provider,
            "reference": reference,
            "subject": payment.subject,
            "type": "wreath.billing.refund.v1",
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _intent_from_grant(grant: ApprovalGrant) -> _RefundIntent:
    resource = grant.resource
    if not isinstance(resource, str) or not resource:
        raise PermissionError("billing refund approval has an invalid durable intent")
    try:
        payload = json.loads(resource)
    except (json.JSONDecodeError, TypeError) as error:
        raise PermissionError("billing refund approval has an invalid durable intent") from error
    if not isinstance(payload, dict) or payload.get("type") != "wreath.billing.refund.v1":
        raise PermissionError("billing refund approval has an invalid durable intent")
    required = ("provider", "payment", "subject", "reference")
    if any(type(payload.get(field)) is not str or not payload[field] for field in required):
        raise PermissionError("billing refund approval has an invalid durable intent")
    payment_amount = payload.get("payment_amount")
    amount = payload.get("amount")
    if not isinstance(payment_amount, dict) or (
        amount is not None and not isinstance(amount, dict)
    ):
        raise PermissionError("billing refund approval has an invalid durable intent")
    try:
        projected_amount = Money(payment_amount.get("currency"), payment_amount.get("minor"))
        refund_amount = (
            None
            if amount is None
            else Money(amount.get("currency"), amount.get("minor"))
        )
        payment = PaymentSnapshot(
            payload["provider"],
            payload["payment"],
            payload["subject"],
            "approved-refund",
            projected_amount,
            PaymentState.SUCCEEDED,
            merchant_account=payload.get("merchant_account"),
        )
    except (TypeError, ValueError) as error:
        raise PermissionError("billing refund approval has an invalid durable intent") from error
    return _RefundIntent(
        grant.approval_id,
        grant.tenant,
        grant.principal_id,
        payload["subject"],
        payment,
        payload["reference"],
        refund_amount,
        resource,
    )


class BillingSupport:
    __slots__ = (
        "_access",
        "_billing",
        "_intents",
        "_money",
        "_reader",
        "_subject_for",
    )

    def __init__(
        self,
        *,
        billing: Any,
        reader: Any,
        subject_for: Callable[[Identity, str], str],
        access: SupportAccess | None = None,
        money: SupportMoneyMovement | None = None,
        max_pending: int = 1024,
    ) -> None:
        if not callable(getattr(reader, "payment", None)):
            raise TypeError("billing support reader must provide async payment(subject, id)")
        if not callable(subject_for):
            raise TypeError("billing support subject_for must be callable")
        if access is not None and not isinstance(access, SupportAccess):
            raise TypeError("billing support access must be SupportAccess or None")
        if access is not None:
            missing = tuple(
                name
                for name in ("subscription", "invoices", "command")
                if not callable(getattr(reader, name, None))
            )
            if missing:
                names = ", ".join(f"{name}()" for name in missing)
                raise TypeError(f"billing support reader must provide {names}")
        if money is not None and not isinstance(money, SupportMoneyMovement):
            raise TypeError("billing support money must be SupportMoneyMovement or None")
        if money is not None and not callable(getattr(billing, "_refund_projected", None)):
            raise TypeError(
                "billing support money movement requires Billing's projected refund boundary"
            )
        if type(max_pending) is not int or max_pending <= 0:
            raise ValueError("billing support max_pending must be a positive integer")
        self._billing = billing
        self._reader = reader
        self._subject_for = subject_for
        self._access = access
        self._money = money
        self._intents = CapabilityMap(
            max_entries=max_pending,
            ttl=None if money is None else money.ttl,
            overflow="refuse",
        )
        if money is not None:
            money.approvals.on_approved(money.action, self._approved_refund)

    @property
    def schema_owners(self) -> tuple[Any, ...]:
        return () if self._money is None else self._money.approvals.schema_owners

    @property
    def payment(self) -> Callable[..., Awaitable[PaymentSnapshot | None]]:
        if self._access is None:
            raise SupportAccessDisabled(
                "billing support reads are disabled by default; configure SupportAccess "
                "with Cedar authorization"
            )
        return self._payment

    @property
    def subscription(self) -> Callable[..., Awaitable[SubscriptionSnapshot | None]]:
        self._require_access()
        return self._subscription

    @property
    def invoices(self) -> Callable[..., Awaitable[InvoicePage]]:
        self._require_access()
        return self._invoices

    @property
    def command(self) -> Callable[..., Awaitable[BillingCommand | None]]:
        self._require_access()
        return self._command

    async def _payment(
        self,
        context: ChatContext,
        payment: str,
    ) -> PaymentSnapshot | None:
        if not isinstance(payment, str) or not payment:
            raise ValueError("billing support payment must not be empty")
        subject = await self._authorize_access(
            context,
            resource_suffix=f"payment:{payment}",
        )
        projected = await self._reader.payment(subject, payment)
        if projected is not None and (
            not isinstance(projected, PaymentSnapshot)
            or projected.subject != subject
            or projected.id != payment
        ):
            raise ValueError(
                "billing support reader returned a payment outside the requested scope"
            )
        return projected

    async def _subscription(self, context: ChatContext) -> SubscriptionSnapshot | None:
        subject = await self._authorize_access(
            context,
            resource_suffix="subscription",
        )
        snapshot = await self._reader.subscription(subject)
        if snapshot is not None and (
            not isinstance(snapshot, SubscriptionSnapshot) or snapshot.subject != subject
        ):
            raise ValueError(
                "billing support reader returned a subscription outside the requested scope"
            )
        return snapshot

    async def _invoices(
        self,
        context: ChatContext,
        *,
        subscription: str | None = None,
        cursor: InvoiceCursor | None = None,
        limit: int = 20,
    ) -> InvoicePage:
        subject = await self._authorize_access(
            context,
            resource_suffix="invoices",
        )
        page = await self._reader.invoices(
            subject,
            subscription=subscription,
            cursor=cursor,
            limit=limit,
        )
        if not isinstance(page, InvoicePage) or any(
            item.subject != subject for item in page.items
        ):
            raise ValueError(
                "billing support reader returned invoices outside the requested scope"
            )
        return page

    async def _command(
        self,
        context: ChatContext,
        operation: str,
        idempotency_key: str,
    ) -> BillingCommand | None:
        for field, value in (("operation", operation), ("idempotency_key", idempotency_key)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"billing support command {field} must not be empty")
        subject = await self._authorize_access(
            context,
            resource_suffix=f"command:{operation}:{idempotency_key}",
        )
        command = await self._reader.command(subject, operation, idempotency_key)
        if command is not None and (
            not isinstance(command, BillingCommand) or command.identity.subject != subject
        ):
            raise ValueError(
                "billing support reader returned a command outside the requested scope"
            )
        return command

    @property
    def propose_refund(self) -> Callable[..., Awaitable[ChatReply]]:
        if self._money is None:
            raise MoneyMovementDisabled(
                "billing support money movement is disabled by default; configure "
                "SupportMoneyMovement with Cedar authorization, human approval, and audit"
            )
        return self._propose_refund

    async def _propose_refund(
        self,
        context: ChatContext,
        *,
        payment: str,
        reference: str,
        amount: Money | None = None,
    ) -> ChatReply:
        money = self._money
        if money is None:
            raise MoneyMovementDisabled("billing support money movement is disabled by default")
        if context.agent_request is not None or getattr(
            context.identity, "narrowing", None
        ) is not None:
            raise PermissionError("billing refund approval must be executed by a human")
        identity, subject = self._identity_subject(context)
        principal_id = qualified_identity_value(identity.namespace, identity.id)
        if money.permission not in identity.authority_permissions:
            raise PermissionError(f"billing refund requires {money.permission} permission")
        if not isinstance(payment, str) or not payment:
            raise ValueError("billing support refund payment must not be empty")
        if not isinstance(reference, str) or not reference:
            raise ValueError("billing support refund reference must not be empty")
        projected = await self._reader.payment(subject, payment)
        if not isinstance(projected, PaymentSnapshot):
            raise KeyError(f"no payment {payment!r} for billing subject {subject!r}")
        if projected.subject != subject or projected.id != payment:
            raise ValueError(
                "billing support reader returned a payment outside the requested scope"
            )
        if projected.state is not PaymentState.SUCCEEDED:
            raise ValueError("billing support can refund only a succeeded payment")
        if amount is not None:
            if amount.currency != projected.amount.currency:
                raise ValueError("billing support refund currency differs from the payment")
            if amount.minor > projected.amount.minor:
                raise ValueError("billing support refund amount exceeds the payment amount")
        approval_id = f"billing-refund-{secrets.token_hex(12)}"
        resource = _refund_resource(projected, reference, amount)
        decision = await money.authorize(
            context,
            PolicyRequirement(money.action, EntityUid("BillingRefund", resource)),
        )
        if not isinstance(decision, AuthorizationDecision) or not decision.allowed:
            reason = decision.reason if isinstance(decision, AuthorizationDecision) else None
            raise PermissionError(f"billing refund denied by Cedar: {reason or 'invalid decision'}")
        intent = _RefundIntent(
            approval_id,
            context.tenant,
            principal_id,
            subject,
            projected,
            reference,
            amount,
            resource,
        )
        if not self._intents.claim(approval_id, intent, ttl=money.ttl):
            raise OverflowError("billing support pending refund approvals reached capacity")
        try:
            return await money.approvals.issue(
                context,
                approval_id=approval_id,
                action=money.action,
                resource=resource,
                ttl=money.ttl,
                require_fresh_auth=True,
            )
        except BaseException:
            self._intents.discard(approval_id)
            raise

    async def _approved_refund(
        self,
        context: ChatContext,
        grant: ApprovalGrant,
    ) -> ChatReply:
        money = self._money
        if money is None:
            raise MoneyMovementDisabled("billing support money movement is disabled by default")
        if context.agent_request is not None or getattr(
            context.identity, "narrowing", None
        ) is not None:
            raise PermissionError("billing refund approval must be executed by a human")
        identity, subject = self._identity_subject(context)
        principal_id = qualified_identity_value(identity.namespace, identity.id)
        intent = self._intents.peek(grant.approval_id)
        if not isinstance(intent, _RefundIntent):
            intent = _intent_from_grant(grant)
        expected = (
            intent.tenant,
            intent.principal_id,
            money.action,
            intent.resource,
        )
        received = (grant.tenant, grant.principal_id, grant.action, grant.resource)
        if received != expected or intent.subject != subject:
            raise PermissionError("billing refund approval does not match the exact refund intent")
        if money.permission not in identity.authority_permissions:
            raise PermissionError(f"billing refund requires {money.permission} permission")
        requirement = PolicyRequirement(
            money.action,
            EntityUid("BillingRefund", intent.resource),
        )
        decision = await money.authorize(context, requirement)
        if not isinstance(decision, AuthorizationDecision) or not decision.allowed:
            reason = decision.reason if isinstance(decision, AuthorizationDecision) else None
            raise PermissionError(f"billing refund denied by Cedar: {reason or 'invalid decision'}")
        consumed = self._intents.consume(
            grant.approval_id,
            predicate=lambda candidate: candidate == intent,
        )
        if consumed is None and self._intents.peek(grant.approval_id) is not None:
            raise PermissionError("billing refund intent was already consumed")
        projected = await self._reader.payment(subject, intent.payment.id)
        if not isinstance(projected, PaymentSnapshot) or (
            projected.provider,
            projected.id,
            projected.subject,
            projected.amount,
            projected.state,
            projected.merchant_account,
        ) != (
            intent.payment.provider,
            intent.payment.id,
            intent.payment.subject,
            intent.payment.amount,
            PaymentState.SUCCEEDED,
            intent.payment.merchant_account,
        ):
            raise PermissionError("billing refund payment changed after human approval")
        await money.audit(
            BillingAuditEvent(
                action=money.action,
                actor=principal_id,
                subject=subject,
                resource=intent.resource,
                approval_id=grant.approval_id,
            )
        )
        refund = await self._billing._refund_projected(
            projected,
            reference=intent.reference,
            amount=intent.amount,
        )
        return ChatReply.ephemeral(f"Refund {refund.id} is {refund.state.value}.")

    async def _authorize_access(
        self,
        context: ChatContext,
        *,
        resource_suffix: str,
    ) -> str:
        access = self._access
        if access is None:
            raise SupportAccessDisabled("billing support reads are disabled by default")
        identity, subject = self._identity_subject(context)
        if access.permission not in identity.authority_permissions:
            raise PermissionError(f"billing support requires {access.permission} permission")
        resource = EntityUid("BillingRecord", f"{subject}:{resource_suffix}")
        decision = await access.authorize(context, PolicyRequirement(access.action, resource))
        if not isinstance(decision, AuthorizationDecision) or not decision.allowed:
            reason = decision.reason if isinstance(decision, AuthorizationDecision) else None
            raise PermissionError(
                f"billing support read denied by Cedar: {reason or 'invalid decision'}"
            )
        return subject

    def _require_access(self) -> SupportAccess:
        if self._access is None:
            raise SupportAccessDisabled(
                "billing support reads are disabled by default; configure SupportAccess "
                "with Cedar authorization"
            )
        return self._access

    def _identity_subject(self, context: ChatContext) -> tuple[Identity, str]:
        identity = context.identity
        if not isinstance(identity, Identity):
            raise PermissionError("billing support requires an authenticated linked identity")
        subject = self._subject_for(identity, context.tenant)
        if not isinstance(subject, str) or not subject:
            raise KeyError("no billing subject mapping for the support identity")
        return identity, subject


__all__ = [
    "BillingAuditEvent",
    "BillingSupport",
    "MoneyMovementDisabled",
    "SupportAccess",
    "SupportAccessDisabled",
    "SupportMoneyMovement",
]
