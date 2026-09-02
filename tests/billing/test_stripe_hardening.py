from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime
from typing import Any

import pytest

from wreath.billing import Billing, HostedRedirect, ProviderMerchant
from wreath.billing.providers.stripe import (
    DirectCharges,
    StripeBilling,
    StripeConnect,
    StripeError,
    StripeInvoiceProjection,
    StripeSubscriptionProjection,
    StripeWebhookPolicy,
)
from wreath.config import Secret
from wreath.http_client import ClientResponse
from wreath.payments import CheckoutItem, CheckoutRequest, RefundRequest
from wreath.subscriptions import Plan, PlanCatalog
from wreath.webhooks import WebhookEnvelope


class Client:
    def __init__(self, response: ClientResponse) -> None:
        self.response = response
        self.options: dict[str, Any] = {}

    async def post(self, target: str, **options: Any) -> ClientResponse:
        self.options = {"target": target, **options}
        return self.response

    async def get(self, target: str, **options: Any) -> ClientResponse:
        self.options = {"target": target, **options}
        return self.response


def stripe(client: Client, **options: Any) -> StripeBilling:
    return StripeBilling(
        client=client,
        api_key=Secret("rk_test_example"),
        api_version="2026-08-26.dahlia",
        allowed_return_origins=("https://app.example",),
        **options,
    )


def envelope(event_type: str, object_payload: dict[str, Any]) -> WebhookEnvelope:
    body = json.dumps(
        {
            "id": "evt_1",
            "type": event_type,
            "api_version": "2026-08-26.dahlia",
            "livemode": False,
            "data": {"object": object_payload},
        }
    ).encode()
    return WebhookEnvelope(
        "evt_1",
        event_type,
        "2026-08-26.dahlia",
        datetime(2026, 9, 2, tzinfo=UTC),
        "application/json",
        body,
    )


def retriever(value: Any) -> Any:
    async def retrieve(subscription_id: str, account: str | None) -> Any:
        return value

    return retrieve


def test_subscription_projection_requires_an_async_retriever() -> None:
    with pytest.raises(TypeError, match="retrieve_subscription must be async callable"):
        StripeSubscriptionProjection(
            PlanCatalog(Plan("pro", "price_pro")),
            webhook=StripeWebhookPolicy("2026-08-26.dahlia", False, "account"),
            subject_for=lambda customer, account: "organization:acme",
            retrieve_subscription=lambda subscription_id, account: {},
        )


def test_webhook_projection_requires_the_basil_invoice_shape() -> None:
    with pytest.raises(ValueError, match="2025-03-31.basil or later"):
        StripeWebhookPolicy("2024-12-18.acacia", False, "account")


def test_managed_payments_accepts_the_current_ga_api_version() -> None:
    backend = stripe(Client(ClientResponse(200, (), b"{}", "1.1")), managed_payments=True)

    assert backend.capabilities.merchant == "provider"


def test_managed_payments_posture_keeps_external_eligibility_visible() -> None:
    backend = StripeBilling(
        client=Client(ClientResponse(200, (), b"{}", "1.1")),
        api_key=Secret("rk_test_example"),
        api_version="2026-08-26.dahlia",
        allowed_return_origins=("https://app.example",),
        managed_payments=True,
    )
    billing = Billing(
        "commerce",
        backend=backend,
        catalog=PlanCatalog(Plan("pro", "price_pro")),
        merchant=ProviderMerchant(),
        capture=HostedRedirect(),
    )

    unresolved = billing.compliance_posture().unresolved

    assert any("eligible tax code" in item for item in unresolved)
    assert any("terms" in item for item in unresolved)
    assert any("business and product eligibility" in item for item in unresolved)


def test_application_fee_percent_matches_stripes_two_decimal_contract() -> None:
    assert DirectCharges(application_fee_percent="12.34").application_fee_percent == "12.34"
    for value in ("12.345", "1e1", ".5", "01.00"):
        with pytest.raises(ValueError, match="decimal string with at most two decimal places"):
            DirectCharges(application_fee_percent=value)


def test_direct_connect_declares_account_scoped_prices() -> None:
    direct = stripe(
        Client(ClientResponse(200, (), b"{}", "1.1")),
        connect=StripeConnect(DirectCharges()),
    )

    assert direct.capabilities.account_scoped_prices is True


@pytest.mark.asyncio
async def test_existing_customer_is_sent_to_checkout() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"cs_1","url":"https://checkout.stripe.com/c/pay/known"}',
            "1.1",
        )
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro"),),
        mode="subscription",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JKNOWNCUSTOMER",
        customer="cus_acme",
    )

    await stripe(client).create_checkout(request, idempotency_key=request.reference)

    assert b"customer=cus_acme" in client.options["body"]


