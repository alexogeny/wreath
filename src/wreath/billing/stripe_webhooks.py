from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..webhooks import (
    PostgresWebhookInbox,
    StripeWebhookVerifier,
    WebhookContext,
    WebhookSource,
)
from .ledger import PostgresBillingLedger
from .operations import BillingOperations

if TYPE_CHECKING:
    from . import Billing
    from .providers.stripe import StripeWebhookPolicy

_CHECKOUT_EVENTS = (
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
)
_SUBSCRIPTION_EVENTS = (
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.paused",
    "customer.subscription.resumed",
)
_EVENTS = (*_CHECKOUT_EVENTS, *_SUBSCRIPTION_EVENTS, "invoice.paid")


def bind_stripe_webhooks(
    source: WebhookSource,
    *,
    billing: Billing,
    webhook: StripeWebhookPolicy,
    checkout_subject_for: Any,
    subscription_subject_for: Any,
    operations: BillingOperations | None = None,
) -> WebhookSource:
    from . import Billing
    from .providers.stripe import (
        DirectCharges,
        StripeBilling,
        StripeInvoiceProjection,
        StripeSubscriptionProjection,
        StripeWebhookPolicy,
    )
    from .providers.stripe_checkout import StripeCheckoutProjection

    if not isinstance(source, WebhookSource):
        raise TypeError("Stripe webhook source must be WebhookSource")
    if not isinstance(source._verifier, StripeWebhookVerifier):
        raise TypeError("Stripe webhook source verifier must be StripeWebhookVerifier")
    if not isinstance(billing, Billing):
        raise TypeError("Stripe webhook billing must be Billing")
    backend = billing.backend
    if not isinstance(backend, StripeBilling):
        raise TypeError("Stripe webhook billing backend must be StripeBilling")
    if not isinstance(webhook, StripeWebhookPolicy):
        raise TypeError("Stripe webhook policy must be StripeWebhookPolicy")
    if operations is not None and not isinstance(operations, BillingOperations):
        raise TypeError("Stripe webhook operations must be BillingOperations or None")
    if backend._api_version != webhook.event_version:
        raise ValueError(
            f"Stripe webhook API version {webhook.event_version!r} differs from "
            f"backend API version {backend._api_version!r}"
        )
    direct_connect = backend._connect is not None and isinstance(
        backend._connect.charges, DirectCharges
    )
    if direct_connect and webhook.scope != "connected_accounts":
        raise ValueError("Stripe direct Connect webhook scope must be 'connected_accounts'")
    if not direct_connect and webhook.scope != "account":
        raise ValueError(
            "Stripe connected_accounts webhook scope requires a direct Connect backend"
        )
    ledger = billing.ledger
    if not isinstance(ledger, PostgresBillingLedger):
        raise TypeError("Stripe webhooks require a configured PostgresBillingLedger")
    if not isinstance(source._inbox, PostgresWebhookInbox):
        if source._inbox is None:
            raise ValueError("Stripe webhooks require a durable PostgresWebhookInbox source")
        raise TypeError("Stripe webhook source inbox must be PostgresWebhookInbox")
    if not callable(source._session_factory):
        raise TypeError("Stripe webhook source requires a callable inbox session_factory")
    duplicate = next((event for event in _EVENTS if event in source._handlers), None)
    if duplicate is not None:
        raise ValueError(f"duplicate webhook event handler: {duplicate}")

    checkout = StripeCheckoutProjection(
        webhook=webhook,
        retrieve_checkout=backend.retrieve_checkout,
        subject_for=checkout_subject_for,
    )
    subscription = StripeSubscriptionProjection(
        billing.catalog,
        webhook=webhook,
        subject_for=subscription_subject_for,
        retrieve_subscription=backend.retrieve_subscription,
        plan_for_price=billing.plan_for_provider_price,
    )
    invoice = StripeInvoiceProjection(
        webhook=webhook,
        subject_for=subscription_subject_for,
    )

    async def project_checkout(context: WebhookContext, payload: Any) -> None:
        del payload
        completed = False
        try:
            session = _transaction(context)
            payment = await checkout.project(context.envelope)
            if payment is not None:
                await ledger.apply_checkout(session, payment)
            completed = True
        finally:
            _record(operations, completed)

    async def project_subscription(context: WebhookContext, payload: Any) -> None:
        del payload
        completed = False
        try:
            session = _transaction(context)
            snapshot = await subscription.project(context.envelope)
            if snapshot is None:
                raise RuntimeError("registered Stripe subscription event was not projected")
            await ledger.apply_subscription(session, snapshot)
            completed = True
        finally:
            _record(operations, completed)

    async def project_invoice(context: WebhookContext, payload: Any) -> None:
        del payload
        completed = False
        try:
            session = _transaction(context)
            payment = invoice.project(context.envelope)
            if payment is None:
                raise RuntimeError("registered Stripe invoice event was not projected")
            await ledger.apply_payment(session, payment)
            completed = True
        finally:
            _record(operations, completed)

    for event in _CHECKOUT_EVENTS:
        source.event(event, payload=dict[str, Any])(project_checkout)
    for event in _SUBSCRIPTION_EVENTS:
        source.event(event, payload=dict[str, Any])(project_subscription)
    source.event("invoice.paid", payload=dict[str, Any])(project_invoice)
    return source


def _transaction(context: WebhookContext) -> Any:
    if context.session is None:
        raise RuntimeError("Stripe webhook projection requires its inbox transaction session")
    return context.session


def _record(operations: BillingOperations | None, completed: bool) -> None:
    if operations is None:
        return
    if completed:
        operations.webhook_applied()
    else:
        operations.webhook_failed()


stripe_webhooks = bind_stripe_webhooks


__all__ = ["bind_stripe_webhooks", "stripe_webhooks"]
