from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

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
from wreath.billing.stripe_webhooks import bind_stripe_webhooks
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
from wreath.request import Request
from wreath.subscriptions import Plan, PlanCatalog, SubscriptionState
from wreath.webhooks import (
    PostgresWebhookInbox,
    StripeWebhookVerifier,
    WebhookContext,
    WebhookEnvelope,
    WebhookLimits,
    WebhookSource,
)


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


def test_subscription_and_invoice_projection_configuration_is_exact() -> None:
    catalog = PlanCatalog(Plan("pro", "price_pro"))
    account_webhook = StripeWebhookPolicy(
        event_version="2026-08-26.dahlia",
        livemode=False,
        scope="account",
    )
    connected_webhook = StripeWebhookPolicy(
        event_version="2026-08-26.dahlia",
        livemode=False,
        scope="connected_accounts",
    )
    retrieve = retriever({})

    with pytest.raises(TypeError, match="subject_for must be callable"):
        StripeSubscriptionProjection(
            catalog,
            webhook=account_webhook,
            subject_for=None,
            retrieve_subscription=retrieve,
        )
    for invalid in (None, lambda subscription, account: {}):
        with pytest.raises(TypeError, match="retrieve_subscription must be async callable"):
            StripeSubscriptionProjection(
                catalog,
                webhook=account_webhook,
                subject_for=lambda customer, account: "organization:acme",
                retrieve_subscription=invalid,
            )
    with pytest.raises(TypeError, match="plan_for_price must be callable"):
        StripeSubscriptionProjection(
            catalog,
            webhook=account_webhook,
            subject_for=lambda customer, account: "organization:acme",
            retrieve_subscription=retrieve,
            plan_for_price=1,
        )
    with pytest.raises(TypeError, match="requires plan_for_price"):
        StripeSubscriptionProjection(
            catalog,
            webhook=connected_webhook,
            subject_for=lambda customer, account: "organization:acme",
            retrieve_subscription=retrieve,
        )
    with pytest.raises(TypeError, match="invoice subject_for must be callable"):
        StripeInvoiceProjection(webhook=account_webhook, subject_for=None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (1_801_267_200, datetime.fromtimestamp(1_801_267_200, UTC)),
    ],
)
def test_subscription_timestamp_preserves_absence_and_valid_unix_time(
    value: int | None, expected: datetime | None
) -> None:
    assert StripeSubscriptionProjection._timestamp(value, "trial_end") == expected


@pytest.mark.parametrize("value", [True, 1.5, "1801267200", 10**30])
def test_subscription_timestamp_refuses_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="Unix timestamp"):
        StripeSubscriptionProjection._timestamp(value, "trial_end")


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
@pytest.mark.parametrize("operation", ["checkout", "portal"])
async def test_platform_stripe_refuses_ignored_merchant_account_context(
    operation: str,
) -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))
    backend = stripe(client)

    with pytest.raises(ValueError, match="merchant_account requires Stripe Connect"):
        if operation == "checkout":
            await backend.create_checkout(
                CheckoutRequest(
                    subject="organization:acme",
                    items=(CheckoutItem("price_pro"),),
                    mode="subscription",
                    success_url="https://app.example/success",
                    cancel_url="https://app.example/cancel",
                    reference="01JACCOUNTCONFUSION",
                    merchant_account="acct_unexpected",
                ),
                idempotency_key="01JACCOUNTCONFUSION",
            )
        else:
            await backend.create_portal(
                PortalRequest(
                    subject="organization:acme",
                    customer="cus_acme",
                    return_url="https://app.example/billing",
                    reference="01JACCOUNTCONFUSION",
                    merchant_account="acct_unexpected",
                ),
                idempotency_key="01JACCOUNTCONFUSION",
            )

    assert client.calls == []


@pytest.mark.asyncio
async def test_platform_destination_portal_refuses_ignored_merchant_account_context() -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))
    backend = stripe(
        client,
        connect=StripeConnect(
            DestinationCharges(refunds=DestinationRefunds(False, False))
        ),
    )

    with pytest.raises(ValueError, match="merchant_account"):
        await backend.create_portal(
            PortalRequest(
                subject="organization:acme",
                customer="cus_acme",
                return_url="https://app.example/billing",
                reference="01JACCOUNTCONFUSION",
                merchant_account="acct_unexpected",
            ),
            idempotency_key="01JACCOUNTCONFUSION",
        )

    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "idempotency_key",
    ["", "line\r\nbreak", "snowman-\u2603", "x" * 256],
)
async def test_stripe_refuses_invalid_idempotency_keys_before_io(
    idempotency_key: str,
) -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))

    with pytest.raises(ValueError, match="idempotency_key"):
        await stripe(client).create_refund(
            RefundRequest("pi_1", "01JREFUND"),
            idempotency_key=idempotency_key,
        )

    assert client.calls == []


