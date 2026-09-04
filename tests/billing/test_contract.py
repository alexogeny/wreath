from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from wreath import Wreath
from wreath.auth import Identity
from wreath.billing import (
    Billing,
    BillingCapabilities,
    BillingCommand,
    BillingCommandIdentity,
    BillingCommandState,
    BillingConfigurationError,
    ConnectedMerchant,
    ConnectedMerchants,
    DeploymentMerchant,
    HostedRedirect,
    PostgresBillingLedger,
    ProviderMerchant,
)
from wreath.billing.providers.stripe import (
    DestinationCharges,
    DestinationRefunds,
    DirectCharges,
    StripeBilling,
    StripeConnect,
)
from wreath.config import Secret
from wreath.http_client import ClientResponse
from wreath.payments import (
    CheckoutItem,
    CheckoutRequest,
    CheckoutSession,
    Money,
    PaymentSnapshot,
    PaymentState,
    PortalRequest,
    PortalSession,
    ProviderPayment,
    Refund,
    RefundRequest,
    RefundState,
)
from wreath.subscriptions import (
    AccessPolicy,
    Plan,
    PlanCatalog,
    SubscriptionEntitlements,
    SubscriptionSnapshot,
    SubscriptionState,
)


def test_hosted_session_reprs_do_not_expose_capability_urls() -> None:
    secret = "hosted-session-capability-secret"
    url = f"https://billing.example/session/{secret}"

    assert secret not in repr(CheckoutSession("provider", "checkout-1", url))
    assert secret not in repr(PortalSession("provider", "portal-1", url))


class Backend:
    provider = "test"

    def __init__(self, capabilities: BillingCapabilities | None = None) -> None:
        self.capabilities = capabilities or BillingCapabilities(
            hosted_checkout=True,
            hosted_portal=True,
            subscriptions=True,
        )
        self.requests: list[tuple[CheckoutRequest, str]] = []
        self.portal_requests: list[tuple[PortalRequest, str]] = []
        self.refund_requests: list[tuple[RefundRequest, str]] = []

    async def create_checkout(
        self, request: CheckoutRequest, *, idempotency_key: str
    ) -> CheckoutSession:
        self.requests.append((request, idempotency_key))
        return CheckoutSession(
            provider="test",
            id="checkout_1",
            url="https://checkout.example/session/1",
        )

    async def create_portal(self, request: PortalRequest, *, idempotency_key: str) -> PortalSession:
        self.portal_requests.append((request, idempotency_key))
        return PortalSession("test", "portal_1", "https://billing.example/portal/1")

    async def create_refund(self, request: RefundRequest, *, idempotency_key: str) -> Refund:
        self.refund_requests.append((request, idempotency_key))
        return Refund("test", "refund_1", RefundState.SUCCEEDED, Money("USD", 100))


class Client:
    def __init__(self, response: ClientResponse) -> None:
        self.response = response
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
        return self.response


def catalog() -> PlanCatalog:
    return PlanCatalog(
        Plan(
            sku="pro",
            provider_price="price_pro",
            entitlements=frozenset({"api", "export"}),
        )
    )


def billing_facade(
    capabilities: BillingCapabilities,
    *,
    customer_for: Any = None,
    payment_for: Any = None,
) -> tuple[Backend, Billing]:
    backend = Backend(capabilities)
    options = (
        {"topology": ConnectedMerchants(account_for=lambda subject: "acct_acme")}
        if capabilities.connect
        else {}
    )
    return backend, Billing(
        "commerce",
        backend=backend,
        catalog=catalog(),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
        customer_for=customer_for,
        payment_for=payment_for,
        **options,
    )


def test_money_is_integer_minor_units_in_an_uppercase_currency() -> None:
    assert Money(currency="USD", minor=2900).minor == 2900
    floating_minor: Any = 29.0
    with pytest.raises(TypeError, match="integer minor units"):
        Money(currency="USD", minor=floating_minor)
    with pytest.raises(ValueError, match="three uppercase ASCII letters"):
        Money(currency="usd", minor=2900)
    with pytest.raises(ValueError, match="must not be negative"):
        Money(currency="USD", minor=-1)


def test_checkout_types_expose_no_raw_card_data_surface() -> None:
    fields = CheckoutRequest.__dataclass_fields__
    assert {"pan", "card_number", "cvc", "cvv", "track_data"}.isdisjoint(fields)
    with pytest.raises(ValueError, match="positive"):
        CheckoutItem(price="price_pro", quantity=0)
    with pytest.raises(ValueError, match="provider price"):
        CheckoutItem(price="", quantity=1)


