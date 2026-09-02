from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from wreath.billing.providers.stripe import (
    DestinationCharges,
    DestinationRefunds,
    DirectCharges,
    SeparateChargesAndTransfers,
    StripeBilling,
    StripeConnect,
    StripeError,
    StripeInvoiceProjection,
    StripeSubscriptionProjection,
    StripeWebhookPolicy,
)
from wreath.config import Secret
from wreath.http_client import ClientResponse
from wreath.payments import (
    CheckoutItem,
    CheckoutRequest,
    Money,
    PortalRequest,
    PortalSession,
    Refund,
    RefundRequest,
    RefundState,
)
from wreath.subscriptions import Plan, PlanCatalog, SubscriptionState
from wreath.webhooks import WebhookEnvelope


class Client:
    def __init__(self, *responses: ClientResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def post(
        self,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
        body: bytes = b"",
        idempotency_key: str | None = None,
    ) -> ClientResponse:
        self.calls.append(
            {
                "target": target,
                "headers": headers,
                "body": body,
                "idempotency_key": idempotency_key,
            }
        )
        return self.responses.pop(0)

    async def get(
        self,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
    ) -> ClientResponse:
        self.calls.append({"target": target, "headers": headers})
        return self.responses.pop(0)


def stripe(client: Client, **options: Any) -> StripeBilling:
    api_version = options.pop("api_version", "2026-08-26.dahlia")
    return StripeBilling(
        client=client,
        api_key=Secret("rk_test_example"),
        api_version=api_version,
        allowed_return_origins=("https://app.example",),
        **options,
    )


def retriever(value: dict[str, Any]) -> Any:
    async def retrieve(subscription_id: str, account: str | None) -> dict[str, Any]:
        return value

    return retrieve


def test_stripe_liability_and_mode_configuration_is_exact() -> None:
    integer_true: Any = 1
    string_false: Any = "false"
    unknown_charges: Any = object()
    separate_charges: Any = SeparateChargesAndTransfers()
    with pytest.raises(TypeError, match="reverse_transfer must be bool"):
        DestinationRefunds(integer_true, False)
    with pytest.raises(TypeError, match="refund_application_fee must be bool"):
        DirectCharges(refund_application_fee=string_false)
    with pytest.raises(TypeError, match="on_behalf_of must be bool"):
        DestinationCharges(refunds=DestinationRefunds(False, False), on_behalf_of=integer_true)
    with pytest.raises(TypeError, match="Managed Payments enabled flag must be bool"):
        stripe(Client(), managed_payments=integer_true)
    with pytest.raises(TypeError, match="DirectCharges or DestinationCharges"):
        StripeConnect(unknown_charges)
    with pytest.raises(
        ValueError, match="separate charges and transfers require a settlement ledger"
    ):
        StripeConnect(separate_charges)


@pytest.mark.asyncio
async def test_stripe_rejects_an_undeclared_https_return_origin_before_io() -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro"),),
        mode="subscription",
        success_url="https://evil.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JEVILRETURN",
    )

    with pytest.raises(ValueError, match="success_url uses undeclared return origin"):
        await stripe(client).create_checkout(request, idempotency_key=request.reference)
    assert client.calls == []


@pytest.mark.asyncio
async def test_connect_fee_mode_mismatch_refuses_before_io() -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_credits"),),
        mode="payment",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JFEEMISMATCH",
        merchant_account="acct_direct",
    )
    backend = stripe(
        client,
        connect=StripeConnect(DirectCharges(application_fee_percent="10")),
    )

    with pytest.raises(ValueError, match="use application_fee_amount for payment mode"):
        await backend.create_checkout(request, idempotency_key=request.reference)
    assert client.calls == []


@pytest.mark.asyncio
async def test_destination_one_time_checkout_uses_payment_intent_routing() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"cs_payment","url":"https://checkout.stripe.com/c/pay/one-time"}',
            "1.1",
        )
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_credits"),),
        mode="payment",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JDESTINATIONPAYMENT",
        merchant_account="acct_destination",
    )
    backend = stripe(
        client,
        connect=StripeConnect(
            DestinationCharges(
                application_fee_amount=125,
                on_behalf_of=True,
                refunds=DestinationRefunds(True, True),
            )
        ),
    )

    await backend.create_checkout(request, idempotency_key=request.reference)

    body = client.calls[0]["body"]
    assert b"payment_intent_data%5Bapplication_fee_amount%5D=125" in body
    assert b"payment_intent_data%5Btransfer_data%5D%5Bdestination%5D=acct_destination" in body
    assert b"payment_intent_data%5Bon_behalf_of%5D=acct_destination" in body
    assert b"subscription_data" not in body


