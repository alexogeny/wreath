from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ..payments import (
    CheckoutItem,
    CheckoutRequest,
    CheckoutSession,
    Money,
    PortalRequest,
    PortalSession,
    ProviderPayment,
    Refund,
    RefundRequest,
)
from ..subscriptions import AccessPolicy, Plan, PlanCatalog
from .ledger import (
    BillingCommand,
    BillingCommandIdentity,
    BillingCommandState,
    PostgresBillingLedger,
)
from .operations import BillingOperations, BillingOperationsUnhealthy
from .queries import (
    InvoiceCursor,
    InvoicePage,
    PostgresBillingQueries,
    PostgresSubscriptionEntitlements,
)
from .reconciliation import (
    ReconciliationPage,
    ReconciliationSnapshot,
    StripeReconciliation,
)
from .stripe_webhooks import bind_stripe_webhooks, stripe_webhooks
from .support import (
    BillingAuditEvent,
    BillingSupport,
    MoneyMovementDisabled,
    SupportAccess,
    SupportAccessDisabled,
    SupportMoneyMovement,
)

_DEFAULT_ACCESS = AccessPolicy()


class BillingConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BillingCapabilities:
    hosted_checkout: bool = False
    hosted_portal: bool = False
    subscriptions: bool = False
    connect: bool = False
    refunds: bool = False
    account_scoped_prices: bool = False
    merchant: Literal["deployment", "connected", "provider"] = "deployment"

    def __post_init__(self) -> None:
        for field in (
            "hosted_checkout",
            "hosted_portal",
            "subscriptions",
            "connect",
            "refunds",
            "account_scoped_prices",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"billing capability {field} must be bool")
        if self.merchant not in {"deployment", "connected", "provider"}:
            raise ValueError(
                "billing capability merchant must be 'deployment', 'connected', or 'provider'"
            )

    @property
    def merchant_of_record(self) -> bool:
        return self.merchant == "provider"


@dataclass(frozen=True, slots=True)
class DeploymentMerchant:
    pass


@dataclass(frozen=True, slots=True)
class ProviderMerchant:
    pass


@dataclass(frozen=True, slots=True)
class ConnectedMerchant:
    pass


@dataclass(frozen=True, slots=True)
class HostedRedirect:
    pass


@dataclass(frozen=True, slots=True)
class SharedMerchant:
    pass


_SHARED_MERCHANT = SharedMerchant()


@dataclass(frozen=True, slots=True)
class ConnectedMerchants:
    account_for: Any
    price_for: Any = None
    sku_for_price: Any = None

    def __post_init__(self) -> None:
        if not callable(self.account_for):
            raise TypeError("connected merchant account_for must be callable")
        if self.price_for is not None and not callable(self.price_for):
            raise TypeError("connected merchant price_for must be callable")
        if self.sku_for_price is not None and not callable(self.sku_for_price):
            raise TypeError("connected merchant sku_for_price must be callable")
        if (self.price_for is None) != (self.sku_for_price is None):
            raise BillingConfigurationError(
                "connected merchant account-scoped prices require both price_for and sku_for_price"
            )


@dataclass(frozen=True, slots=True)
class CompliancePosture:
    capture: str
    cardholder_data: str
    candidate_saq: str
    merchant_of_record: str
    unresolved: tuple[str, ...]