def test_catalog_refuses_ambiguous_plan_and_provider_price_ownership() -> None:
    first = Plan("pro", "price_pro")
    with pytest.raises(ValueError, match="duplicate plan sku 'pro'"):
        PlanCatalog(first, Plan("pro", "price_other"))
    with pytest.raises(ValueError, match="provider price 'price_pro'.*two plans"):
        PlanCatalog(first, Plan("team", "price_pro"))


def test_plan_entitlements_are_validated_and_frozen_at_declaration() -> None:
    mutable = {"api"}
    supplied: Any = mutable
    plan = Plan("pro", "price_pro", entitlements=supplied)
    mutable.add("admin")

    assert plan.entitlements == frozenset({"api"})
    invalid: Any = frozenset({"api", 7})
    with pytest.raises(TypeError, match="entitlements must be non-empty strings"):
        Plan("invalid", "price_invalid", entitlements=invalid)


def test_billing_compiles_a_hosted_redirect_compliance_posture() -> None:
    billing = Billing(
        "commerce",
        backend=Backend(),
        catalog=catalog(),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
    )

    posture = billing.compliance_posture()

    assert posture.capture == "hosted-redirect"
    assert posture.cardholder_data == "provider-only"
    assert posture.candidate_saq == "SAQ A"
    assert posture.merchant_of_record == "deployment"
    assert "confirm SAQ with the acquirer or payment brand" in posture.unresolved
    assert any("durable billing ledger" in item for item in posture.unresolved)


def test_billing_delegates_schema_ownership_to_its_durable_ledger() -> None:
    ledger = PostgresBillingLedger()
    billing = Billing(
        "commerce",
        backend=Backend(),
        catalog=catalog(),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
        ledger=ledger,
    )

    assert billing.schema_owners == (ledger,)
    assert not any(
        "durable billing ledger" in item for item in billing.compliance_posture().unresolved
    )
    assert BillingCommand.__module__ == "wreath.billing.ledger"
    assert BillingCommandIdentity.__module__ == "wreath.billing.ledger"
    assert BillingCommandState.__module__ == "wreath.billing.ledger"


def test_billing_refuses_capture_and_merchant_shapes_it_cannot_keep_out_of_pci_scope() -> None:
    embedded_capture: Any = object()
    unknown_merchant: Any = object()
    with pytest.raises(
        BillingConfigurationError,
        match="capture must be HostedRedirect; embedded or direct card capture is unsupported",
    ):
        Billing(
            "commerce",
            backend=Backend(),
            catalog=catalog(),
            merchant=DeploymentMerchant(),
            capture=embedded_capture,
        )
    with pytest.raises(BillingConfigurationError, match="merchant must be one of"):
        Billing(
            "commerce",
            backend=Backend(),
            catalog=catalog(),
            merchant=unknown_merchant,
            capture=HostedRedirect(),
        )


def test_provider_merchant_requires_a_backend_that_accepts_that_responsibility() -> None:
    with pytest.raises(BillingConfigurationError, match="merchant of record"):
        Billing(
            "commerce",
            backend=Backend(),
            catalog=catalog(),
            merchant=ProviderMerchant(),
            capture=HostedRedirect(),
        )


def test_subscription_plans_require_subscription_capability() -> None:
    backend = Backend(
        BillingCapabilities(hosted_checkout=True, hosted_portal=True, subscriptions=False)
    )
    with pytest.raises(BillingConfigurationError, match="plan 'pro'.*subscriptions"):
        Billing(
            "commerce",
            backend=backend,
            catalog=catalog(),
            merchant=DeploymentMerchant(),
            capture=HostedRedirect(),
        )


@pytest.mark.asyncio
async def test_one_time_payment_plan_does_not_require_subscription_capability() -> None:
    backend = Backend(
        BillingCapabilities(hosted_checkout=True, hosted_portal=True, subscriptions=False)
    )
    billing = Billing(
        "commerce",
        backend=backend,
        catalog=PlanCatalog(Plan("credits", "price_credits", mode="payment")),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
    )

    await billing.checkout(
        subject="organization:acme",
        plan="credits",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JONETIMEPAYMENT",
    )

    assert backend.requests[0][0].mode == "payment"


def test_plan_mode_is_refused_at_declaration() -> None:
    bad_mode: Any = "recurring"
    with pytest.raises(ValueError, match="plan mode must be 'payment' or 'subscription'"):
        Plan("pro", "price_pro", mode=bad_mode)


