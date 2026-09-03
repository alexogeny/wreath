from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime
from typing import Any

import pytest

from wreath.billing import Billing, HostedRedirect, ProviderMerchant
from wreath.billing.providers.stripe import (
    DestinationCharges,
    DestinationRefunds,
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


def paid_invoice() -> dict[str, Any]:
    return {
        "id": "in_1",
        "customer": "cus_acme",
        "paid": True,
        "status": "paid",
        "parent": {
            "type": "subscription_details",
            "subscription_details": {
                "subscription": "sub_1",
                "metadata": {},
            },
        },
        "lines": {
            "data": [
                {
                    "parent": {
                        "subscription_item_details": {"subscription": "sub_1"},
                    },
                    "period": {"end": 1_801_267_200},
                }
            ],
            "has_more": False,
        },
    }


def invoice_with(path: tuple[str | int, ...], value: Any) -> dict[str, Any]:
    invoice = paid_invoice()
    target: Any = invoice
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return invoice


def invoice_projection(subject_for: Any = None) -> StripeInvoiceProjection:
    return StripeInvoiceProjection(
        webhook=StripeWebhookPolicy("2026-08-26.dahlia", False, "account"),
        subject_for=(
            (lambda customer, account: "organization:acme")
            if subject_for is None
            else subject_for
        ),
    )


def current_subscription() -> dict[str, Any]:
    return {
        "id": "sub_1",
        "customer": "cus_acme",
        "status": "active",
        "trial_end": 1_801_267_200,
        "items": {
            "data": [{"price": {"id": "price_pro"}}],
            "has_more": False,
        },
    }


def subscription_with(path: tuple[str | int, ...], value: Any) -> dict[str, Any]:
    subscription = current_subscription()
    target: Any = subscription
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return subscription


def subscription_projection(
    current: Any,
    *,
    subject_for: Any = None,
    plan_for_price: Any = None,
) -> StripeSubscriptionProjection:
    options = {} if plan_for_price is None else {"plan_for_price": plan_for_price}
    return StripeSubscriptionProjection(
        PlanCatalog(Plan("pro", "price_pro")),
        webhook=StripeWebhookPolicy("2026-08-26.dahlia", False, "account"),
        subject_for=(
            (lambda customer, account: "organization:acme")
            if subject_for is None
            else subject_for
        ),
        retrieve_subscription=retriever(current),
        **options,
    )


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
async def test_subscription_projection_preserves_a_valid_current_subscription() -> None:
    snapshot = await subscription_projection(current_subscription()).project(
        envelope("customer.subscription.updated", {"id": "sub_1"})
    )

    assert snapshot is not None
    assert snapshot.id == "sub_1"
    assert snapshot.subject == "organization:acme"
    assert snapshot.plan == "pro"
    assert snapshot.state.value == "active"
    assert snapshot.trial_ends_at == datetime.fromtimestamp(1_801_267_200, UTC)


@pytest.mark.asyncio
async def test_subscription_projection_ignores_other_events() -> None:
    assert (
        await subscription_projection(current_subscription()).project(
            envelope("invoice.paid", {"id": "sub_1"})
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"not-json", "not valid JSON"),
        (b"[]", "must be an object"),
        (
            json.dumps(
                {
                    "id": "evt_other",
                    "type": "customer.subscription.updated",
                    "api_version": "2026-08-26.dahlia",
                    "livemode": False,
                    "data": {"object": {"id": "sub_1"}},
                }
            ).encode(),
            "envelope differs",
        ),
        (
            json.dumps(
                {
                    "id": "evt_1",
                    "type": "customer.subscription.deleted",
                    "api_version": "2026-08-26.dahlia",
                    "livemode": False,
                    "data": {"object": {"id": "sub_1"}},
                }
            ).encode(),
            "envelope differs",
        ),
        (
            json.dumps(
                {
                    "id": "evt_1",
                    "type": "customer.subscription.updated",
                    "api_version": "2026-08-26.dahlia",
                    "livemode": False,
                    "data": None,
                }
            ).encode(),
            "missing data.object",
        ),
        (
            json.dumps(
                {
                    "id": "evt_1",
                    "type": "customer.subscription.updated",
                    "api_version": "2026-08-26.dahlia",
                    "livemode": False,
                    "data": {"object": {"id": ""}},
                }
            ).encode(),
            "invalid subscription id",
        ),
        (
            json.dumps(
                {
                    "id": "evt_1",
                    "type": "customer.subscription.updated",
                    "api_version": "2026-08-26.dahlia",
                    "livemode": False,
                    "data": {"object": {"id": 1}},
                }
            ).encode(),
            "invalid subscription id",
        ),
        (
            json.dumps(
                {
                    "id": "evt_1",
                    "type": "customer.subscription.updated",
                    "api_version": "2026-08-26.dahlia",
                    "livemode": False,
                    "data": {"object": {"id": "price_1"}},
                }
            ).encode(),
            "invalid subscription id",
        ),
    ],
)
async def test_subscription_projection_refuses_each_invalid_envelope_body(
    body: bytes, message: str
) -> None:
    current = envelope("customer.subscription.updated", {"id": "sub_1"})
    malformed = WebhookEnvelope(
        current.id,
        current.type,
        current.version,
        current.timestamp,
        current.content_type,
        body,
    )

    with pytest.raises(ValueError, match=message):
        await subscription_projection(current_subscription()).project(malformed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subscription", "message"),
    [
        (subscription_with(("customer",), ""), "missing customer id"),
        (subscription_with(("customer",), 1), "missing customer id"),
        (subscription_with(("items",), None), "no price items"),
        (subscription_with(("items", "has_more"), "false"), "has_more must be bool"),
        (subscription_with(("items", "data"), None), "no price items"),
        (subscription_with(("items", "data"), []), "no price items"),
        (subscription_with(("items", "data"), "entry"), "no price items"),
        (subscription_with(("items", "data"), [1]), "invalid price item"),
        (
            subscription_with(("items", "data", 0, "price"), None),
            "invalid price item",
        ),
        (
            subscription_with(("items", "data", 0, "price", "id"), ""),
            "invalid price item",
        ),
        (
            subscription_with(("items", "data", 0, "price", "id"), 1),
            "invalid price item",
        ),
        (
            subscription_with(
                ("items", "data"),
                [
                    {"price": {"id": "price_pro"}},
                    {"price": {"id": "price_other"}},
                ],
            ),
            "exactly one plan price",
        ),
    ],
)
async def test_subscription_projection_refuses_each_malformed_current_subscription(
    subscription: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await subscription_projection(subscription).project(
            envelope("customer.subscription.updated", {"id": "sub_1"})
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", ["", 1])
async def test_subscription_projection_refuses_each_invalid_subject_mapping(
    subject: object,
) -> None:
    with pytest.raises(KeyError, match="no billing subject mapping"):
        await subscription_projection(
            current_subscription(),
            subject_for=lambda customer, account: subject,
        ).project(envelope("customer.subscription.updated", {"id": "sub_1"}))


@pytest.mark.asyncio
async def test_subscription_projection_refuses_an_invalid_resolved_plan() -> None:
    with pytest.raises(TypeError, match="plan_for_price must return Plan"):
        await subscription_projection(
            current_subscription(),
            plan_for_price=lambda **options: object(),
        ).project(envelope("customer.subscription.updated", {"id": "sub_1"}))


def test_invoice_projection_preserves_a_valid_paid_invoice() -> None:
    payment = invoice_projection().project(envelope("invoice.paid", paid_invoice()))

    assert payment is not None
    assert payment.invoice == "in_1"
    assert payment.subscription == "sub_1"
    assert payment.subject == "organization:acme"
    assert payment.paid_through == datetime.fromtimestamp(1_801_267_200, UTC)


def test_invoice_projection_preserves_the_original_merchant_account() -> None:
    invoice = invoice_with(
        ("parent", "subscription_details", "metadata"),
        {"wreath_merchant_account": "acct_destination"},
    )

    payment = invoice_projection().project(envelope("invoice.paid", invoice))

    assert payment is not None
    assert payment.merchant_account == "acct_destination"


def test_invoice_projection_ignores_other_events() -> None:
    assert (
        invoice_projection().project(
            envelope("customer.subscription.updated", paid_invoice())
        )
        is None
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"not-json", "not valid JSON"),
        (b"[]", "must be an object"),
        (
            json.dumps(
                {
                    "id": "evt_other",
                    "type": "invoice.paid",
                    "api_version": "2026-08-26.dahlia",
                    "livemode": False,
                    "data": {"object": paid_invoice()},
                }
            ).encode(),
            "envelope differs",
        ),
        (
            json.dumps(
                {
                    "id": "evt_1",
                    "type": "invoice.payment_failed",
                    "api_version": "2026-08-26.dahlia",
                    "livemode": False,
                    "data": {"object": paid_invoice()},
                }
            ).encode(),
            "envelope differs",
        ),
        (
            json.dumps(
                {
                    "id": "evt_1",
                    "type": "invoice.paid",
                    "api_version": "2026-08-26.dahlia",
                    "livemode": False,
                    "data": None,
                }
            ).encode(),
            "missing data.object",
        ),
    ],
)
def test_invoice_projection_refuses_each_invalid_envelope_body(
    body: bytes, message: str
) -> None:
    current = envelope("invoice.paid", paid_invoice())
    malformed = WebhookEnvelope(
        current.id,
        current.type,
        current.version,
        current.timestamp,
        current.content_type,
        body,
    )

    with pytest.raises(ValueError, match=message):
        invoice_projection().project(malformed)