@pytest.mark.asyncio
async def test_stripe_accepts_the_maximum_idempotency_key_length() -> None:
    key = "x" * 255
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"re_1","status":"succeeded","amount":1,"currency":"usd"}',
            "1.1",
        )
    )

    await stripe(client).create_refund(
        RefundRequest("pi_1", "01JREFUND"),
        idempotency_key=key,
    )

    assert client.calls[0]["idempotency_key"] == key


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["checkout", "portal", "refund"])
async def test_stripe_refuses_provider_response_ids_from_another_resource(
    operation: str,
) -> None:
    payloads = {
        "checkout": {
            "id": "bps_wrong",
            "url": "https://checkout.stripe.com/c/pay/1",
        },
        "portal": {
            "id": "cs_wrong",
            "url": "https://billing.stripe.com/p/session/1",
        },
        "refund": {
            "id": "pi_wrong",
            "status": "succeeded",
            "amount": 1,
            "currency": "usd",
        },
    }
    client = Client(
        ClientResponse(200, (), json.dumps(payloads[operation]).encode(), "1.1")
    )
    backend = stripe(client)

    with pytest.raises(StripeError, match="missing id"):
        if operation == "checkout":
            await backend.create_checkout(
                CheckoutRequest(
                    subject="organization:acme",
                    items=(CheckoutItem("price_pro"),),
                    mode="subscription",
                    success_url="https://app.example/success",
                    cancel_url="https://app.example/cancel",
                    reference="01JRESOURCECONFUSION",
                ),
                idempotency_key="01JRESOURCECONFUSION",
            )
        elif operation == "portal":
            await backend.create_portal(
                PortalRequest(
                    subject="organization:acme",
                    customer="cus_acme",
                    return_url="https://app.example/billing",
                    reference="01JRESOURCECONFUSION",
                ),
                idempotency_key="01JRESOURCECONFUSION",
            )
        else:
            await backend.create_refund(
                RefundRequest("pi_1", "01JRESOURCECONFUSION"),
                idempotency_key="01JRESOURCECONFUSION",
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_account", [cast(Any, 1), "cus_wrong"])
async def test_stripe_connect_refuses_an_invalid_merchant_account_before_io(
    merchant_account: Any,
) -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro"),),
        mode="subscription",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JBADACCOUNT",
        merchant_account=merchant_account,
    )

    with pytest.raises(ValueError, match="acct_ identifier"):
        await stripe(client, connect=StripeConnect(DirectCharges())).create_checkout(
            request,
            idempotency_key=request.reference,
        )

    assert client.calls == []


def test_stripe_refuses_an_application_fee_beyond_its_amount_range() -> None:
    with pytest.raises(ValueError, match="application_fee_amount"):
        DirectCharges(application_fee_amount=100_000_000)


@pytest.mark.asyncio
async def test_stripe_refuses_a_refund_beyond_its_amount_range_before_io() -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))

    with pytest.raises(ValueError, match="refund amount"):
        await stripe(client).create_refund(
            RefundRequest("pi_1", "01JREFUND", Money("USD", 100_000_000)),
            idempotency_key="01JREFUND",
        )

    assert client.calls == []