@pytest.mark.asyncio
async def test_invalid_provider_json_does_not_retain_the_response_body() -> None:
    secret = "secret-customer-payload"
    client = Client(ClientResponse(200, (), secret.encode(), "1.1"))

    with pytest.raises(StripeError, match="invalid JSON") as caught:
        await stripe(client).create_refund(
            RefundRequest("pi_1", "01JINVALIDJSON"),
            idempotency_key="01JINVALIDJSON",
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
async def test_invalid_webhook_json_does_not_retain_the_body() -> None:
    secret = b"secret-webhook-payload"
    invalid = WebhookEnvelope(
        "evt_1",
        "customer.subscription.updated",
        "2026-08-26.dahlia",
        datetime(2026, 9, 2, tzinfo=UTC),
        "application/json",
        secret,
    )
    projection = StripeSubscriptionProjection(
        PlanCatalog(Plan("pro", "price_pro")),
        webhook=StripeWebhookPolicy("2026-08-26.dahlia", False, "account"),
        subject_for=lambda customer, account: "organization:acme",
        retrieve_subscription=retriever({}),
    )

    with pytest.raises(ValueError, match="not valid JSON") as caught:
        await projection.project(invalid)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret.decode() not in "".join(traceback.format_exception(caught.value))


def test_invoice_projection_refuses_truncated_embedded_lines() -> None:
    invoice = {
        "id": "in_1",
        "customer": "cus_acme",
        "paid": True,
        "status": "paid",
        "parent": {
            "type": "subscription_details",
            "subscription_details": {"subscription": "sub_1"},
        },
        "lines": {"data": [], "has_more": True},
    }
    projection = StripeInvoiceProjection(
        webhook=StripeWebhookPolicy("2026-08-26.dahlia", False, "account"),
        subject_for=lambda customer, account: "organization:acme",
    )

    with pytest.raises(ValueError, match="truncated.*retrieve all invoice lines"):
        projection.project(envelope("invoice.paid", invoice))


@pytest.mark.asyncio
async def test_subscription_projection_refuses_truncated_current_items() -> None:
    subscription = {
        "id": "sub_1",
        "customer": "cus_acme",
        "status": "active",
        "items": {
            "data": [{"price": {"id": "price_pro"}}],
            "has_more": True,
        },
    }
    projection = StripeSubscriptionProjection(
        PlanCatalog(Plan("pro", "price_pro")),
        webhook=StripeWebhookPolicy("2026-08-26.dahlia", False, "account"),
        subject_for=lambda customer, account: "organization:acme",
        retrieve_subscription=retriever(subscription),
    )

    with pytest.raises(ValueError, match="truncated.*retrieve all subscription items"):
        await projection.project(envelope("customer.subscription.updated", {"id": "sub_1"}))


@pytest.mark.asyncio
async def test_connected_subscription_projection_requires_account_scoped_price_resolution() -> None:
    webhook = StripeWebhookPolicy("2026-08-26.dahlia", False, "connected_accounts")
    catalog = PlanCatalog(Plan("pro", "price_platform_pro"))

    with pytest.raises(TypeError, match="plan_for_price"):
        StripeSubscriptionProjection(
            catalog,
            webhook=webhook,
            subject_for=lambda customer, account: "organization:acme",
            retrieve_subscription=retriever({}),
        )

    connected = envelope(
        "customer.subscription.updated",
        {
            "id": "sub_1",
            "customer": "cus_acme",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_acme_pro"}}]},
        },
    )
    payload = json.loads(connected.body)
    payload["account"] = "acct_acme"
    connected = WebhookEnvelope(
        connected.id,
        connected.type,
        connected.version,
        connected.timestamp,
        connected.content_type,
        json.dumps(payload).encode(),
    )
    calls: list[tuple[str, str, str | None]] = []

    def plan_for_price(*, subject: str, provider_price: str, merchant_account: str | None) -> Plan:
        calls.append((subject, provider_price, merchant_account))
        return catalog["pro"]

    projection = StripeSubscriptionProjection(
        catalog,
        webhook=webhook,
        subject_for=lambda customer, account: "organization:acme",
        plan_for_price=plan_for_price,
        retrieve_subscription=retriever(payload["data"]["object"]),
    )

    snapshot = await projection.project(connected)

    assert snapshot is not None
    assert snapshot.plan == "pro"
    assert calls == [("organization:acme", "price_acme_pro", "acct_acme")]


@pytest.mark.asyncio
async def test_delayed_active_event_projects_the_current_canceled_subscription() -> None:
    old_active = {
        "id": "sub_1",
        "customer": "cus_acme",
        "status": "active",
        "items": {"data": [{"price": {"id": "price_pro"}}]},
    }
    current_canceled = {**old_active, "status": "canceled"}
    projection = StripeSubscriptionProjection(
        PlanCatalog(Plan("pro", "price_pro")),
        webhook=StripeWebhookPolicy("2026-08-26.dahlia", False, "account"),
        subject_for=lambda customer, account: "organization:acme",
        retrieve_subscription=retriever(current_canceled),
    )

    snapshot = await projection.project(envelope("customer.subscription.updated", old_active))

    assert snapshot is not None
    assert snapshot.state.value == "canceled"


@pytest.mark.asyncio
async def test_direct_connect_subscription_retrieval_is_account_scoped() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"sub_1","customer":"cus_acme","status":"active"}',
            "1.1",
        )
    )
    backend = stripe(client, connect=StripeConnect(DirectCharges()))

    subscription = await backend.retrieve_subscription("sub_1", "acct_acme")

    assert subscription["id"] == "sub_1"
    assert client.options == {
        "target": "/v1/subscriptions/sub_1",
        "headers": (
            (b"authorization", b"Bearer rk_test_example"),
            (b"stripe-version", b"2026-08-26.dahlia"),
            (b"stripe-account", b"acct_acme"),
        ),
    }