@pytest.mark.parametrize(
    ("invoice", "message"),
    [
        (invoice_with(("parent",), None), "missing subscription id"),
        (
            invoice_with(("parent", "subscription_details"), None),
            "missing subscription id",
        ),
        (invoice_with(("id",), ""), "missing invoice id"),
        (invoice_with(("id",), 1), "missing invoice id"),
        (invoice_with(("customer",), ""), "missing customer id"),
        (invoice_with(("customer",), 1), "missing customer id"),
        (
            invoice_with(("parent", "subscription_details", "subscription"), ""),
            "missing subscription id",
        ),
        (
            invoice_with(("parent", "subscription_details", "subscription"), 1),
            "missing subscription id",
        ),
        (invoice_with(("parent", "type"), "quote_details"), "parent must be"),
        (invoice_with(("paid",), False), "does not contain a paid invoice"),
        (invoice_with(("status",), "open"), "does not contain a paid invoice"),
        (invoice_with(("lines",), None), "no subscription line periods"),
        (invoice_with(("lines", "has_more"), "false"), "has_more must be bool"),
        (invoice_with(("lines", "data"), None), "no subscription line periods"),
        (invoice_with(("lines", "data"), []), "no subscription line periods"),
        (invoice_with(("lines", "data"), "entry"), "no subscription line periods"),
        (invoice_with(("lines", "data"), [1]), "invalid line"),
        (
            invoice_with(("lines", "data", 0, "parent"), None),
            "no matching subscription line periods",
        ),
        (
            invoice_with(
                (
                    "lines",
                    "data",
                    0,
                    "parent",
                    "subscription_item_details",
                    "subscription",
                ),
                "sub_other",
            ),
            "no matching subscription line periods",
        ),
        (
            invoice_with(("lines", "data", 0, "period"), None),
            "no matching subscription line periods",
        ),
        (
            invoice_with(("lines", "data", 0, "period", "end"), "tomorrow"),
            "Unix timestamp",
        ),
    ],
)
def test_invoice_projection_refuses_each_malformed_paid_invoice(
    invoice: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        invoice_projection().project(envelope("invoice.paid", invoice))


@pytest.mark.parametrize("subject", ["", 1])
def test_invoice_projection_refuses_each_invalid_subject_mapping(subject: object) -> None:
    with pytest.raises(KeyError, match="no billing subject mapping"):
        invoice_projection(lambda customer, account: subject).project(
            envelope("invoice.paid", paid_invoice())
        )


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
async def test_stripe_retrieval_identifiers_are_exact_before_io() -> None:
    for operation, invalid in (
        ("retrieve_subscription", ""),
        ("retrieve_subscription", 1),
        ("retrieve_checkout", ""),
        ("retrieve_checkout", 1),
    ):
        client = Client(ClientResponse(200, (), b"{}", "1.1"))
        with pytest.raises(ValueError, match="must be a .*_ identifier"):
            await getattr(stripe(client), operation)(invalid, None)
        assert client.options == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["retrieve_subscription", "retrieve_checkout"])