@pytest.mark.asyncio
async def test_stripe_refuses_a_provider_refund_beyond_its_amount_range() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"re_1","status":"succeeded","amount":100000000,"currency":"usd"}',
            "1.1",
        )
    )

    with pytest.raises(StripeError, match="invalid amount"):
        await stripe(client).create_refund(
            RefundRequest("pi_1", "01JREFUND"),
            idempotency_key="01JREFUND",
        )


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
async def test_connect_subscription_amount_mismatch_refuses_before_io() -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro"),),
        mode="subscription",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JFEESUBMISMATCH",
        merchant_account="acct_direct",
    )
    backend = stripe(
        client,
        connect=StripeConnect(DirectCharges(application_fee_amount=125)),
    )

    with pytest.raises(ValueError, match="use application_fee_percent for subscription mode"):
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
@pytest.mark.parametrize(
    ("mode", "charges", "required", "forbidden"),
    [
        (
            "payment",
            DestinationCharges(
                refunds=DestinationRefunds(False, False),
                application_fee_percent="10",
                application_fee_amount=125,
            ),
            b"payment_intent_data%5Bapplication_fee_amount%5D=125",
            b"subscription_data%5Bapplication_fee_percent%5D",
        ),
        (
            "subscription",
            DestinationCharges(
                refunds=DestinationRefunds(False, False),
                application_fee_percent="10",
                application_fee_amount=125,
            ),
            b"subscription_data%5Bapplication_fee_percent%5D=10",
            b"payment_intent_data%5Bapplication_fee_amount%5D",
        ),
        (
            "payment",
            DestinationCharges(refunds=DestinationRefunds(False, False)),
            b"transfer_data",
            b"application_fee",
        ),
        (
            "subscription",
            DestinationCharges(refunds=DestinationRefunds(False, False)),
            b"transfer_data",
            b"application_fee",
        ),
    ],
)
async def test_destination_checkout_fee_routing_is_exact(
    mode: str,
    charges: DestinationCharges,
    required: bytes,
    forbidden: bytes,
) -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"cs_1","url":"https://checkout.stripe.com/c/pay/destination"}',
            "1.1",
        )
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro"),),
        mode=mode,
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference=f"01JDESTINATION{mode.upper()}",
        merchant_account="acct_destination",
    )

    await stripe(client, connect=StripeConnect(charges)).create_checkout(
        request, idempotency_key=request.reference
    )

    body = client.calls[0]["body"]
    assert required in body
    assert forbidden not in body
    assert b"on_behalf_of" not in body


@pytest.mark.asyncio
async def test_direct_one_time_checkout_sends_the_application_fee_to_payment_intent() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"cs_payment","url":"https://checkout.stripe.com/c/pay/direct"}',
            "1.1",
        )
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_credits"),),
        mode="payment",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JDIRECTPAYMENT",
        merchant_account="acct_direct",
    )
    backend = stripe(
        client,
        connect=StripeConnect(DirectCharges(application_fee_amount=125)),
    )

    await backend.create_checkout(request, idempotency_key=request.reference)

    call = client.calls[0]
    assert dict(call["headers"])[b"stripe-account"] == b"acct_direct"
    assert b"payment_intent_data%5Bapplication_fee_amount%5D=125" in call["body"]
    assert b"subscription_data" not in call["body"]


@pytest.mark.asyncio
async def test_direct_payment_with_both_fee_shapes_uses_only_amount() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"cs_payment","url":"https://checkout.stripe.com/c/pay/direct"}',
            "1.1",
        )
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_credits"),),
        mode="payment",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JDIRECTBOTHFEES",
        merchant_account="acct_direct",
    )

    await stripe(
        client,
        connect=StripeConnect(
            DirectCharges(application_fee_percent="10", application_fee_amount=125)
        ),
    ).create_checkout(request, idempotency_key=request.reference)

    body = client.calls[0]["body"]
    assert b"payment_intent_data%5Bapplication_fee_amount%5D=125" in body
    assert b"subscription_data%5Bapplication_fee_percent%5D" not in body


@pytest.mark.asyncio
async def test_direct_one_time_checkout_without_a_fee_emits_no_fee_field() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"cs_payment","url":"https://checkout.stripe.com/c/pay/direct"}',
            "1.1",
        )
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_credits"),),
        mode="payment",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JDIRECTNOFEE",
        merchant_account="acct_direct",
    )

    await stripe(
        client,
        connect=StripeConnect(DirectCharges()),
    ).create_checkout(request, idempotency_key=request.reference)

    assert b"application_fee" not in client.calls[0]["body"]


@pytest.mark.asyncio
async def test_direct_subscription_without_a_fee_emits_no_fee_field() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"cs_subscription","url":"https://checkout.stripe.com/c/pay/direct"}',
            "1.1",
        )
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro"),),
        mode="subscription",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JDIRECTSUBNOFEE",
        merchant_account="acct_direct",
    )

    await stripe(
        client,
        connect=StripeConnect(DirectCharges()),
    ).create_checkout(request, idempotency_key=request.reference)

    assert b"application_fee" not in client.calls[0]["body"]