@pytest.mark.asyncio
async def test_subscription_retrieval_refuses_invalid_id_before_io() -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))

    with pytest.raises(ValueError, match="subscription_id must be a sub_ identifier"):
        await stripe(client).retrieve_subscription("sub_1/../../customers", None)

    assert client.options == {}


@pytest.mark.asyncio
async def test_subscription_retrieval_failure_does_not_retain_response_body() -> None:
    secret = "secret-subscription-payload"
    client = Client(ClientResponse(500, (), secret.encode(), "1.1"))

    with pytest.raises(StripeError, match="HTTP status 500") as caught:
        await stripe(client).retrieve_subscription("sub_1", None)

    assert secret not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
async def test_direct_connect_checkout_retrieval_is_account_scoped() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"cs_1","mode":"payment","payment_status":"paid"}',
            "1.1",
        )
    )
    backend = stripe(client, connect=StripeConnect(DirectCharges()))

    session = await backend.retrieve_checkout("cs_1", "acct_acme")

    assert session["id"] == "cs_1"
    assert client.options == {
        "target": "/v1/checkout/sessions/cs_1",
        "headers": (
            (b"authorization", b"Bearer rk_test_example"),
            (b"stripe-version", b"2026-08-26.dahlia"),
            (b"stripe-account", b"acct_acme"),
        ),
    }


@pytest.mark.asyncio
async def test_subscription_projection_refuses_mismatched_current_resource() -> None:
    projection = StripeSubscriptionProjection(
        PlanCatalog(Plan("pro", "price_pro")),
        webhook=StripeWebhookPolicy("2026-08-26.dahlia", False, "account"),
        subject_for=lambda customer, account: "organization:acme",
        retrieve_subscription=retriever(
            {
                "id": "sub_other",
                "customer": "cus_acme",
                "status": "active",
                "items": {"data": [{"price": {"id": "price_pro"}}]},
            }
        ),
    )

    with pytest.raises(ValueError, match="id differs from webhook resource id"):
        await projection.project(envelope("customer.subscription.updated", {"id": "sub_1"}))


@pytest.mark.asyncio
async def test_destination_subscription_recovers_its_original_account_from_metadata() -> None:
    projection = StripeSubscriptionProjection(
        PlanCatalog(Plan("pro", "price_pro")),
        webhook=StripeWebhookPolicy("2026-08-26.dahlia", False, "account"),
        subject_for=lambda customer, account: "organization:acme",
        retrieve_subscription=retriever(
            {
                "id": "sub_1",
                "customer": "cus_acme",
                "status": "active",
                "metadata": {"wreath_merchant_account": "acct_destination"},
                "items": {"data": [{"price": {"id": "price_pro"}}]},
            }
        ),
    )

    snapshot = await projection.project(envelope("customer.subscription.updated", {"id": "sub_1"}))

    assert snapshot is not None
    assert snapshot.merchant_account == "acct_destination"


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [None, [], "sub_1"])
async def test_subscription_projection_requires_a_current_object(current: Any) -> None:
    projection = StripeSubscriptionProjection(
        PlanCatalog(Plan("pro", "price_pro")),
        webhook=StripeWebhookPolicy("2026-08-26.dahlia", False, "account"),
        subject_for=lambda customer, account: "organization:acme",
        retrieve_subscription=retriever(current),
    )

    with pytest.raises(TypeError, match="retrieve_subscription must return an object"):
        await projection.project(envelope("customer.subscription.updated", {"id": "sub_1"}))