@pytest.mark.asyncio
async def test_customer_portal_is_host_validated_and_idempotent() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"bps_1","url":"https://billing.stripe.com/p/session/live_1"}',
            "1.1",
        )
    )
    request = PortalRequest(
        subject="organization:acme",
        customer="cus_acme",
        return_url="https://app.example/settings/billing",
        reference="01JPORTALSESSION",
    )

    session = await stripe(client).create_portal(request, idempotency_key=request.reference)

    assert session == PortalSession(
        provider="stripe",
        id="bps_1",
        url="https://billing.stripe.com/p/session/live_1",
    )
    assert client.calls == [
        {
            "target": "/v1/billing_portal/sessions",
            "headers": (
                (b"authorization", b"Bearer rk_test_example"),
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"stripe-version", b"2026-08-26.dahlia"),
            ),
            "body": (
                b"customer=cus_acme&return_url=https%3A%2F%2Fapp.example%2Fsettings%2Fbilling"
            ),
            "idempotency_key": "01JPORTALSESSION",
        }
    ]


@pytest.mark.asyncio
async def test_direct_connect_portal_is_scoped_to_the_connected_account() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"bps_1","url":"https://billing.stripe.com/p/session/direct"}',
            "1.1",
        )
    )
    request = PortalRequest(
        subject="organization:acme",
        customer="cus_acme",
        return_url="https://app.example/settings/billing",
        reference="01JDIRECTPORTAL",
        merchant_account="acct_direct",
    )

    await stripe(client, connect=StripeConnect(DirectCharges())).create_portal(
        request, idempotency_key=request.reference
    )

    assert dict(client.calls[0]["headers"])[b"stripe-account"] == b"acct_direct"


@pytest.mark.asyncio
async def test_destination_portal_uses_declared_settlement_merchant() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"bps_1","url":"https://billing.stripe.com/p/session/destination"}',
            "1.1",
        )
    )
    request = PortalRequest(
        subject="organization:acme",
        customer="cus_acme",
        return_url="https://app.example/settings/billing",
        reference="01JDESTINATIONPORTAL",
        merchant_account="acct_destination",
    )
    backend = stripe(
        client,
        connect=StripeConnect(
            DestinationCharges(refunds=DestinationRefunds(False, False), on_behalf_of=True)
        ),
    )

    await backend.create_portal(request, idempotency_key=request.reference)

    assert b"stripe-account" not in dict(client.calls[0]["headers"])
    assert client.calls[0]["body"].endswith(b"on_behalf_of=acct_destination")


@pytest.mark.asyncio
async def test_portal_refuses_a_provider_url_on_a_lookalike_host() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"bps_1","url":"https://billing.stripe.com.evil.example/session"}',
            "1.1",
        )
    )
    request = PortalRequest(
        subject="organization:acme",
        customer="cus_acme",
        return_url="https://app.example/settings/billing",
        reference="01JEVILPORTAL",
    )

    with pytest.raises(ValueError, match="must use billing.stripe.com"):
        await stripe(client).create_portal(request, idempotency_key=request.reference)


def test_managed_payments_supports_customer_portal() -> None:
    assert (
        stripe(
            Client(),
            managed_payments=True,
        ).capabilities.hosted_portal
        is True
    )


@pytest.mark.asyncio
async def test_destination_refund_makes_liability_choices_explicit() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"re_1","status":"succeeded","amount":1200,"currency":"usd"}',
            "1.1",
        )
    )
    request = RefundRequest(
        payment="pi_1",
        reference="01JREFUND",
        amount=Money("USD", 1200),
        merchant_account="acct_destination",
    )

    refund = await stripe(
        client,
        connect=StripeConnect(
            DestinationCharges(
                refunds=DestinationRefunds(
                    reverse_transfer=True,
                    refund_application_fee=True,
                )
            )
        ),
    ).create_refund(request, idempotency_key=request.reference)

    assert refund == Refund(
        provider="stripe",
        id="re_1",
        state=RefundState.SUCCEEDED,
        amount=Money("USD", 1200),
    )
    assert client.calls[0]["body"] == (
        b"payment_intent=pi_1&amount=1200&reverse_transfer=true&refund_application_fee=true"
    )
    assert b"stripe-account" not in dict(client.calls[0]["headers"])