@pytest.mark.asyncio
async def test_direct_subscription_does_not_emit_the_payment_fee_field() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"cs_subscription","url":"https://checkout.stripe.com/c/pay/direct"}',
            "1.1",
        )
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro"),),
        mode="subscription",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JDIRECTSUBSCRIPTION",
        merchant_account="acct_direct",
    )
    backend = stripe(
        client,
        connect=StripeConnect(
            DirectCharges(application_fee_percent="10")
        ),
    )

    await backend.create_checkout(request, idempotency_key=request.reference)

    body = client.calls[0]["body"]
    assert b"subscription_data%5Bapplication_fee_percent%5D=10" in body
    assert b"payment_intent_data%5Bapplication_fee_amount%5D" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "error", "message"),
    [
        (
            {"id": None, "url": "https://checkout.stripe.com/c/pay/1"},
            StripeError,
            "missing id",
        ),
        (
            {"id": "", "url": "https://checkout.stripe.com/c/pay/1"},
            StripeError,
            "missing id",
        ),
        (
            {"id": 1, "url": "https://checkout.stripe.com/c/pay/1"},
            StripeError,
            "missing id",
        ),
        ({"id": "cs_1", "url": None}, StripeError, "missing url"),
        ({"id": "cs_1", "url": 1}, StripeError, "missing url"),
        (
            {"id": "cs_1", "url": "http://checkout.stripe.com/c/pay/1"},
            ValueError,
            "over HTTPS",
        ),
        (
            {"id": "cs_1", "url": "https://user@checkout.stripe.com/c/pay/1"},
            ValueError,
            "over HTTPS",
        ),
        (
            {"id": "cs_1", "url": "https://checkout.stripe.com:444/c/pay/1"},
            ValueError,
            "over HTTPS",
        ),
        (
            {"id": "cs_1", "url": "https://checkout.stripe.com:bad/c/pay/1"},
            ValueError,
            "over HTTPS",
        ),
        (
            {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/1#attacker"},
            ValueError,
            "over HTTPS",
        ),
        (
            {"id": "cs_1", "url": "https://checkout.stripe.com\r\n/c/pay/1"},
            ValueError,
            "over HTTPS",
        ),
        (
            {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/\u0085attacker"},
            ValueError,
            "over HTTPS",
        ),
        (
            {
                "id": "cs_1",
                "url": "https://checkout.stripe.com/c/pay/1",
                "expires_at": True,
            },
            StripeError,
            "expires_at must be a timestamp",
        ),
    ],
)
async def test_checkout_refuses_each_invalid_provider_response(
    payload: dict[str, Any], error: type[Exception], message: str
) -> None:
    client = Client(ClientResponse(200, (), json.dumps(payload).encode(), "1.1"))
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro"),),
        mode="subscription",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JINVALIDCHECKOUT",
    )

    with pytest.raises(error, match=message):
        await stripe(client).create_checkout(request, idempotency_key=request.reference)


