from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    selected_topology = options.pop("topology", topology())
    return Billing(
        "marketplace",
        backend=backend,
        catalog=PlanCatalog(Plan("pro", "price_shared_pro")),
        merchant=ConnectedMerchant(),
        capture=HostedRedirect(),
        topology=selected_topology,
        **options,
    )


def test_billing_capability_configuration_is_exact() -> None:
    assert BillingCapabilities().merchant == "deployment"
    for name in (
        "hosted_checkout",
        "hosted_portal",
        "subscriptions",
        "connect",
        "refunds",
        "account_scoped_prices",
    ):
        with pytest.raises(TypeError, match=f"billing capability {name} must be bool"):
            BillingCapabilities(**{name: 1})
    with pytest.raises(ValueError, match="merchant must be"):
        BillingCapabilities(merchant="platform")


def test_connected_merchant_callbacks_and_pairing_are_exact() -> None:
    def resolve(*values: object) -> str:
        return "resolved"

    assert ConnectedMerchants(resolve).price_for is None

    with pytest.raises(TypeError, match="account_for must be callable"):
        ConnectedMerchants(None)
    for name in ("price_for", "sku_for_price"):
        options: dict[str, Any] = {
            "price_for": resolve,
            "sku_for_price": resolve,
            name: 1,
        }
        with pytest.raises(TypeError, match=f"{name} must be callable"):
            ConnectedMerchants(resolve, **options)
    for options in (
        {"price_for": resolve},
        {"sku_for_price": resolve},
    ):
        with pytest.raises(BillingConfigurationError, match="require both"):
            ConnectedMerchants(resolve, **options)


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


@pytest.mark.parametrize("sku", [None, "", 1])
def test_account_scoped_price_lookup_refuses_a_missing_inverse_mapping(
    sku: object,
) -> None:
    backend = Backend()
    commerce = billing(
        backend,
        topology=ConnectedMerchants(
            account_for=lambda subject: "acct_acme",
            price_for=lambda subject, plan, account: "price_acme_pro",
            sku_for_price=lambda subject, price, account: sku,
        ),
    )

    with pytest.raises(KeyError, match="no billing plan mapping"):
        commerce.plan_for_provider_price(
            subject="organization:acme",
            provider_price="price_acme_pro",
            merchant_account="acct_acme",
        )


def test_account_scoped_price_lookup_refuses_a_non_inverse_mapping() -> None:
    backend = Backend()
    commerce = billing(
        backend,
        topology=ConnectedMerchants(
            account_for=lambda subject: "acct_acme",
            price_for=lambda subject, plan, account: "price_other",
            sku_for_price=lambda subject, price, account: "pro",
        ),
    )

    with pytest.raises(ValueError, match="price inverse contradicts plan"):
        commerce.plan_for_provider_price(
            subject="organization:acme",
            provider_price="price_acme_pro",
            merchant_account="acct_acme",
        )


@pytest.mark.parametrize("price", [None, "", 1])
@pytest.mark.asyncio
async def test_checkout_refuses_an_invalid_account_scoped_price(price: object) -> None:
    backend = Backend()
    commerce = billing(
        backend,
        topology=ConnectedMerchants(
            account_for=lambda subject: "acct_acme",
            price_for=lambda subject, plan, account: price,
            sku_for_price=lambda subject, provider_price, account: "pro",
        ),
    )

    with pytest.raises(KeyError, match="no provider price mapping"):
        await commerce.checkout(
            subject="organization:acme",
            plan="pro",
            success_url="https://app.example/success",
            cancel_url="https://app.example/cancel",
            reference="checkout:invalid-price",
        )
    assert backend.checkout_requests == []


@pytest.mark.parametrize("customer", ["", 1])
@pytest.mark.asyncio
async def test_checkout_refuses_an_invalid_customer_mapping(customer: object) -> None:
    backend = Backend()
    commerce = billing(backend, customer_for=lambda subject: customer)

    with pytest.raises(ValueError, match="customer_for must return"):
        await commerce.checkout(
            subject="organization:acme",
            plan="pro",
            success_url="https://app.example/success",
            cancel_url="https://app.example/cancel",
            reference="checkout:invalid-customer",
        )
    assert backend.checkout_requests == []


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
