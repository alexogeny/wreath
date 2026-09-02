from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal, cast

import pytest

from wreath.billing import (
    Billing,
    ConnectedMerchant,
    ConnectedMerchants,
    DeploymentMerchant,
    HostedRedirect,
    PostgresBillingLedger,
)
from wreath.billing.providers.stripe import (
    DirectCharges,
    StripeBilling,
    StripeConnect,
    StripeWebhookPolicy,
)
from wreath.billing.stripe_webhooks import bind_stripe_webhooks
from wreath.config import Secret
from wreath.http_client import ClientResponse
from wreath.payments import PaymentSnapshot
from wreath.request import Request
from wreath.subscriptions import Plan, PlanCatalog, SubscriptionPayment, SubscriptionSnapshot
from wreath.webhooks import (
    PostgresWebhookInbox,
    StripeWebhookVerifier,
    WebhookContext,
    WebhookEnvelope,
    WebhookLimits,
    WebhookSource,
)


class App:
    def post(self, path: str, **options: Any) -> Any:
        del path, options

        def register(handler: Any) -> Any:
            return handler

        return register


class SessionFactory:
    def __call__(self) -> Any:
        raise AssertionError("the binding does not open a second transaction")


class Client:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    async def get(
        self,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
    ) -> ClientResponse:
        del headers
        self.calls.append(target)
        return ClientResponse(200, (), json.dumps(self.objects[target]).encode(), "1.1")


class RecordingLedger(PostgresBillingLedger):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, Any, Any]] = []

    async def apply_checkout(self, session: Any, payment: PaymentSnapshot) -> None:
        self.calls.append(("checkout", session, payment))

    async def apply_subscription(
        self,
        session: Any,
        snapshot: SubscriptionSnapshot,
        *,
        merchant_account: str | None = None,
    ) -> None:
        assert merchant_account is None
        self.calls.append(("subscription", session, snapshot))

    async def apply_payment(
        self,
        session: Any,
        payment: SubscriptionPayment,
        *,
        merchant_account: str | None = None,
    ) -> None:
        assert merchant_account is None
        self.calls.append(("invoice", session, payment))


def source(*, inbox: Any = None, session_factory: Any = None) -> WebhookSource:
    return WebhookSource(
        App(),
        "stripe",
        path="/webhooks/stripe",
        verifier=StripeWebhookVerifier(b"whsec_test"),
        replay=None,
        limits=WebhookLimits(),
        inbox=PostgresWebhookInbox() if inbox is None else inbox,
        session_factory=SessionFactory() if session_factory is None else session_factory,
        lease_owner="billing-webhook",
        lease_seconds=30,
    )


def configured(
    *,
    api_version: str = "2026-08-26.dahlia",
    connect: StripeConnect | None = None,
) -> tuple[Billing, Client, RecordingLedger]:
    client = Client()
    backend = StripeBilling(
        client=client,
        api_key=Secret("rk_test_example"),
        api_version=api_version,
        allowed_return_origins=("https://app.example",),
        connect=connect,
    )
    ledger = RecordingLedger()
    merchant = ConnectedMerchant() if connect is not None else DeploymentMerchant()
    topology = (
        ConnectedMerchants(
            account_for=lambda subject: "acct_acme",
            price_for=lambda subject, sku, account: "price_pro",
            sku_for_price=lambda subject, price, account: "pro",
        )
        if connect is not None
        else None
    )
    options = {} if topology is None else {"topology": topology}
    billing = Billing(
        "billing",
        backend=backend,
        catalog=PlanCatalog(Plan("pro", "price_pro")),
        merchant=merchant,
        capture=HostedRedirect(),
        ledger=ledger,
        **options,
    )
    return billing, client, ledger


def policy(
    scope: Literal["account", "connected_accounts"] = "account",
) -> StripeWebhookPolicy:
    return StripeWebhookPolicy("2026-08-26.dahlia", False, scope)


def bind(
    webhook_source: WebhookSource,
    billing: Billing,
    webhook: StripeWebhookPolicy | None = None,
) -> None:
    bind_stripe_webhooks(
        webhook_source,
        billing=billing,
        webhook=policy() if webhook is None else webhook,
        checkout_subject_for=lambda reference, customer, account: "organization:acme",
        subscription_subject_for=lambda customer, account: "organization:acme",
    )


def bind_native(
    webhook_source: WebhookSource,
    billing: Billing,
    webhook: StripeWebhookPolicy | None = None,
) -> None:
    billing.stripe_webhooks(
        webhook_source,
        webhook=policy() if webhook is None else webhook,
        checkout_subject_for=lambda reference, customer, account: "organization:acme",
        subscription_subject_for=lambda customer, account: "organization:acme",
    )


