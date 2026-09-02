from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal


@dataclass(frozen=True, slots=True)
class Money:
    currency: str
    minor: int

    def __post_init__(self) -> None:
        if type(self.minor) is not int:
            raise TypeError("money must use integer minor units")
        if len(self.currency) != 3 or not self.currency.isascii() or not self.currency.isupper():
            raise ValueError("currency must contain three uppercase ASCII letters")
        if self.minor < 0:
            raise ValueError("money minor units must not be negative")


@dataclass(frozen=True, slots=True)
class CheckoutItem:
    price: str
    quantity: int = 1

    def __post_init__(self) -> None:
        if not self.price:
            raise ValueError("checkout item provider price must not be empty")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("checkout item quantity must be a positive integer")


@dataclass(frozen=True, slots=True)
class CheckoutRequest:
    subject: str
    items: tuple[CheckoutItem, ...]
    mode: Literal["payment", "subscription"]
    success_url: str
    cancel_url: str
    reference: str
    customer: str | None = None
    merchant_account: str | None = None

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("checkout subject must not be empty")
        if not self.items:
            raise ValueError("checkout items must not be empty")
        if self.mode not in {"payment", "subscription"}:
            raise ValueError("checkout mode must be 'payment' or 'subscription'")
        if not self.reference:
            raise ValueError("checkout reference must not be empty")
        if self.customer is not None and (not isinstance(self.customer, str) or not self.customer):
            raise ValueError("checkout customer must be a non-empty string or None")


@dataclass(frozen=True, slots=True)
class CheckoutSession:
    provider: str
    id: str
    url: str
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PortalRequest:
    subject: str
    customer: str
    return_url: str
    reference: str
    merchant_account: str | None = None

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("portal subject must not be empty")
        if not self.customer:
            raise ValueError("portal customer must not be empty")
        if not self.reference:
            raise ValueError("portal reference must not be empty")


@dataclass(frozen=True, slots=True)
class PortalSession:
    provider: str
    id: str
    url: str


class RefundState(StrEnum):
    PENDING = "pending"
    REQUIRES_ACTION = "requires_action"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class PaymentState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PaymentSnapshot:
    provider: str
    id: str
    subject: str
    reference: str
    amount: Money
    state: PaymentState
    customer: str | None = None
    merchant_account: str | None = None

    def __post_init__(self) -> None:
        for field, value in (
            ("provider", self.provider),
            ("payment id", self.id),
            ("subject", self.subject),
            ("reference", self.reference),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"payment {field} must be a non-empty string")
        if not isinstance(self.amount, Money):
            raise TypeError("payment amount must be Money")
        if not isinstance(self.state, PaymentState):
            raise TypeError("payment state must be PaymentState")
        if self.customer is not None and (not isinstance(self.customer, str) or not self.customer):
            raise ValueError("payment customer must be a non-empty string or None")
        if self.merchant_account is not None and (
            not isinstance(self.merchant_account, str) or not self.merchant_account
        ):
            raise ValueError("payment merchant account must be a non-empty string or None")


@dataclass(frozen=True, slots=True)
class ProviderPayment:
    provider_id: str
    currency: str
    merchant_account: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not self.provider_id:
            raise ValueError("provider payment id must be a non-empty string")
        if (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isupper()
        ):
            raise ValueError("provider payment currency must contain three uppercase ASCII letters")
        if self.merchant_account is not None and (
            not isinstance(self.merchant_account, str) or not self.merchant_account
        ):
            raise ValueError("provider payment merchant account must be a non-empty string or None")


@dataclass(frozen=True, slots=True)
class RefundRequest:
    payment: str
    reference: str
    amount: Money | None = None
    merchant_account: str | None = None

    def __post_init__(self) -> None:
        if not self.payment:
            raise ValueError("refund payment must not be empty")
        if not self.reference:
            raise ValueError("refund reference must not be empty")
        if self.amount is not None and self.amount.minor == 0:
            raise ValueError("partial refund amount must be positive")


@dataclass(frozen=True, slots=True)
class Refund:
    provider: str
    id: str
    state: RefundState
    amount: Money


__all__ = [
    "CheckoutItem",
    "CheckoutRequest",
    "CheckoutSession",
    "Money",
    "PaymentSnapshot",
    "PaymentState",
    "PortalRequest",
    "PortalSession",
    "ProviderPayment",
    "Refund",
    "RefundRequest",
    "RefundState",
]
