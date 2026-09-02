from __future__ import annotations

from dataclasses import dataclass

import pytest

from wreath.billing import (
    Billing,
    BillingCapabilities,
    BillingConfigurationError,
    ConnectedMerchant,
    ConnectedMerchants,
    HostedRedirect,
)
from wreath.payments import (
    CheckoutRequest,
    CheckoutSession,
    Money,
    ProviderPayment,
    Refund,
    RefundRequest,
    RefundState,
)
from wreath.subscriptions import Plan, PlanCatalog


@dataclass
class Backend:
    capabilities = BillingCapabilities(
        hosted_checkout=True,
        subscriptions=True,
        connect=True,
        refunds=True,
        merchant="connected",
        account_scoped_prices=True,
    )

    def __post_init__(self) -> None:
        self.checkout_requests: list[CheckoutRequest] = []
        self.refund_requests: list[RefundRequest] = []

    async def create_checkout(
        self, request: CheckoutRequest, *, idempotency_key: str
    ) -> CheckoutSession:
        self.checkout_requests.append(request)
        return CheckoutSession("test", idempotency_key, "https://checkout.example/session")

    async def create_refund(self, request: RefundRequest, *, idempotency_key: str) -> Refund:
        self.refund_requests.append(request)
        return Refund("test", idempotency_key, RefundState.SUCCEEDED, Money("USD", 100))


def topology(*, inverse: bool = True) -> ConnectedMerchants:
    prices = {
        ("organization:acme", "pro", "acct_acme"): "price_acme_pro",
        ("organization:globex", "pro", "acct_globex"): "price_globex_pro",
    }
    inverse_prices = {key[0:1] + (price, key[2]): key[1] for key, price in prices.items()}
    return ConnectedMerchants(
        account_for=lambda subject: {
            "organization:acme": "acct_acme",
            "organization:globex": "acct_globex",
        }.get(subject),
        price_for=lambda subject, sku, account: prices.get((subject, sku, account)),
        sku_for_price=(
            lambda subject, price, account: inverse_prices.get((subject, price, account))
        )
        if inverse
        else None,
    )


def billing(backend: Backend, **options: object) -> Billing:
    return Billing(
        "marketplace",
        backend=backend,
        catalog=PlanCatalog(Plan("pro", "price_shared_pro")),
        merchant=ConnectedMerchant(),
        capture=HostedRedirect(),
        topology=topology(),
        **options,
    )


@pytest.mark.asyncio
async def test_direct_connect_resolves_price_and_existing_customer_per_subject() -> None:
    backend = Backend()
    commerce = billing(
        backend,
        customer_for=lambda subject: {
            "organization:acme": "cus_acme",
            "organization:globex": None,
        }[subject],
    )

    for subject in ("organization:acme", "organization:globex"):
        await commerce.checkout(
            subject=subject,
            plan="pro",
            success_url="https://app.example/success",
            cancel_url="https://app.example/cancel",
            reference=f"checkout:{subject}",
        )

    acme, globex = backend.checkout_requests
    assert acme.items[0].price == "price_acme_pro"
    assert acme.customer == "cus_acme"
    assert acme.merchant_account == "acct_acme"
    assert globex.items[0].price == "price_globex_pro"
    assert globex.customer is None
    assert globex.merchant_account == "acct_globex"


def test_account_scoped_prices_require_a_safe_inverse_at_startup() -> None:
    with pytest.raises(BillingConfigurationError, match="sku_for_price"):
        Billing(
            "marketplace",
            backend=Backend(),
            catalog=PlanCatalog(Plan("pro", "price_shared_pro")),
            merchant=ConnectedMerchant(),
            capture=HostedRedirect(),
            topology=topology(inverse=False),
        )


def test_account_scoped_price_inverse_rejects_contradictory_tenant_account() -> None:
    commerce = billing(Backend())

    assert (
        commerce.plan_for_provider_price(
            subject="organization:acme",
            provider_price="price_acme_pro",
            merchant_account="acct_acme",
        ).sku
        == "pro"
    )
    with pytest.raises(ValueError, match="merchant account contradicts billing subject"):
        commerce.plan_for_provider_price(
            subject="organization:acme",
            provider_price="price_globex_pro",
            merchant_account="acct_globex",
        )


@pytest.mark.asyncio
async def test_refund_uses_the_original_payment_route_and_validates_currency() -> None:
    backend = Backend()
    commerce = billing(
        backend,
        payment_for=lambda subject, payment: (
            ProviderPayment(
                provider_id="pi_original",
                currency="USD",
                merchant_account="acct_original",
            )
            if (subject, payment) == ("organization:acme", "payment:order-1")
            else None
        ),
    )

    await commerce.refund(
        subject="organization:acme",
        payment="payment:order-1",
        amount=Money("USD", 100),
        reference="refund:partial",
    )
    await commerce.refund(
        subject="organization:acme",
        payment="payment:order-1",
        reference="refund:full",
    )

    partial, full = backend.refund_requests
    assert partial.payment == "pi_original"
    assert partial.merchant_account == "acct_original"
    assert full.merchant_account == "acct_original"

    with pytest.raises(ValueError, match="refund currency 'EUR'.*payment currency 'USD'"):
        await commerce.refund(
            subject="organization:acme",
            payment="payment:order-1",
            amount=Money("EUR", 100),
            reference="refund:wrong-currency",
        )
    assert len(backend.refund_requests) == 2