def envelope(event_type: str, resource: dict[str, Any]) -> WebhookEnvelope:
    body = json.dumps(
        {
            "id": f"evt_{event_type}",
            "type": event_type,
            "api_version": "2026-08-26.dahlia",
            "livemode": False,
            "data": {"object": resource},
        }
    ).encode()
    return WebhookEnvelope(
        f"evt_{event_type}",
        event_type,
        "2026-08-26.dahlia",
        datetime(2026, 9, 2, tzinfo=UTC),
        "application/json",
        body,
    )


def test_binding_registers_the_complete_supported_stripe_event_set() -> None:
    billing, _, _ = configured()
    webhook_source = source()

    bind_native(webhook_source, billing)

    assert set(webhook_source._handlers) == {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "customer.subscription.paused",
        "customer.subscription.resumed",
        "invoice.paid",
    }
    assert billing.preflight() == ("configure durable Stripe reconciliation",)


@pytest.mark.asyncio
async def test_checkout_handler_retrieves_and_applies_inside_the_inbox_transaction() -> None:
    billing, client, ledger = configured()
    webhook_source = source()
    bind_native(webhook_source, billing)
    client.objects["/v1/checkout/sessions/cs_1"] = {
        "id": "cs_1",
        "mode": "payment",
        "payment_status": "paid",
        "payment_intent": "pi_1",
        "amount_total": 2500,
        "currency": "aud",
        "client_reference_id": "order_1",
        "customer": "cus_acme",
    }
    event = envelope("checkout.session.completed", {"id": "cs_1"})
    session = object()
    context = WebhookContext("stripe", event, cast(Request, object()), session)

    await webhook_source._handlers[event.type][1](context, {"discard": "decoded payload"})

    assert client.calls == ["/v1/checkout/sessions/cs_1"]
    assert [(kind, used_session) for kind, used_session, _ in ledger.calls] == [
        ("checkout", session)
    ]
    assert ledger.calls[0][2].id == "pi_1"
    assert billing.operations.counters().values["webhooks_applied"] == 1


@pytest.mark.asyncio
async def test_native_binding_counts_projection_failure_without_swallowing_it() -> None:
    billing, _, _ = configured()
    webhook_source = source()
    bind_native(webhook_source, billing)
    event = envelope("checkout.session.completed", {"id": "cs_missing"})
    context = WebhookContext("stripe", event, cast(Request, object()), object())

    with pytest.raises(KeyError):
        await webhook_source._handlers[event.type][1](context, {})

    assert billing.operations.counters().values["webhook_failures"] == 1


@pytest.mark.asyncio
async def test_subscription_and_invoice_handlers_apply_authoritative_projections() -> None:
    billing, client, ledger = configured()
    webhook_source = source()
    bind(webhook_source, billing)
    client.objects["/v1/subscriptions/sub_1"] = {
        "id": "sub_1",
        "customer": "cus_acme",
        "status": "active",
        "items": {"data": [{"price": {"id": "price_pro"}}]},
    }
    subscription = envelope("customer.subscription.resumed", {"id": "sub_1"})
    invoice = envelope(
        "invoice.paid",
        {
            "id": "in_1",
            "customer": "cus_acme",
            "status": "paid",
            "paid": True,
            "parent": {
                "type": "subscription_details",
                "subscription_details": {"subscription": "sub_1"},
            },
            "lines": {
                "data": [
                    {
                        "period": {"end": 1_801_267_200},
                        "parent": {
                            "type": "subscription_item_details",
                            "subscription_item_details": {"subscription": "sub_1"},
                        },
                    }
                ]
            },
        },
    )
    session = object()

    for event in (subscription, invoice):
        context = WebhookContext("stripe", event, cast(Request, object()), session)
        await webhook_source._handlers[event.type][1](context, {})

    assert [kind for kind, _, _ in ledger.calls] == ["subscription", "invoice"]
    assert all(used_session is session for _, used_session, _ in ledger.calls)


def test_binding_refuses_version_backend_scope_ledger_and_transaction_mismatches() -> None:
    billing, _, _ = configured()
    with pytest.raises(ValueError, match="API version.*backend"):
        bind(source(), billing, StripeWebhookPolicy("2026-09-01.elm", False, "account"))

    direct = StripeConnect(DirectCharges())
    direct_billing, _, _ = configured(connect=direct)
    with pytest.raises(ValueError, match="connected_accounts"):
        bind(source(), direct_billing)

    billing.ledger = object()
    with pytest.raises(TypeError, match="PostgresBillingLedger"):
        bind(source(), billing)

    billing, _, _ = configured()
    local = WebhookSource(
        App(),
        "stripe",
        path="/webhooks/stripe-local",
        verifier=StripeWebhookVerifier(b"whsec_test"),
        replay=None,
        limits=WebhookLimits(),
        inbox=None,
        session_factory=None,
        lease_owner="unused",
        lease_seconds=30,
    )
    with pytest.raises(ValueError, match="durable PostgresWebhookInbox"):
        bind(local, billing)
    with pytest.raises(TypeError, match="PostgresWebhookInbox"):
        bind(source(inbox=object()), billing)