@pytest.mark.asyncio
async def test_checkout_resolves_the_declared_plan_and_forwards_one_opaque_reference() -> None:
    backend = Backend()
    billing = Billing(
        "commerce",
        backend=backend,
        catalog=catalog(),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
    )

    session = await billing.checkout(
        subject="organization:acme",
        plan="pro",
        quantity=3,
        success_url="https://app.example/billing/success",
        cancel_url="https://app.example/billing/cancel",
        reference="01JCOMMERCECHECKOUT",
    )

    assert session.url == "https://checkout.example/session/1"
    request, key = backend.requests[0]
    assert request.subject == "organization:acme"
    assert request.items == (CheckoutItem("price_pro", 3),)
    assert request.mode == "subscription"
    assert request.reference == "01JCOMMERCECHECKOUT"
    assert key == "01JCOMMERCECHECKOUT"


@pytest.mark.asyncio
async def test_checkout_refuses_an_unknown_plan_before_crossing_the_network() -> None:
    backend = Backend()
    billing = Billing(
        "commerce",
        backend=backend,
        catalog=catalog(),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
    )

    with pytest.raises(KeyError, match="unknown billing plan 'enterprise'; configured: pro"):
        await billing.checkout(
            subject="organization:acme",
            plan="enterprise",
            success_url="https://app.example/success",
            cancel_url="https://app.example/cancel",
            reference="01JUNKNOWNPLAN",
        )
    assert backend.requests == []


@pytest.mark.asyncio
async def test_connected_topology_resolves_account_from_subject_not_the_caller() -> None:
    backend = Backend(
        BillingCapabilities(
            hosted_checkout=True,
            hosted_portal=True,
            subscriptions=True,
            connect=True,
        )
    )
    billing = Billing(
        "commerce",
        backend=backend,
        catalog=catalog(),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
        topology=ConnectedMerchants(
            account_for=lambda subject: {
                "organization:acme": "acct_acme",
            }.get(subject)
        ),
    )

    await billing.checkout(
        subject="organization:acme",
        plan="pro",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JCONNECTEDTENANT",
    )

    assert backend.requests[0][0].merchant_account == "acct_acme"
    with pytest.raises(KeyError, match="no merchant account mapping for billing subject"):
        await billing.checkout(
            subject="organization:unknown",
            plan="pro",
            success_url="https://app.example/success",
            cancel_url="https://app.example/cancel",
            reference="01JUNKNOWNMERCHANT",
        )
    assert len(backend.requests) == 1


def test_connected_topology_requires_a_connect_backend() -> None:
    with pytest.raises(BillingConfigurationError, match="connected merchant topology"):
        Billing(
            "commerce",
            backend=Backend(),
            catalog=catalog(),
            merchant=DeploymentMerchant(),
            capture=HostedRedirect(),
            topology=ConnectedMerchants(account_for=lambda subject: "acct_acme"),
        )


def test_stripe_merchant_responsibility_must_match_the_declared_charge_mode() -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))
    ordinary = StripeBilling(
        client=client,
        api_key=Secret("rk_test_example"),
        api_version="2026-08-26.dahlia",
        allowed_return_origins=("https://app.example",),
    )
    managed = StripeBilling(
        client=client,
        api_key=Secret("rk_test_example"),
        api_version="2026-08-26.dahlia",
        allowed_return_origins=("https://app.example",),
        managed_payments=True,
    )
    direct = StripeBilling(
        client=client,
        api_key=Secret("rk_test_example"),
        api_version="2026-08-26.dahlia",
        allowed_return_origins=("https://app.example",),
        connect=StripeConnect(DirectCharges()),
    )

    with pytest.raises(BillingConfigurationError, match="declares merchant 'deployment'"):
        Billing(
            "ordinary",
            backend=ordinary,
            catalog=catalog(),
            merchant=ProviderMerchant(),
            capture=HostedRedirect(),
        )
    with pytest.raises(BillingConfigurationError, match="declares merchant 'provider'"):
        Billing(
            "managed",
            backend=managed,
            catalog=catalog(),
            merchant=DeploymentMerchant(),
            capture=HostedRedirect(),
        )
    with pytest.raises(BillingConfigurationError, match="declares merchant 'connected'"):
        Billing(
            "direct",
            backend=direct,
            catalog=catalog(),
            merchant=DeploymentMerchant(),
            capture=HostedRedirect(),
            topology=ConnectedMerchants(account_for=lambda subject: "acct_direct"),
        )

    Billing(
        "managed",
        backend=managed,
        catalog=catalog(),
        merchant=ProviderMerchant(),
        capture=HostedRedirect(),
    )
    Billing(
        "direct",
        backend=direct,
        catalog=catalog(),
        merchant=ConnectedMerchant(),
        capture=HostedRedirect(),
        topology=ConnectedMerchants(
            account_for=lambda subject: "acct_direct",
            price_for=lambda subject, sku, account: "price_pro",
            sku_for_price=lambda subject, price, account: "pro",
        ),
    )