async def test_stripe_retrieval_requires_direct_connect_for_an_account(
    operation: str,
) -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))
    identifier = "sub_1" if operation == "retrieve_subscription" else "cs_1"

    with pytest.raises(ValueError, match="merchant_account requires direct Connect"):
        await getattr(stripe(client), operation)(identifier, "acct_acme")
    assert client.options == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "identifier"),
    [("retrieve_subscription", "sub_1"), ("retrieve_checkout", "cs_1")],
)
async def test_destination_connect_retrieval_uses_the_platform_account(
    operation: str, identifier: str
) -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))
    backend = stripe(
        client,
        connect=StripeConnect(DestinationCharges(DestinationRefunds(False, False))),
    )

    assert await getattr(backend, operation)(identifier, None) == {}
    assert b"stripe-account" not in dict(client.options["headers"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "identifier", "response", "message"),
    [
        (
            "retrieve_subscription",
            "sub_1",
            ClientResponse(199, (), b"{}", "1.1"),
            "HTTP status 199",
        ),
        (
            "retrieve_subscription",
            "sub_1",
            ClientResponse(200, (), b"not-json", "1.1"),
            "invalid JSON",
        ),
        (
            "retrieve_subscription",
            "sub_1",
            ClientResponse(200, (), b"[]", "1.1"),
            "non-object response",
        ),
        (
            "retrieve_checkout",
            "cs_1",
            ClientResponse(199, (), b"{}", "1.1"),
            "HTTP status 199",
        ),
        (
            "retrieve_checkout",
            "cs_1",
            ClientResponse(300, (), b"{}", "1.1"),
            "HTTP status 300",
        ),
        (
            "retrieve_checkout",
            "cs_1",
            ClientResponse(200, (), b"not-json", "1.1"),
            "invalid JSON",
        ),
        (
            "retrieve_checkout",
            "cs_1",
            ClientResponse(200, (), b"[]", "1.1"),
            "non-object response",
        ),
    ],
)
async def test_stripe_retrieval_refuses_each_invalid_response(
    operation: str,
    identifier: str,
    response: ClientResponse,
    message: str,
) -> None:
    with pytest.raises(StripeError, match=message):
        await getattr(stripe(Client(response)), operation)(identifier, None)


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