@pytest.mark.asyncio
async def test_checkout_preserves_a_valid_provider_expiry() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"cs_1","url":"https://checkout.stripe.com/c/pay/1",'
            b'"expires_at":1801267200}',
            "1.1",
        )
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro"),),
        mode="subscription",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JEXPIRINGCHECKOUT",
    )

    session = await stripe(client).create_checkout(
        request, idempotency_key=request.reference
    )

    assert session.expires_at == datetime.fromtimestamp(1_801_267_200, UTC)


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
async def test_destination_portal_without_on_behalf_of_uses_the_platform_account() -> None:
    client = Client(
        ClientResponse(
            200,
            (),
            b'{"id":"bps_1","url":"https://billing.stripe.com/p/session/platform"}',
            "1.1",
        )
    )
    request = PortalRequest(
        subject="organization:acme",
        customer="cus_acme",
        return_url="https://app.example/settings/billing",
        reference="01JPLATFORMPORTAL",
    )
    backend = stripe(
        client,
        connect=StripeConnect(
            DestinationCharges(refunds=DestinationRefunds(False, False))
        ),
    )

    await backend.create_portal(request, idempotency_key=request.reference)

    assert b"stripe-account" not in dict(client.calls[0]["headers"])
    assert b"on_behalf_of" not in client.calls[0]["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "error", "message"),
    [
        ({"id": None, "url": "https://billing.stripe.com/p/session/1"}, StripeError, "missing id"),
        ({"id": "", "url": "https://billing.stripe.com/p/session/1"}, StripeError, "missing id"),
        ({"id": 1, "url": "https://billing.stripe.com/p/session/1"}, StripeError, "missing id"),
        ({"id": "bps_1", "url": None}, StripeError, "missing url"),
        ({"id": "bps_1", "url": 1}, StripeError, "missing url"),
        (
            {"id": "bps_1", "url": "http://billing.stripe.com/p/session/1"},
            ValueError,
            "over HTTPS",
        ),
        (
            {"id": "bps_1", "url": "https://user@billing.stripe.com/p/session/1"},
            ValueError,
            "over HTTPS",
        ),
        (
            {"id": "bps_1", "url": "https://user:secret@billing.stripe.com/p/session/1"},
            ValueError,
            "over HTTPS",
        ),
        (
            {"id": "bps_1", "url": "https://billing.stripe.com:444/p/session/1"},
            ValueError,
            "over HTTPS",
        ),
        (
            {"id": "bps_1", "url": "https://billing.stripe.com/p/session/1#attacker"},
            ValueError,
            "over HTTPS",
        ),
        (
            {"id": "bps_1", "url": "https://billing.stripe.com\t/p/session/1"},
            ValueError,
            "over HTTPS",
        ),
        (
            {"id": "bps_1", "url": "https://billing.stripe.com/p/session/\u009fattacker"},
            ValueError,
            "over HTTPS",
        ),
    ],
)
async def test_portal_refuses_each_invalid_provider_response(
    payload: dict[str, Any], error: type[Exception], message: str
) -> None:
    client = Client(ClientResponse(200, (), json.dumps(payload).encode(), "1.1"))
    request = PortalRequest(
        subject="organization:acme",
        customer="cus_acme",
        return_url="https://app.example/settings/billing",
        reference="01JINVALIDPORTAL",
    )

    with pytest.raises(error, match=message):
        await stripe(client).create_portal(request, idempotency_key=request.reference)


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


class WebhookApp:
    def post(self, path: str, **options: Any) -> Any:
        del path, options

        def register(handler: Any) -> Any:
            return handler

        return register


class InboxSessionFactory:
    def __call__(self) -> Any:
        raise AssertionError("binding must not open an inbox transaction")


class RecordingStripeLedger(PostgresBillingLedger):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, Any, Any]] = []

    async def apply_checkout(self, session: Any, payment: Any) -> None:
        self.calls.append(("checkout", session, payment))

    async def apply_subscription(self, session: Any, snapshot: Any, **options: Any) -> None:
        del options
        self.calls.append(("subscription", session, snapshot))

    async def apply_payment(self, session: Any, payment: Any, **options: Any) -> None:
        del options
        self.calls.append(("invoice", session, payment))


def webhook_source() -> WebhookSource:
    return WebhookSource(
        WebhookApp(),
        "stripe",
        path="/webhooks/stripe",
        verifier=StripeWebhookVerifier(b"whsec_test"),
        replay=None,
        limits=WebhookLimits(),
        inbox=PostgresWebhookInbox(),
        session_factory=InboxSessionFactory(),
        lease_owner="stripe-tests",
        lease_seconds=30,
    )


def webhook_billing(
    *,
    connect: StripeConnect | None = None,
    response: dict[str, Any] | None = None,
) -> tuple[Billing, RecordingStripeLedger]:
    client = Client(
        ClientResponse(200, (), json.dumps(response or {}).encode(), "1.1")
    )
    backend = stripe(client, connect=connect)
    ledger = RecordingStripeLedger()
    connected = connect is not None
    direct = connected and isinstance(connect.charges, DirectCharges)
    billing = Billing(
        "stripe-webhook-tests",
        backend=backend,
        catalog=PlanCatalog(Plan("pro", "price_pro")),
        merchant=ConnectedMerchant() if direct else DeploymentMerchant(),
        capture=HostedRedirect(),
        ledger=ledger,
        **(
            {
                "topology": ConnectedMerchants(
                    account_for=lambda subject: "acct_acme",
                    price_for=(
                        (lambda subject, sku, account: "price_pro") if direct else None
                    ),
                    sku_for_price=(
                        (lambda subject, price, account: "pro") if direct else None
                    ),
                )
            }
            if connected
            else {}
        ),
    )
    return billing, ledger