@pytest.mark.asyncio
async def test_billing_facade_owns_portal_and_refund_provider_commands() -> None:
    backend = Backend(
        BillingCapabilities(
            hosted_checkout=True,
            hosted_portal=True,
            subscriptions=True,
            refunds=True,
        )
    )
    billing = Billing(
        "commerce",
        backend=backend,
        catalog=catalog(),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
        customer_for=lambda subject: {"organization:acme": "cus_acme"}.get(subject),
        payment_for=lambda subject, payment: {
            ("organization:acme", "payment:order-1"): ProviderPayment("pi_1", "USD")
        }.get((subject, payment)),
    )

    portal = await billing.portal(
        subject="organization:acme",
        return_url="https://app.example/settings/billing",
        reference="01JFACADEPORTAL",
    )
    refund = await billing.refund(
        subject="organization:acme",
        payment="payment:order-1",
        reference="01JFACADEREFUND",
        amount=Money("USD", 100),
    )
    projected_refund = await billing._refund_projected(
        PaymentSnapshot(
            provider="test",
            id="pi_2",
            subject="organization:acme",
            reference="order-2",
            amount=Money("USD", 500),
            state=PaymentState.SUCCEEDED,
        ),
        reference="01JFACADEPROJECTED",
        amount=Money("USD", 50),
    )

    assert portal.id == "portal_1"
    assert refund.id == "refund_1"
    assert projected_refund.id == "refund_1"
    assert backend.portal_requests[0][0].subject == "organization:acme"
    assert backend.refund_requests[0][0].payment == "pi_1"
    assert backend.refund_requests[1][0].payment == "pi_2"


@pytest.mark.asyncio
async def test_billing_refuses_unknown_customer_and_payment_before_provider_io() -> None:
    backend = Backend(
        BillingCapabilities(
            hosted_checkout=True,
            hosted_portal=True,
            subscriptions=True,
            refunds=True,
        )
    )
    billing = Billing(
        "commerce",
        backend=backend,
        catalog=catalog(),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
        customer_for=lambda subject: None,
        payment_for=lambda subject, payment: None,
    )

    with pytest.raises(KeyError, match="no provider customer mapping for billing subject"):
        await billing.portal(
            subject="organization:globex",
            return_url="https://app.example/settings/billing",
            reference="01JUNKNOWNCUSTOMER",
        )
    with pytest.raises(KeyError, match="no provider payment mapping for billing subject"):
        await billing.refund(
            subject="organization:globex",
            payment="payment:acme-order",
            reference="01JWRONGTENANTREFUND",
        )
    assert backend.portal_requests == []
    assert backend.refund_requests == []


@pytest.mark.asyncio
async def test_billing_portal_refuses_each_missing_capability_and_mapping() -> None:
    unsupported_backend, unsupported = billing_facade(
        BillingCapabilities(hosted_checkout=True, subscriptions=True),
        customer_for=lambda subject: "cus_acme",
    )
    with pytest.raises(BillingConfigurationError, match="does not support hosted portal"):
        await unsupported.portal(
            subject="organization:acme",
            return_url="https://app.example/billing",
            reference="portal:unsupported",
        )
    assert unsupported_backend.portal_requests == []

    missing_backend, missing = billing_facade(
        BillingCapabilities(hosted_checkout=True, hosted_portal=True, subscriptions=True)
    )
    with pytest.raises(BillingConfigurationError, match="requires customer_for"):
        await missing.portal(
            subject="organization:acme",
            return_url="https://app.example/billing",
            reference="portal:missing",
        )
    assert missing_backend.portal_requests == []

    for customer in ("", 1):
        backend, invalid = billing_facade(
            BillingCapabilities(hosted_checkout=True, hosted_portal=True, subscriptions=True),
            customer_for=lambda subject, value=customer: value,
        )
        with pytest.raises(KeyError, match="no provider customer mapping"):
            await invalid.portal(
                subject="organization:acme",
                return_url="https://app.example/billing",
                reference="portal:invalid",
            )
        assert backend.portal_requests == []