@pytest.mark.asyncio
async def test_direct_refund_is_scoped_to_the_connected_account() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"re_1","status":"pending","amount":1200,"currency":"usd"}',
            "1.1",
        )
    )
    request = RefundRequest(
        payment="pi_1",
        reference="01JDIRECTREFUND",
        merchant_account="acct_direct",
    )

    refund = await stripe(
        client,
        connect=StripeConnect(DirectCharges(refund_application_fee=True)),
    ).create_refund(request, idempotency_key=request.reference)

    assert refund.state is RefundState.PENDING
    assert dict(client.calls[0]["headers"])[b"stripe-account"] == b"acct_direct"
    assert client.calls[0]["body"] == (b"payment_intent=pi_1&refund_application_fee=true")


@pytest.mark.asyncio
async def test_stripe_errors_never_include_provider_response_bodies() -> None:
    client = Client(
        ClientResponse(
            402,
            (),
            b'{"error":{"message":"secret customer detail"}}',
            "1.1",
        )
    )
    request = RefundRequest(payment="pi_1", reference="01JFAILEDREFUND")

    with pytest.raises(StripeError, match="HTTP status 402") as caught:
        await stripe(client).create_refund(request, idempotency_key=request.reference)
    assert "secret customer detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_subscription_webhook_projection_uses_declared_customer_mapping() -> None:
    body = json.dumps(
        {
            "id": "evt_1",
            "type": "customer.subscription.updated",
            "api_version": "2026-08-26.dahlia",
            "livemode": False,
            "account": "acct_direct",
            "data": {
                "object": {
                    "id": "sub_1",
                    "customer": "cus_acme",
                    "status": "active",
                    "current_period_end": 1_801_267_200,
                    "items": {"data": [{"price": {"id": "price_pro"}}]},
                }
            },
        }
    ).encode()
    envelope = WebhookEnvelope(
        id="evt_1",
        type="customer.subscription.updated",
        version="2026-08-26.dahlia",
        timestamp=datetime(2026, 9, 2, tzinfo=UTC),
        content_type="application/json",
        body=body,
    )
    calls: list[tuple[str, str | None]] = []

    def subject_for(customer: str, account: str | None) -> str | None:
        calls.append((customer, account))
        return "organization:acme"

    catalog = PlanCatalog(Plan("pro", "price_pro"))
    projection = StripeSubscriptionProjection(
        catalog,
        webhook=StripeWebhookPolicy(
            event_version="2026-08-26.dahlia",
            livemode=False,
            scope="connected_accounts",
        ),
        subject_for=subject_for,
        plan_for_price=lambda **_lookup: catalog["pro"],
        retrieve_subscription=retriever(json.loads(body)["data"]["object"]),
    )

    snapshot = await projection.project(envelope)

    assert snapshot is not None
    assert snapshot.provider == "stripe"
    assert snapshot.id == "sub_1"
    assert snapshot.subject == "organization:acme"
    assert snapshot.plan == "pro"
    assert snapshot.state is SubscriptionState.ACTIVE
    assert snapshot.paid_through is None
    assert calls == [("cus_acme", "acct_direct")]


@pytest.mark.asyncio
async def test_subscription_projection_refuses_unknown_customer_and_price_mappings() -> None:
    def envelope(price: str) -> WebhookEnvelope:
        body = json.dumps(
            {
                "id": "evt_1",
                "type": "customer.subscription.updated",
                "api_version": "2026-08-26.dahlia",
                "livemode": False,
                "data": {
                    "object": {
                        "id": "sub_1",
                        "customer": "cus_unknown",
                        "status": "active",
                        "items": {"data": [{"price": {"id": price}}]},
                    }
                },
            }
        ).encode()
        return WebhookEnvelope(
            "evt_1",
            "customer.subscription.updated",
            "2026-08-26.dahlia",
            datetime(2026, 9, 2, tzinfo=UTC),
            "application/json",
            body,
        )

    catalog = PlanCatalog(Plan("pro", "price_pro"))
    known_price_event = envelope("price_pro")
    unknown_price_event = envelope("price_unknown")
    without_customer = StripeSubscriptionProjection(
        catalog,
        webhook=StripeWebhookPolicy(
            event_version="2026-08-26.dahlia", livemode=False, scope="account"
        ),
        subject_for=lambda customer, account: None,
        retrieve_subscription=retriever(json.loads(known_price_event.body)["data"]["object"]),
    )
    with_customer = StripeSubscriptionProjection(
        catalog,
        webhook=StripeWebhookPolicy(
            event_version="2026-08-26.dahlia", livemode=False, scope="account"
        ),
        subject_for=lambda customer, account: "organization:acme",
        retrieve_subscription=retriever(json.loads(unknown_price_event.body)["data"]["object"]),
    )

    with pytest.raises(KeyError, match="no billing subject mapping for Stripe customer"):
        await without_customer.project(known_price_event)
    with pytest.raises(KeyError, match="unknown provider price 'price_unknown'"):
        await with_customer.project(unknown_price_event)


