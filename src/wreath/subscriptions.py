from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class Plan:
    sku: str
    provider_price: str
    entitlements: frozenset[str] = frozenset()
    mode: Literal["payment", "subscription"] = "subscription"

    def __post_init__(self) -> None:
        if not self.sku:
            raise ValueError("plan sku must not be empty")
        if not self.provider_price:
            raise ValueError(f"plan {self.sku!r} provider price must not be empty")
        if isinstance(self.entitlements, str):
            raise TypeError("plan entitlements must be non-empty strings")
        try:
            frozen_entitlements = frozenset(self.entitlements)
        except TypeError as error:
            raise TypeError("plan entitlements must be an iterable of strings") from error
        if any(
            type(entitlement) is not str or not entitlement for entitlement in frozen_entitlements
        ):
            raise TypeError("plan entitlements must be non-empty strings")
        object.__setattr__(self, "entitlements", frozen_entitlements)
        if self.mode not in {"payment", "subscription"}:
            raise ValueError("plan mode must be 'payment' or 'subscription'")


class PlanCatalog:
    __slots__ = ("_by_price", "_by_sku", "_entitlements")

    def __init__(self, *plans: Plan) -> None:
        by_sku: dict[str, Plan] = {}
        by_price: dict[str, Plan] = {}
        entitlements: set[str] = set()
        for plan in plans:
            if plan.sku in by_sku:
                raise ValueError(f"duplicate plan sku {plan.sku!r}")
            if plan.provider_price in by_price:
                raise ValueError(
                    f"provider price {plan.provider_price!r} cannot belong to two plans"
                )
            by_sku[plan.sku] = plan
            by_price[plan.provider_price] = plan
            entitlements.update(plan.entitlements)
        self._by_sku = by_sku
        self._by_price = by_price
        self._entitlements = frozenset(entitlements)

    def __len__(self) -> int:
        return len(self._by_sku)

    def __iter__(self) -> Iterator[Plan]:
        return iter(self._by_sku.values())

    def __getitem__(self, sku: str) -> Plan:
        try:
            return self._by_sku[sku]
        except KeyError:
            known = ", ".join(sorted(self._by_sku)) or "none"
            raise KeyError(f"unknown billing plan {sku!r}; configured: {known}") from None

    def for_provider_price(self, provider_price: str) -> Plan:
        try:
            return self._by_price[provider_price]
        except KeyError:
            known = ", ".join(sorted(self._by_price)) or "none"
            raise KeyError(
                f"unknown provider price {provider_price!r}; configured: {known}"
            ) from None

    @property
    def entitlement_names(self) -> frozenset[str]:
        return self._entitlements


class SubscriptionState(StrEnum):
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    UNPAID = "unpaid"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class SubscriptionSnapshot:
    provider: str
    id: str
    subject: str
    plan: str
    state: SubscriptionState
    provider_state: str
    paid_through: datetime | None = None
    trial_ends_at: datetime | None = None
    merchant_account: str | None = None

    def __post_init__(self) -> None:
        for field, value in (
            ("paid_through", self.paid_through),
            ("trial_ends_at", self.trial_ends_at),
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"subscription {field} must include a timezone")
        if self.merchant_account is not None and (
            not isinstance(self.merchant_account, str) or not self.merchant_account
        ):
            raise ValueError("subscription merchant_account must be a non-empty string or None")


@dataclass(frozen=True, slots=True)
class SubscriptionPayment:
    provider: str
    invoice: str
    subscription: str
    subject: str
    paid_through: datetime
    merchant_account: str | None = None

    def __post_init__(self) -> None:
        if self.paid_through.tzinfo is None:
            raise ValueError("subscription payment paid_through must include a timezone")
        if self.merchant_account is not None and (
            not isinstance(self.merchant_account, str) or not self.merchant_account
        ):
            raise ValueError("subscription payment merchant_account must be non-empty or None")