@pytest.mark.asyncio
async def test_billing_refund_refuses_each_invalid_control_boundary() -> None:
    unsupported_backend, unsupported = billing_facade(
        BillingCapabilities(hosted_checkout=True, subscriptions=True),
        payment_for=lambda subject, payment: ProviderPayment("pi_1", "USD"),
    )
    with pytest.raises(BillingConfigurationError, match="does not support refunds"):
        await unsupported.refund(
            subject="organization:acme",
            payment="payment:1",
            reference="refund:unsupported",
        )
    assert unsupported_backend.refund_requests == []

    for subject in ("", 1):
        backend, invalid = billing_facade(
            BillingCapabilities(hosted_checkout=True, subscriptions=True, refunds=True),
            payment_for=lambda owner, payment: ProviderPayment("pi_1", "USD"),
        )
        with pytest.raises(ValueError, match="subject must be a non-empty string"):
            await invalid.refund(
                subject=subject,
                payment="payment:1",
                reference="refund:invalid-subject",
            )
        assert backend.refund_requests == []

    missing_backend, missing = billing_facade(
        BillingCapabilities(hosted_checkout=True, subscriptions=True, refunds=True)
    )
    with pytest.raises(BillingConfigurationError, match="requires payment_for"):
        await missing.refund(
            subject="organization:acme",
            payment="payment:1",
            reference="refund:missing",
        )
    assert missing_backend.refund_requests == []

    invalid_backend, invalid_mapping = billing_facade(
        BillingCapabilities(hosted_checkout=True, subscriptions=True, refunds=True),
        payment_for=lambda subject, payment: object(),
    )
    with pytest.raises(TypeError, match="must return ProviderPayment"):
        await invalid_mapping.refund(
            subject="organization:acme",
            payment="payment:1",
            reference="refund:invalid-mapping",
        )
    assert invalid_backend.refund_requests == []


@pytest.mark.asyncio
async def test_billing_refund_preserves_connect_merchant_ownership() -> None:
    connected_backend, connected = billing_facade(
        BillingCapabilities(
            hosted_checkout=True,
            subscriptions=True,
            connect=True,
            refunds=True,
        ),
        payment_for=lambda subject, payment: ProviderPayment("pi_1", "USD"),
    )
    with pytest.raises(ValueError, match="must retain its original merchant account"):
        await connected.refund(
            subject="organization:acme",
            payment="payment:1",
            reference="refund:missing-account",
        )
    assert connected_backend.refund_requests == []

    retained_backend, retained = billing_facade(
        BillingCapabilities(
            hosted_checkout=True,
            subscriptions=True,
            connect=True,
            refunds=True,
        ),
        payment_for=lambda subject, payment: ProviderPayment("pi_1", "USD", "acct_acme"),
    )
    await retained.refund(
        subject="organization:acme",
        payment="payment:1",
        reference="refund:retained-account",
    )
    assert retained_backend.refund_requests[0][0].merchant_account == "acct_acme"

    ordinary_backend, ordinary = billing_facade(
        BillingCapabilities(hosted_checkout=True, subscriptions=True, refunds=True),
        payment_for=lambda subject, payment: ProviderPayment("pi_1", "USD", "acct_acme"),
    )
    with pytest.raises(ValueError, match="must not carry a merchant account"):
        await ordinary.refund(
            subject="organization:acme",
            payment="payment:1",
            reference="refund:unexpected-account",
        )
    assert ordinary_backend.refund_requests == []


@pytest.mark.asyncio
async def test_projected_refund_rechecks_the_refund_capability() -> None:
    backend, billing = billing_facade(
        BillingCapabilities(hosted_checkout=True, subscriptions=True)
    )

    with pytest.raises(BillingConfigurationError, match="does not support refunds"):
        await billing._refund_projected(
            PaymentSnapshot(
                provider="test",
                id="pi_1",
                subject="organization:acme",
                reference="order:1",
                amount=Money("USD", 100),
                state=PaymentState.SUCCEEDED,
            ),
            reference="refund:projected",
        )
    assert backend.refund_requests == []