def bind_webhooks(
    source: WebhookSource,
    billing: Billing,
    webhook: StripeWebhookPolicy | None = None,
    *,
    operations: Any = None,
) -> None:
    bind_stripe_webhooks(
        source,
        billing=billing,
        webhook=webhook or StripeWebhookPolicy("2026-08-26.dahlia", False, "account"),
        checkout_subject_for=lambda reference, customer, account: "organization:acme",
        subscription_subject_for=lambda customer, account: "organization:acme",
        operations=operations,
    )


def stripe_event(event_type: str, resource: dict[str, Any]) -> WebhookEnvelope:
    body = json.dumps(
        {
            "id": "evt_1",
            "type": event_type,
            "api_version": "2026-08-26.dahlia",
            "livemode": False,
            "data": {"object": resource},
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


def test_stripe_webhook_binding_refuses_each_invalid_collaborator() -> None:
    billing, _ = webhook_billing()
    invalid: Any = object()
    with pytest.raises(TypeError, match="source must be WebhookSource"):
        bind_webhooks(invalid, billing)

    source = webhook_source()
    source._verifier = invalid
    with pytest.raises(TypeError, match="verifier must be StripeWebhookVerifier"):
        bind_webhooks(source, billing)

    with pytest.raises(TypeError, match="billing must be Billing"):
        bind_webhooks(webhook_source(), invalid)

    billing, _ = webhook_billing()
    billing.backend = invalid
    with pytest.raises(TypeError, match="backend must be StripeBilling"):
        bind_webhooks(webhook_source(), billing)

    billing, _ = webhook_billing()
    with pytest.raises(TypeError, match="policy must be StripeWebhookPolicy"):
        bind_webhooks(webhook_source(), billing, invalid)
    with pytest.raises(TypeError, match="operations must be BillingOperations or None"):
        bind_webhooks(webhook_source(), billing, operations=invalid)

    source = webhook_source()
    source._session_factory = invalid
    with pytest.raises(TypeError, match="callable inbox session_factory"):
        bind_webhooks(source, billing)

    source = webhook_source()
    source._handlers["invoice.paid"] = (invalid, invalid)
    with pytest.raises(ValueError, match="duplicate webhook event handler: invoice.paid"):
        bind_webhooks(source, billing)


def test_stripe_webhook_binding_enforces_each_connect_scope_pair() -> None:
    destination = StripeConnect(DestinationCharges(DestinationRefunds(False, False)))
    billing, _ = webhook_billing(connect=destination)
    bind_webhooks(webhook_source(), billing)

    direct = StripeConnect(DirectCharges())
    billing, _ = webhook_billing(connect=direct)
    connected = StripeWebhookPolicy(
        "2026-08-26.dahlia", False, "connected_accounts"
    )
    bind_webhooks(webhook_source(), billing, connected)

    billing, _ = webhook_billing()
    with pytest.raises(ValueError, match="requires a direct Connect backend"):
        bind_webhooks(webhook_source(), billing, connected)


@pytest.mark.asyncio
async def test_stripe_webhook_handlers_preserve_projection_and_transaction_refusals() -> None:
    billing, ledger = webhook_billing(response={"id": "cs_1", "mode": "subscription"})
    source = webhook_source()
    bind_webhooks(source, billing)
    checkout = stripe_event("checkout.session.completed", {"id": "cs_1"})
    context = WebhookContext("stripe", checkout, cast(Request, object()), object())

    await source._handlers[checkout.type][1](context, {})
    assert ledger.calls == []

    unrelated = stripe_event("checkout.session.completed", {})
    unrelated_context = WebhookContext(
        "stripe", unrelated, cast(Request, object()), object()
    )
    for registered in ("customer.subscription.updated", "invoice.paid"):
        with pytest.raises(RuntimeError, match="was not projected"):
            await source._handlers[registered][1](unrelated_context, {})

    missing_session = WebhookContext(
        "stripe", checkout, cast(Request, object()), None
    )
    with pytest.raises(RuntimeError, match="requires its inbox transaction session"):
        await source._handlers[checkout.type][1](missing_session, {})