class Billing:
    __slots__ = (
        "_customer_for",
        "_payment_for",
        "_reconciliation_configured",
        "_stripe_webhooks_bound",
        "_support_facades",
        "backend",
        "capture",
        "catalog",
        "ledger",
        "merchant",
        "name",
        "operations",
        "topology",
    )

    def __init__(
        self,
        name: str,
        *,
        backend: Any,
        catalog: PlanCatalog,
        merchant: DeploymentMerchant | ConnectedMerchant | ProviderMerchant,
        capture: HostedRedirect,
        topology: SharedMerchant | ConnectedMerchants = _SHARED_MERCHANT,
        customer_for: Any = None,
        payment_for: Any = None,
        ledger: Any = None,
    ) -> None:
        if not name:
            raise BillingConfigurationError("billing control-plane name must not be empty")
        if type(capture) is not HostedRedirect:
            raise BillingConfigurationError(
                "capture must be HostedRedirect; embedded or direct card capture is unsupported"
            )
        if type(merchant) not in {
            DeploymentMerchant,
            ConnectedMerchant,
            ProviderMerchant,
        }:
            raise BillingConfigurationError(
                "merchant must be one of DeploymentMerchant, ConnectedMerchant, or ProviderMerchant"
            )
        if type(topology) not in {SharedMerchant, ConnectedMerchants}:
            raise BillingConfigurationError("topology must be SharedMerchant or ConnectedMerchants")
        capabilities = getattr(backend, "capabilities", None)
        if not isinstance(capabilities, BillingCapabilities):
            raise BillingConfigurationError(
                "billing backend must declare BillingCapabilities as capabilities"
            )
        if not capabilities.hosted_checkout:
            raise BillingConfigurationError("billing backend does not support hosted checkout")
        declared_merchant = (
            "provider"
            if isinstance(merchant, ProviderMerchant)
            else "connected"
            if isinstance(merchant, ConnectedMerchant)
            else "deployment"
        )
        if declared_merchant != capabilities.merchant:
            raise BillingConfigurationError(
                f"billing backend declares merchant {capabilities.merchant!r}; "
                f"merchant of record configuration must use "
                f"{capabilities.merchant.title()}Merchant"
            )
        if isinstance(topology, ConnectedMerchants) and not capabilities.connect:
            raise BillingConfigurationError(
                "connected merchant topology requires a backend supporting Connect"
            )
        if isinstance(topology, SharedMerchant) and capabilities.connect:
            raise BillingConfigurationError(
                "Connect backend requires a connected merchant topology"
            )
        if capabilities.account_scoped_prices:
            if not isinstance(topology, ConnectedMerchants):
                raise BillingConfigurationError(
                    "account-scoped prices require a connected merchant topology"
                )
            if topology.price_for is None:
                raise BillingConfigurationError(
                    "account-scoped prices require price_for(subject, sku, account)"
                )
            if topology.sku_for_price is None:
                raise BillingConfigurationError(
                    "account-scoped prices require sku_for_price(subject, price, account)"
                )
        elif isinstance(topology, ConnectedMerchants) and topology.price_for is not None:
            raise BillingConfigurationError(
                "billing backend does not use account-scoped prices; remove price_for and "
                "sku_for_price"
            )
        subscription_plan = next((plan for plan in catalog if plan.mode == "subscription"), None)
        if subscription_plan is not None and not capabilities.subscriptions:
            raise BillingConfigurationError(
                f"plan {subscription_plan.sku!r} requires a backend supporting subscriptions"
            )
        self.name = name
        self.backend = backend
        self.catalog = catalog
        self.merchant = merchant
        self.capture = capture
        self.topology = topology
        if customer_for is not None and not callable(customer_for):
            raise TypeError("billing customer_for must be callable")
        if payment_for is not None and not callable(payment_for):
            raise TypeError("billing payment_for must be callable")
        if ledger is not None:
            required = (
                "component",
                "register_command",
                "apply_checkout",
                "apply_subscription",
                "apply_payment",
            )
            missing = tuple(name for name in required if not callable(getattr(ledger, name, None)))
            if missing:
                names = ", ".join(f"{name}()" for name in missing)
                raise TypeError(f"billing ledger must provide {names}")
        self._customer_for = customer_for
        self._payment_for = payment_for
        self.ledger = ledger
        self.operations = BillingOperations(name)
        self._stripe_webhooks_bound = False
        self._reconciliation_configured = False
        self._support_facades: list[BillingSupport] = []

    @property
    def schema_owners(self) -> tuple[Any, ...]:
        ledger = () if self.ledger is None else (self.ledger,)
        approvals = tuple(
            owner
            for support in self._support_facades
            for owner in support.schema_owners
        )
        return (*ledger, *approvals)

    @property
    def counter_sources(self) -> tuple[BillingOperations, ...]:
        return (self.operations,)

    def preflight(self) -> tuple[str, ...]:
        findings: list[str] = []
        if self.ledger is None:
            findings.append("configure a durable billing ledger")
        if getattr(self.backend, "provider", None) == "stripe":
            if not self._stripe_webhooks_bound:
                findings.append(
                    "bind the Stripe webhook projection to a durable webhook source"
                )
            if not self._reconciliation_configured:
                findings.append("configure durable Stripe reconciliation")
        return tuple(findings)

    def queries(self, session_factory: Any) -> Any:
        from .queries import PostgresBillingQueries

        if not isinstance(self.ledger, PostgresBillingLedger):
            raise BillingConfigurationError(
                "billing queries require a configured PostgresBillingLedger"
            )
        provider = getattr(self.backend, "provider", None)
        if type(provider) is not str or not provider:
            raise BillingConfigurationError(
                "billing queries require the backend to declare a non-empty provider"
            )
        return PostgresBillingQueries(
            session_factory,
            provider=provider,
            merchant_account_for=self._merchant_account,
            schema=self.ledger._schema,
        )

    def entitlements(
        self,
        queries: Any,
        *,
        subject_for: Any,
        access: AccessPolicy = _DEFAULT_ACCESS,
        now: Any = None,
    ) -> Any:
        from .queries import PostgresSubscriptionEntitlements

        return PostgresSubscriptionEntitlements(
            queries,
            self.catalog,
            subject_for=subject_for,
            access=access,
            now=now,
        )

    def stripe_webhooks(
        self,
        source: Any,
        *,
        webhook: Any,
        checkout_subject_for: Any,
        subscription_subject_for: Any,
    ) -> Any:
        from .stripe_webhooks import bind_stripe_webhooks

        bound = bind_stripe_webhooks(
            source,
            billing=self,
            webhook=webhook,
            checkout_subject_for=checkout_subject_for,
            subscription_subject_for=subscription_subject_for,
            operations=self.operations,
        )
        self._stripe_webhooks_bound = True
        return bound

    def reconciliation(
        self,
        *,
        jobs: Any,
        session_factory: Any,
        retrieve_page: Any,
        merchant_accounts: tuple[str | None, ...] = (None,),
        cron: str | None = "*/15 * * * *",
        limit: int = 100,
    ) -> Any:
        from .reconciliation import StripeReconciliation

        if not isinstance(self.ledger, PostgresBillingLedger):
            raise BillingConfigurationError(
                "Stripe reconciliation requires a configured PostgresBillingLedger"
            )
        reconciliation = StripeReconciliation(
            self.name,
            jobs=jobs,
            session_factory=session_factory,
            state=self.ledger,
            ledger=self.ledger,
            retrieve_page=retrieve_page,
            merchant_accounts=merchant_accounts,
            cron=cron,
            limit=limit,
            operations=self.operations,
        )
        self._reconciliation_configured = True
        return reconciliation

    def support(
        self,
        *,
        reader: Any,
        subject_for: Any,
        access: Any = None,
        money: Any = None,
        max_pending: int = 1024,
    ) -> Any:
        from .support import BillingSupport

        support = BillingSupport(
            billing=self,
            reader=reader,
            subject_for=subject_for,
            access=access,
            money=money,
            max_pending=max_pending,
        )
        self._support_facades.append(support)
        return support

    def _merchant_account(self, subject: str) -> str | None:
        if isinstance(self.topology, SharedMerchant):
            return None
        account = self.topology.account_for(subject)
        if not isinstance(account, str) or not account:
            raise KeyError(f"no merchant account mapping for billing subject {subject!r}")
        return account

    def compliance_posture(self) -> CompliancePosture:
        provider_posture = getattr(self.backend, "compliance_unresolved", None)
        provider_unresolved = () if provider_posture is None else provider_posture()
        if type(provider_unresolved) is not tuple or any(
            type(item) is not str or not item for item in provider_unresolved
        ):
            raise BillingConfigurationError(
                "billing backend compliance_unresolved() must return a tuple of strings"
            )
        return CompliancePosture(
            capture="hosted-redirect",
            cardholder_data="provider-only",
            candidate_saq="SAQ A",
            merchant_of_record=self.backend.capabilities.merchant,
            unresolved=(
                "confirm SAQ with the acquirer or payment brand",
                *(
                    ("configure a durable billing ledger for commands and webhook projections",)
                    if self.ledger is None
                    else ()
                ),
                *provider_unresolved,
            ),
        )

    def plan_for_provider_price(
        self,
        *,
        subject: str,
        provider_price: str,
        merchant_account: str | None = None,
    ) -> Plan:
        if not self.backend.capabilities.account_scoped_prices:
            return self.catalog.for_provider_price(provider_price)
        account = self._merchant_account(subject)
        if merchant_account != account:
            raise ValueError(
                f"merchant account contradicts billing subject {subject!r}: "
                f"expected {account!r}, received {merchant_account!r}"
            )
        topology = self.topology
        if not isinstance(topology, ConnectedMerchants):
            raise BillingConfigurationError(
                "account-scoped prices require a connected merchant topology"
            )
        sku_for_price = topology.sku_for_price
        price_for = topology.price_for
        if not callable(sku_for_price) or not callable(price_for):
            raise BillingConfigurationError(
                "account-scoped prices require price_for and sku_for_price"
            )
        sku = sku_for_price(subject, provider_price, account)
        if not isinstance(sku, str) or not sku:
            raise KeyError(
                f"no billing plan mapping for provider price {provider_price!r} "
                f"and merchant account {account!r}"
            )
        plan = self.catalog[sku]
        if price_for(subject, sku, account) != provider_price:
            raise ValueError(
                f"account-scoped price inverse contradicts plan {sku!r} for "
                f"merchant account {account!r}"
            )
        return plan

    async def checkout(
        self,
        *,
        subject: str,
        plan: str,
        success_url: str,
        cancel_url: str,
        reference: str,
        quantity: int = 1,
    ) -> CheckoutSession:
        declared = self.catalog[plan]
        account = self._merchant_account(subject)
        provider_price = declared.provider_price
        if self.backend.capabilities.account_scoped_prices:
            topology = self.topology
            if not isinstance(topology, ConnectedMerchants) or not callable(topology.price_for):
                raise BillingConfigurationError(
                    "account-scoped prices require price_for(subject, sku, account)"
                )
            resolved_price = topology.price_for(subject, declared.sku, account)
            if not isinstance(resolved_price, str) or not resolved_price:
                raise KeyError(
                    f"no provider price mapping for billing subject {subject!r}, "
                    f"plan {declared.sku!r}, and merchant account {account!r}"
                )
            provider_price = resolved_price
        customer = None
        if self._customer_for is not None:
            customer = self._customer_for(subject)
            if customer is not None and (not isinstance(customer, str) or not customer):
                raise ValueError("billing customer_for must return a non-empty string or None")
        request = CheckoutRequest(
            subject=subject,
            items=(CheckoutItem(provider_price, quantity),),
            mode=declared.mode,
            success_url=success_url,
            cancel_url=cancel_url,
            reference=reference,
            customer=customer,
            merchant_account=account,
        )
        return await self.backend.create_checkout(request, idempotency_key=reference)

    async def portal(
        self,
        *,
        subject: str,
        return_url: str,
        reference: str,
    ) -> PortalSession:
        if not self.backend.capabilities.hosted_portal:
            raise BillingConfigurationError("billing backend does not support hosted portal")
        if self._customer_for is None:
            raise BillingConfigurationError(
                "billing portal requires customer_for(subject) to prevent cross-tenant access"
            )
        customer = self._customer_for(subject)
        if not isinstance(customer, str) or not customer:
            raise KeyError(f"no provider customer mapping for billing subject {subject!r}")
        request = PortalRequest(
            subject=subject,
            customer=customer,
            return_url=return_url,
            reference=reference,
            merchant_account=self._merchant_account(subject),
        )
        return await self.backend.create_portal(request, idempotency_key=reference)

    async def refund(
        self,
        *,
        subject: str,
        payment: str,
        reference: str,
        amount: Money | None = None,
    ) -> Refund:
        if not self.backend.capabilities.refunds:
            raise BillingConfigurationError("billing backend does not support refunds")
        if not isinstance(subject, str) or not subject:
            raise ValueError("billing refund subject must be a non-empty string")
        if self._payment_for is None:
            raise BillingConfigurationError(
                "billing refund requires payment_for(subject, payment) to prevent "
                "cross-tenant access"
            )
        provider_payment = self._payment_for(subject, payment)
        if provider_payment is None:
            raise KeyError(
                f"no provider payment mapping for billing subject {subject!r} "
                f"and payment {payment!r}"
            )
        if not isinstance(provider_payment, ProviderPayment):
            raise TypeError("billing payment_for must return ProviderPayment or None")
        return await self._refund_provider(
            provider_payment,
            reference=reference,
            amount=amount,
        )

    async def _refund_projected(
        self,
        payment: Any,
        *,
        reference: str,
        amount: Money | None = None,
    ) -> Refund:
        from ..payments import PaymentSnapshot, PaymentState

        if not isinstance(payment, PaymentSnapshot):
            raise TypeError("billing projected refund payment must be PaymentSnapshot")
        provider = getattr(self.backend, "provider", None)
        if type(provider) is not str or not provider:
            raise BillingConfigurationError(
                "billing projected refund requires the backend to declare its provider"
            )
        if payment.provider != provider:
            raise ValueError(
                f"projected payment provider {payment.provider!r} differs from "
                f"billing backend provider {provider!r}"
            )
        if payment.state is not PaymentState.SUCCEEDED:
            raise ValueError("billing can refund only a succeeded projected payment")
        if amount is not None and amount.minor > payment.amount.minor:
            raise ValueError("projected refund amount exceeds the payment amount")
        return await self._refund_provider(
            ProviderPayment(
                payment.id,
                payment.amount.currency,
                payment.merchant_account,
            ),
            reference=reference,
            amount=amount,
        )

    async def _refund_provider(
        self,
        provider_payment: ProviderPayment,
        *,
        reference: str,
        amount: Money | None,
    ) -> Refund:
        if not self.backend.capabilities.refunds:
            raise BillingConfigurationError("billing backend does not support refunds")
        if amount is not None and amount.currency != provider_payment.currency:
            raise ValueError(
                f"refund currency {amount.currency!r} does not match payment currency "
                f"{provider_payment.currency!r}"
            )
        if self.backend.capabilities.connect and provider_payment.merchant_account is None:
            raise ValueError("connected provider payment must retain its original merchant account")
        if not self.backend.capabilities.connect and provider_payment.merchant_account is not None:
            raise ValueError("non-Connect provider payment must not carry a merchant account")
        request = RefundRequest(
            payment=provider_payment.provider_id,
            reference=reference,
            amount=amount,
            merchant_account=provider_payment.merchant_account,
        )
        return await self.backend.create_refund(request, idempotency_key=reference)


__all__ = [
    "Billing",
    "BillingAuditEvent",
    "BillingCapabilities",
    "BillingCommand",
    "BillingCommandIdentity",
    "BillingCommandState",
    "BillingConfigurationError",
    "BillingOperations",
    "BillingOperationsUnhealthy",
    "BillingSupport",
    "CompliancePosture",
    "ConnectedMerchant",
    "ConnectedMerchants",
    "DeploymentMerchant",
    "HostedRedirect",
    "InvoiceCursor",
    "InvoicePage",
    "MoneyMovementDisabled",
    "PostgresBillingLedger",
    "PostgresBillingQueries",
    "PostgresSubscriptionEntitlements",
    "ProviderMerchant",
    "ReconciliationPage",
    "ReconciliationSnapshot",
    "SharedMerchant",
    "StripeReconciliation",
    "SupportAccess",
    "SupportAccessDisabled",
    "SupportMoneyMovement",
    "bind_stripe_webhooks",
    "stripe_webhooks",
]