@pytest.mark.asyncio
async def test_stripe_checkout_is_versioned_idempotent_and_server_priced() -> None:
    response = ClientResponse(
        200,
        ((b"content-type", b"application/json"),),
        json.dumps(
            {
                "id": "cs_test_1",
                "url": "https://checkout.stripe.com/c/pay/cs_test_1",
                "expires_at": 1_800_000_000,
            }
        ).encode(),
        "1.1",
    )
    client = Client(response)
    stripe = StripeBilling(
        client=client,
        api_key=Secret("rk_test_example"),
        api_version="2026-08-26.dahlia",
        allowed_return_origins=("https://app.example",),
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro", 2),),
        mode="subscription",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JSTRIPECHECKOUT",
    )

    session = await stripe.create_checkout(request, idempotency_key=request.reference)

    assert session == CheckoutSession(
        provider="stripe",
        id="cs_test_1",
        url="https://checkout.stripe.com/c/pay/cs_test_1",
        expires_at=datetime.fromtimestamp(1_800_000_000, UTC),
    )
    call = client.calls[0]
    assert call["target"] == "/v1/checkout/sessions"
    assert call["idempotency_key"] == "01JSTRIPECHECKOUT"
    assert dict(call["headers"]) == {
        b"authorization": b"Bearer rk_test_example",
        b"content-type": b"application/x-www-form-urlencoded",
        b"stripe-version": b"2026-08-26.dahlia",
    }
    assert call["body"] == (
        b"mode=subscription&success_url=https%3A%2F%2Fapp.example%2Fsuccess&"
        b"cancel_url=https%3A%2F%2Fapp.example%2Fcancel&"
        b"client_reference_id=01JSTRIPECHECKOUT&line_items%5B0%5D%5Bprice%5D="
        b"price_pro&line_items%5B0%5D%5Bquantity%5D=2"
    )
    assert b"organization%3Aacme" not in call["body"]


@pytest.mark.asyncio
async def test_stripe_refuses_insecure_return_urls_without_calling_the_provider() -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))
    stripe = StripeBilling(
        client=client,
        api_key=Secret("rk_test_example"),
        api_version="2026-08-26.dahlia",
        allowed_return_origins=("https://app.example",),
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro"),),
        mode="subscription",
        success_url="http://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JINSECURERETURN",
    )

    with pytest.raises(ValueError, match="success_url must be an absolute HTTPS URL"):
        await stripe.create_checkout(request, idempotency_key=request.reference)
    assert client.calls == []


@pytest.mark.asyncio
async def test_stripe_refuses_a_checkout_url_outside_stripes_host() -> None:
    response = ClientResponse(
        200,
        (),
        b'{"id":"cs_test_1","url":"https://evil.example/cs_test_1"}',
        "1.1",
    )
    stripe = StripeBilling(
        client=Client(response),
        api_key=Secret("rk_test_example"),
        api_version="2026-08-26.dahlia",
        allowed_return_origins=("https://app.example",),
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro"),),
        mode="subscription",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JUNTRUSTEDRETURN",
    )

    with pytest.raises(ValueError, match="Stripe checkout URL must use checkout.stripe.com"):
        await stripe.create_checkout(request, idempotency_key=request.reference)


@pytest.mark.asyncio
async def test_stripe_managed_payments_is_explicit_and_incompatible_with_connect() -> None:
    response = ClientResponse(
        200,
        (),
        b'{"id":"cs_test_managed","url":"https://checkout.stripe.com/c/pay/managed"}',
        "1.1",
    )
    client = Client(response)
    stripe = StripeBilling(
        client=client,
        api_key=Secret("rk_test_example"),
        api_version="2026-08-26.dahlia",
        allowed_return_origins=("https://app.example",),
        managed_payments=True,
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro"),),
        mode="subscription",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JMANAGEDPAYMENTS",
    )

    await stripe.create_checkout(request, idempotency_key=request.reference)

    assert stripe.capabilities.merchant_of_record is True
    assert b"managed_payments%5Benabled%5D=true" in client.calls[0]["body"]
    with pytest.raises(ValueError, match="Managed Payments requires Stripe API version"):
        StripeBilling(
            client=client,
            api_key=Secret("rk_test_example"),
            api_version="2024-12-18.acacia",
            allowed_return_origins=("https://app.example",),
            managed_payments=True,
        )
    with pytest.raises(ValueError, match="Managed Payments does not support Connect"):
        StripeBilling(
            client=client,
            api_key=Secret("rk_test_example"),
            api_version="2026-08-26.dahlia",
            allowed_return_origins=("https://app.example",),
            managed_payments=True,
            connect=StripeConnect(DirectCharges()),
        )