class SubscriptionLedger:
    __slots__ = ("_invoices", "_owners", "_paid_through", "_snapshots")

    def __init__(self) -> None:
        self._snapshots: dict[tuple[str, str, str], SubscriptionSnapshot] = {}
        self._paid_through: dict[tuple[str, str, str], datetime] = {}
        self._owners: dict[str, tuple[str, str, str | None]] = {}
        self._invoices: dict[tuple[str, str], SubscriptionPayment] = {}

    def apply(
        self, value: SubscriptionSnapshot | SubscriptionPayment
    ) -> SubscriptionSnapshot | None:
        if isinstance(value, SubscriptionSnapshot):
            subscription = value.id
        elif isinstance(value, SubscriptionPayment):
            subscription = value.subscription
        else:
            raise TypeError(
                "subscription ledger accepts SubscriptionSnapshot or SubscriptionPayment"
            )
        self._bind_owner(
            subscription,
            value.provider,
            value.subject,
            value.merchant_account,
        )
        key = (value.provider, subscription, value.subject)
        if isinstance(value, SubscriptionPayment):
            self._record_payment(value, key)
            current = self._snapshots.get(key)
            if current is None:
                return None
            merged = self._with_paid_through(current, value.paid_through)
            self._snapshots[key] = merged
            return merged
        paid_through = self._paid_through.get(key)
        merged = value if paid_through is None else self._with_paid_through(value, paid_through)
        self._snapshots[key] = merged
        return merged

    def get(self, provider: str, subscription: str, subject: str) -> SubscriptionSnapshot | None:
        return self._snapshots.get((provider, subscription, subject))

    def _bind_owner(
        self,
        subscription: str,
        provider: str,
        subject: str,
        merchant_account: str | None,
    ) -> None:
        owner = self._owners.get(subscription)
        if owner is None:
            self._owners[subscription] = (provider, subject, merchant_account)
            return
        known_provider, known_subject, known_account = owner
        if provider != known_provider:
            raise ValueError(
                f"subscription {subscription!r} changed provider from "
                f"{known_provider!r} to {provider!r}"
            )
        if subject != known_subject:
            raise ValueError(
                f"subscription {subscription!r} changed subject from "
                f"{known_subject!r} to {subject!r}"
            )
        if merchant_account != known_account:
            raise ValueError(
                f"subscription {subscription!r} changed merchant account from "
                f"{known_account!r} to {merchant_account!r}"
            )

    def _record_payment(
        self,
        payment: SubscriptionPayment,
        key: tuple[str, str, str],
    ) -> None:
        invoice_key = (payment.provider, payment.invoice)
        existing = self._invoices.get(invoice_key)
        if existing is not None and existing != payment:
            raise ValueError(
                f"subscription payment invoice {payment.invoice!r} contradicts its first value"
            )
        self._invoices[invoice_key] = payment
        current = self._paid_through.get(key)
        if current is None or payment.paid_through > current:
            self._paid_through[key] = payment.paid_through

    @staticmethod
    def _with_paid_through(
        snapshot: SubscriptionSnapshot, paid_through: datetime
    ) -> SubscriptionSnapshot:
        current = snapshot.paid_through
        if current is not None and current >= paid_through:
            return snapshot
        return replace(snapshot, paid_through=paid_through)


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    granted: frozenset[SubscriptionState] = frozenset(
        {SubscriptionState.TRIALING, SubscriptionState.ACTIVE}
    )

    def __post_init__(self) -> None:
        try:
            granted = frozenset(self.granted)
        except TypeError as error:
            raise TypeError(
                "subscription access granted must contain SubscriptionState members"
            ) from error
        if any(not isinstance(state, SubscriptionState) for state in granted):
            raise TypeError("subscription access granted must contain SubscriptionState members")
        object.__setattr__(self, "granted", granted)


_DEFAULT_ACCESS_POLICY = AccessPolicy()


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SubscriptionAccess:
    plan: str | None
    entitlements: frozenset[str]


_NO_SUBSCRIPTION_ACCESS = SubscriptionAccess(None, frozenset())


class SubscriptionEntitlements:
    __slots__ = (
        "_access",
        "_catalog",
        "_now",
        "_subscription_for",
        "_subscription_for_request",
    )

    def __init__(
        self,
        catalog: PlanCatalog,
        *,
        subscription_for: Callable[[Any], SubscriptionSnapshot | None] | None = None,
        subscription_for_request: Callable[[Any], SubscriptionSnapshot | None] | None = None,
        access: AccessPolicy = _DEFAULT_ACCESS_POLICY,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if (subscription_for is None) == (subscription_for_request is None):
            raise ValueError(
                "configure exactly one of subscription_for or subscription_for_request"
            )
        self._catalog = catalog
        self._subscription_for = subscription_for
        self._subscription_for_request = subscription_for_request
        self._access = access
        self._now = now

    def _granted_plan(self, subscription: SubscriptionSnapshot | None) -> Plan | None:
        if subscription is None or subscription.state not in self._access.granted:
            return None
        current = self._now()
        if current.tzinfo is None:
            raise ValueError("subscription entitlement clock must include a timezone")
        if subscription.state is SubscriptionState.TRIALING:
            if subscription.trial_ends_at is None or subscription.trial_ends_at <= current:
                return None
        elif subscription.paid_through is None or subscription.paid_through <= current:
            return None
        return self._catalog[subscription.plan]

    def entitlements(self, identity: Any) -> frozenset[str]:
        return self.resolve(identity).entitlements

    def plan_for(self, identity: Any) -> str | None:
        return self.resolve(identity).plan

    def resolve(self, identity: Any) -> SubscriptionAccess:
        if self._subscription_for is None:
            raise TypeError(
                "request-aware subscription entitlements require resolve_request(request)"
            )
        plan = self._granted_plan(self._subscription_for(identity))
        if plan is None:
            return _NO_SUBSCRIPTION_ACCESS
        return SubscriptionAccess(plan.sku, plan.entitlements)

    def for_request(self, request: Any) -> frozenset[str]:
        return self.resolve_request(request).entitlements

    def plan_for_request(self, request: Any) -> str | None:
        return self.resolve_request(request).plan

    def resolve_request(self, request: Any) -> SubscriptionAccess:
        plan = self._granted_plan(self._snapshot_for_request(request))
        if plan is None:
            return _NO_SUBSCRIPTION_ACCESS
        return SubscriptionAccess(plan.sku, plan.entitlements)

    def _snapshot_for_request(self, request: Any) -> SubscriptionSnapshot | None:
        by_request = self._subscription_for_request
        if by_request is not None:
            return by_request(request)
        by_identity = self._subscription_for
        if by_identity is None:
            raise RuntimeError("subscription entitlement resolver is not configured")
        return by_identity(request.identity)

    def names(self) -> frozenset[str]:
        return self._catalog.entitlement_names


__all__ = [
    "AccessPolicy",
    "Plan",
    "PlanCatalog",
    "SubscriptionAccess",
    "SubscriptionEntitlements",
    "SubscriptionLedger",
    "SubscriptionPayment",
    "SubscriptionSnapshot",
    "SubscriptionState",
]