@pytest.mark.asyncio
async def test_subscription_projection_refuses_webhook_environment_mismatch() -> None:
    def envelope(*, event_version: str, livemode: bool, account: str | None) -> WebhookEnvelope:
        payload: dict[str, Any] = {
            "id": "evt_1",
            "type": "customer.subscription.updated",
            "api_version": event_version,
            "livemode": livemode,
            "data": {
                "object": {
                    "id": "sub_1",
                    "customer": "cus_acme",
                    "status": "active",
                    "items": {"data": [{"price": {"id": "price_pro"}}]},
                }
            },
        }
        if account is not None:
            payload["account"] = account
        return WebhookEnvelope(
            "evt_1",
            "customer.subscription.updated",
            event_version,
            datetime(2026, 9, 2, tzinfo=UTC),
            "application/json",
            json.dumps(payload).encode(),
        )

    catalog = PlanCatalog(Plan("pro", "price_pro"))

    async def unexpected_retrieval(subscription_id: str, account: str | None) -> dict[str, Any]:
        raise AssertionError("invalid webhook reached authoritative retrieval")

    projection = StripeSubscriptionProjection(
        catalog,
        webhook=StripeWebhookPolicy(
            event_version="2026-08-26.dahlia",
            livemode=False,
            scope="connected_accounts",
        ),
        subject_for=lambda customer, account: "organization:acme",
        plan_for_price=lambda **_lookup: catalog["pro"],
        retrieve_subscription=unexpected_retrieval,
    )

    with pytest.raises(ValueError, match="event version"):
        await projection.project(
            envelope(event_version="2025-12-15.clover", livemode=False, account="acct_acme")
        )
    with pytest.raises(ValueError, match="livemode"):
        await projection.project(
            envelope(event_version="2026-08-26.dahlia", livemode=True, account="acct_acme")
        )
    with pytest.raises(ValueError, match="connected-account webhook requires account"):
        await projection.project(
            envelope(event_version="2026-08-26.dahlia", livemode=False, account=None)
        )

    account_projection = StripeSubscriptionProjection(
        PlanCatalog(Plan("pro", "price_pro")),
        webhook=StripeWebhookPolicy(
            event_version="2026-08-26.dahlia", livemode=False, scope="account"
        ),
        subject_for=lambda customer, account: "organization:acme",
        retrieve_subscription=unexpected_retrieval,
    )
    with pytest.raises(ValueError, match="account webhook must not carry connected account"):
        await account_projection.project(
            envelope(event_version="2026-08-26.dahlia", livemode=False, account="acct_acme")
        )


def test_invoice_paid_is_the_event_that_advances_subscription_payment_truth() -> None:
    body = json.dumps(
        {
            "id": "evt_invoice_paid",
            "type": "invoice.paid",
            "api_version": "2026-08-26.dahlia",
            "livemode": True,
            "data": {
                "object": {
                    "id": "in_1",
                    "customer": "cus_acme",
                    "status": "paid",
                    "paid": True,
                    "parent": {
                        "type": "subscription_details",
                        "subscription_details": {
                            "subscription": "sub_1",
                            "metadata": {"wreath_merchant_account": "acct_destination"},
                        },
                    },
                    "lines": {
                        "data": [
                            {
                                "period": {"start": 1_798_675_200, "end": 1_801_267_200},
                                "parent": {
                                    "type": "subscription_item_details",
                                    "subscription_item_details": {"subscription": "sub_1"},
                                },
                            }
                        ]
                    },
                }
            },
        }
    ).encode()
    envelope = WebhookEnvelope(
        "evt_invoice_paid",
        "invoice.paid",
        "2026-08-26.dahlia",
        datetime(2026, 9, 2, tzinfo=UTC),
        "application/json",
        body,
    )
    projection = StripeInvoiceProjection(
        webhook=StripeWebhookPolicy(
            event_version="2026-08-26.dahlia", livemode=True, scope="account"
        ),
        subject_for=lambda customer, account: "organization:acme",
    )

    payment = projection.project(envelope)

    assert payment is not None
    assert payment.provider == "stripe"
    assert payment.invoice == "in_1"
    assert payment.subscription == "sub_1"
    assert payment.subject == "organization:acme"
    assert payment.merchant_account == "acct_destination"
    assert payment.paid_through == datetime.fromtimestamp(1_801_267_200, UTC)