@pytest.mark.asyncio
async def test_stripe_connect_direct_charge_scopes_checkout_to_the_account() -> None:
    response = ClientResponse(
        200,
        (),
        b'{"id":"cs_test_direct","url":"https://checkout.stripe.com/c/pay/direct"}',
        "1.1",
    )
    client = Client(response)
    stripe = StripeBilling(
        client=client,
        api_key=Secret("rk_test_example"),
        api_version="2026-08-26.dahlia",
        allowed_return_origins=("https://app.example",),
        connect=StripeConnect(DirectCharges(application_fee_percent="12.5")),
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_connected_pro"),),
        mode="subscription",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JDIRECTCHARGE",
        merchant_account="acct_direct",
    )

    await stripe.create_checkout(request, idempotency_key=request.reference)

    call = client.calls[0]
    assert dict(call["headers"])[b"stripe-account"] == b"acct_direct"
    assert b"subscription_data%5Bapplication_fee_percent%5D=12.5" in call["body"]
    assert b"transfer_data" not in call["body"]


@pytest.mark.asyncio
async def test_stripe_connect_destination_charge_routes_without_account_header() -> None:
    response = ClientResponse(
        200,
        (),
        b'{"id":"cs_test_destination","url":"https://checkout.stripe.com/c/pay/dest"}',
        "1.1",
    )
    client = Client(response)
    stripe = StripeBilling(
        client=client,
        api_key=Secret("rk_test_example"),
        api_version="2026-08-26.dahlia",
        allowed_return_origins=("https://app.example",),
        connect=StripeConnect(
            DestinationCharges(
                application_fee_percent="8",
                on_behalf_of=True,
                refunds=DestinationRefunds(
                    reverse_transfer=True,
                    refund_application_fee=True,
                ),
            )
        ),
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_platform_pro"),),
        mode="subscription",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JDESTINATIONCHARGE",
        merchant_account="acct_destination",
    )

    await stripe.create_checkout(request, idempotency_key=request.reference)

    call = client.calls[0]
    assert b"stripe-account" not in dict(call["headers"])
    assert b"subscription_data%5Btransfer_data%5D%5Bdestination%5D=acct_destination" in call["body"]
    assert b"subscription_data%5Bapplication_fee_percent%5D=8" in call["body"]
    assert b"subscription_data%5Bon_behalf_of%5D=acct_destination" in call["body"]


@pytest.mark.asyncio
async def test_stripe_connect_requires_a_declared_account_before_provider_io() -> None:
    client = Client(ClientResponse(200, (), b"{}", "1.1"))
    stripe = StripeBilling(
        client=client,
        api_key=Secret("rk_test_example"),
        api_version="2026-08-26.dahlia",
        allowed_return_origins=("https://app.example",),
        connect=StripeConnect(DirectCharges()),
    )
    request = CheckoutRequest(
        subject="organization:acme",
        items=(CheckoutItem("price_pro"),),
        mode="subscription",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
        reference="01JMISSINGACCOUNT",
    )

    with pytest.raises(ValueError, match="Connect checkout requires merchant_account"):
        await stripe.create_checkout(request, idempotency_key=request.reference)
    assert client.calls == []


def test_subscription_entitlements_are_derived_from_the_local_projection() -> None:
    subscriptions = {
        "alice": SubscriptionSnapshot(
            provider="stripe",
            id="sub_1",
            subject="user:alice",
            plan="pro",
            state=SubscriptionState.ACTIVE,
            provider_state="active",
            paid_through=datetime(2026, 10, 1, tzinfo=UTC),
        ),
        "bob": SubscriptionSnapshot(
            provider="stripe",
            id="sub_2",
            subject="user:bob",
            plan="pro",
            state=SubscriptionState.PAST_DUE,
            provider_state="past_due",
            paid_through=datetime(2026, 10, 1, tzinfo=UTC),
        ),
    }
    provider = SubscriptionEntitlements(
        catalog(),
        subscription_for=lambda identity: subscriptions.get(identity.id),
        access=AccessPolicy(
            granted=frozenset({SubscriptionState.TRIALING, SubscriptionState.ACTIVE})
        ),
        now=lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )

    assert provider.entitlements(Identity("alice")) == frozenset({"api", "export"})
    assert provider.plan_for(Identity("alice")) == "pro"
    assert provider.entitlements(Identity("bob")) == frozenset()
    assert provider.plan_for(Identity("bob")) is None
    assert provider.names() == frozenset({"api", "export"})


def test_past_due_access_is_an_explicit_policy_not_a_provider_default() -> None:
    snapshot = SubscriptionSnapshot(
        provider="stripe",
        id="sub_1",
        subject="user:alice",
        plan="pro",
        state=SubscriptionState.PAST_DUE,
        provider_state="past_due",
        paid_through=datetime(2026, 10, 1, tzinfo=UTC),
    )
    provider = SubscriptionEntitlements(
        catalog(),
        subscription_for=lambda identity: snapshot,
        access=AccessPolicy(
            granted=frozenset(
                {
                    SubscriptionState.TRIALING,
                    SubscriptionState.ACTIVE,
                    SubscriptionState.PAST_DUE,
                }
            )
        ),
        now=lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )

    assert provider.entitlements(Identity("alice")) == frozenset({"api", "export"})


def test_subscription_access_requires_a_paid_or_bounded_trial_window() -> None:
    current = datetime(2026, 9, 2, tzinfo=UTC)
    catalog_value = catalog()

    def entitlements(snapshot: SubscriptionSnapshot) -> frozenset[str]:
        return SubscriptionEntitlements(
            catalog_value,
            subscription_for=lambda identity: snapshot,
            now=lambda: current,
        ).entitlements(Identity("alice"))

    assert (
        entitlements(
            SubscriptionSnapshot(
                "stripe",
                "sub_active_unpaid",
                "user:alice",
                "pro",
                SubscriptionState.ACTIVE,
                "active",
            )
        )
        == frozenset()
    )
    assert entitlements(
        SubscriptionSnapshot(
            "stripe",
            "sub_paid",
            "user:alice",
            "pro",
            SubscriptionState.ACTIVE,
            "active",
            paid_through=datetime(2026, 10, 1, tzinfo=UTC),
        )
    ) == frozenset({"api", "export"})
    assert entitlements(
        SubscriptionSnapshot(
            "stripe",
            "sub_trial",
            "user:alice",
            "pro",
            SubscriptionState.TRIALING,
            "trialing",
            trial_ends_at=datetime(2026, 9, 9, tzinfo=UTC),
        )
    ) == frozenset({"api", "export"})


def test_app_registers_one_named_billing_control_plane() -> None:
    app = Wreath()
    backend = Backend()

    billing = app.billing(
        "commerce",
        backend=backend,
        catalog=catalog(),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
    )

    assert isinstance(billing, Billing)
    assert app.state.billing_commerce is billing
    with pytest.raises(ValueError, match="duplicate billing control plane: commerce"):
        app.billing(
            "commerce",
            backend=backend,
            catalog=catalog(),
            merchant=DeploymentMerchant(),
            capture=HostedRedirect(),
        )


def test_app_attributes_a_billing_ledger_to_its_declared_database() -> None:
    app = Wreath()
    main = app.postgres("main", dsn="postgresql://u@main.invalid:5432/app")
    app.postgres("archive", dsn="postgresql://u@archive.invalid:5432/archive")

    app.billing(
        "commerce",
        backend=Backend(),
        catalog=catalog(),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
        ledger=PostgresBillingLedger(),
        database="main",
    )

    grouped = app._components_by_database(app.schema_components())

    assert tuple(grouped) == (main,)
    assert tuple(component.name for component in grouped[main]) == ("billing-ledger",)


def test_app_refuses_distinct_billing_ledgers_with_the_same_component_identity() -> None:
    app = Wreath()
    app.postgres("main", dsn="postgresql://u@main.invalid:5432/app")
    app.postgres("archive", dsn="postgresql://u@archive.invalid:5432/archive")
    app.billing(
        "commerce",
        backend=Backend(),
        catalog=catalog(),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
        ledger=PostgresBillingLedger(),
        database="main",
    )

    with pytest.raises(
        ValueError,
        match="billing ledger component 'billing-ledger'.*component_name=",
    ):
        app.billing(
            "archive",
            backend=Backend(),
            catalog=catalog(),
            merchant=DeploymentMerchant(),
            capture=HostedRedirect(),
            ledger=PostgresBillingLedger(schema="archive"),
            database="archive",
        )


def test_distinct_billing_component_identities_bootstrap_separate_databases() -> None:
    app = Wreath()
    main = app.postgres("main", dsn="postgresql://u@main.invalid:5432/app")
    archive = app.postgres("archive", dsn="postgresql://u@archive.invalid:5432/archive")
    app.billing(
        "commerce",
        backend=Backend(),
        catalog=catalog(),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
        ledger=PostgresBillingLedger(component_name="commerce-billing-ledger"),
        database="main",
    )
    app.billing(
        "archive",
        backend=Backend(),
        catalog=catalog(),
        merchant=DeploymentMerchant(),
        capture=HostedRedirect(),
        ledger=PostgresBillingLedger(
            schema="archive",
            component_name="archive-billing-ledger",
        ),
        database="archive",
    )

    grouped = app._components_by_database(app.schema_components())

    assert tuple(component.name for component in grouped[main]) == ("commerce-billing-ledger",)
    assert tuple(component.name for component in grouped[archive]) == ("archive-billing-ledger",)
