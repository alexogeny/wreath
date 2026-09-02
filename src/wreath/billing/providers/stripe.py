from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

from ...config import Secret
from ...payments import (
    CheckoutRequest,
    CheckoutSession,
    Money,
    PortalRequest,
    PortalSession,
    Refund,
    RefundRequest,
    RefundState,
)
from ...subscriptions import (
    Plan,
    PlanCatalog,
    SubscriptionPayment,
    SubscriptionSnapshot,
    SubscriptionState,
)
from ...webhooks import WebhookEnvelope
from .. import BillingCapabilities

_ACCOUNT = re.compile(r"acct_[A-Za-z0-9]+\Z")
_CHECKOUT_SESSION = re.compile(r"cs_[A-Za-z0-9_]+\Z")
_SUBSCRIPTION = re.compile(r"sub_[A-Za-z0-9]+\Z")
_VERSION = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\.(?P<release>[a-z][a-z0-9_]*)\Z"
)
_MANAGED_PAYMENTS_MINIMUM = date(2025, 3, 31)
_FEE_PERCENT = re.compile(r"(?:0|[1-9]\d{0,2})(?:\.\d{1,2})?\Z")
_INVALID_JSON = object()


class StripeError(RuntimeError):
    pass


def _fee_percent(value: str | None) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _FEE_PERCENT.fullmatch(value) is None:
        raise ValueError(
            "Stripe application_fee_percent must be a decimal string with at most "
            "two decimal places"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Stripe application_fee_percent must be a decimal string") from error
    if not parsed.is_finite() or parsed < 0 or parsed > 100:
        raise ValueError("Stripe application_fee_percent must be between 0 and 100")
    return value


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError:
        return _INVALID_JSON


def _async_callable(value: Any) -> bool:
    if not callable(value):
        return False
    return inspect.iscoroutinefunction(value) or inspect.iscoroutinefunction(type(value).__call__)


def _projected_account(
    metadata: Any,
    event_account: str | None,
    resource: str,
) -> str | None:
    if metadata is None:
        return event_account
    if not isinstance(metadata, dict):
        raise ValueError(f"Stripe {resource} metadata must be an object")
    stored = metadata.get("wreath_merchant_account")
    if stored is None:
        return event_account
    if not isinstance(stored, str) or _ACCOUNT.fullmatch(stored) is None:
        raise ValueError(f"Stripe {resource} has an invalid Wreath merchant account")
    if event_account is not None and event_account != stored:
        raise ValueError(f"Stripe {resource} merchant account contradicts webhook account")
    return stored


def _fee_amount(value: int | None) -> int | None:
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError("Stripe application_fee_amount must be positive integer minor units")
    return value


@dataclass(frozen=True, slots=True)
class DirectCharges:
    application_fee_percent: str | None = None
    application_fee_amount: int | None = None
    refund_application_fee: bool = False

    def __post_init__(self) -> None:
        if type(self.refund_application_fee) is not bool:
            raise TypeError("Stripe refund_application_fee must be bool")
        object.__setattr__(
            self, "application_fee_percent", _fee_percent(self.application_fee_percent)
        )
        object.__setattr__(self, "application_fee_amount", _fee_amount(self.application_fee_amount))


@dataclass(frozen=True, slots=True)
class DestinationRefunds:
    reverse_transfer: bool
    refund_application_fee: bool

    def __post_init__(self) -> None:
        if type(self.reverse_transfer) is not bool:
            raise TypeError("Stripe reverse_transfer must be bool")
        if type(self.refund_application_fee) is not bool:
            raise TypeError("Stripe refund_application_fee must be bool")


@dataclass(frozen=True, slots=True)
class DestinationCharges:
    refunds: DestinationRefunds
    application_fee_percent: str | None = None
    application_fee_amount: int | None = None
    on_behalf_of: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.refunds, DestinationRefunds):
            raise TypeError("Stripe destination refunds must be DestinationRefunds")
        if type(self.on_behalf_of) is not bool:
            raise TypeError("Stripe on_behalf_of must be bool")
        object.__setattr__(
            self, "application_fee_percent", _fee_percent(self.application_fee_percent)
        )
        object.__setattr__(self, "application_fee_amount", _fee_amount(self.application_fee_amount))


@dataclass(frozen=True, slots=True)
class SeparateChargesAndTransfers:
    pass


@dataclass(frozen=True, slots=True)
class StripeConnect:
    charges: DirectCharges | DestinationCharges

    def __post_init__(self) -> None:
        if isinstance(self.charges, SeparateChargesAndTransfers):
            raise ValueError(
                "Stripe separate charges and transfers require a settlement ledger and "
                "are unsupported"
            )
        if not isinstance(self.charges, DirectCharges | DestinationCharges):
            raise TypeError("Stripe Connect charges must be DirectCharges or DestinationCharges")


@dataclass(frozen=True, slots=True)
class StripeWebhookPolicy:
    event_version: str
    livemode: bool
    scope: Literal["account", "connected_accounts"]

    def __post_init__(self) -> None:
        if _stripe_version_date(self.event_version) < _MANAGED_PAYMENTS_MINIMUM:
            raise ValueError("Stripe webhook event version must be 2025-03-31.basil or later")
        if type(self.livemode) is not bool:
            raise TypeError("Stripe webhook livemode must be bool")
        if self.scope not in {"account", "connected_accounts"}:
            raise ValueError("Stripe webhook scope must be 'account' or 'connected_accounts'")

    def validate(self, envelope: WebhookEnvelope, payload: dict[str, Any]) -> str | None:
        event_version = payload.get("api_version")
        if event_version != self.event_version or envelope.version != self.event_version:
            raise ValueError(f"Stripe webhook event version must be {self.event_version!r}")
        livemode = payload.get("livemode")
        if type(livemode) is not bool or livemode is not self.livemode:
            raise ValueError(f"Stripe webhook livemode must be {self.livemode!r}")
        account = payload.get("account")
        if account is not None and (
            not isinstance(account, str) or _ACCOUNT.fullmatch(account) is None
        ):
            raise ValueError("Stripe webhook account is invalid")
        if self.scope == "connected_accounts" and account is None:
            raise ValueError("Stripe connected-account webhook requires account")
        if self.scope == "account" and account is not None:
            raise ValueError("Stripe account webhook must not carry connected account")
        return account


def _https_url(value: str, field: str) -> Any:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{field} must be an absolute HTTPS URL")
    try:
        _port = parsed.port
    except ValueError as error:
        raise ValueError(f"{field} must use a valid HTTPS port") from error
    return parsed


def _return_origin(value: str) -> str:
    parsed = _https_url(value, "allowed return origin")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("allowed return origin must contain only scheme, host, and optional port")
    port = parsed.port
    suffix = "" if port in {None, 443} else f":{port}"
    return f"https://{parsed.hostname.lower()}{suffix}"


def _stripe_version_date(value: str) -> date:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ValueError("Stripe api_version must use YYYY-MM-DD.release")
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError as error:
        raise ValueError("Stripe api_version must start with a valid YYYY-MM-DD") from error


class StripeBilling:
    provider = "stripe"

    __slots__ = (
        "_api_key",
        "_api_version",
        "_client",
        "_connect",
        "_managed_payments",
        "_return_origins",
        "capabilities",
    )

    def __init__(
        self,
        *,
        client: Any,
        api_key: Secret[str],
        api_version: str,
        allowed_return_origins: tuple[str, ...],
        managed_payments: bool = False,
        connect: StripeConnect | None = None,
    ) -> None:
        version_date = _stripe_version_date(api_version)
        if type(managed_payments) is not bool:
            raise TypeError("Stripe Managed Payments enabled flag must be bool")
        if connect is not None and not isinstance(connect, StripeConnect):
            raise TypeError("Stripe connect must be StripeConnect or None")
        if type(allowed_return_origins) is not tuple or not allowed_return_origins:
            raise ValueError("Stripe allowed_return_origins must be a non-empty tuple")
        return_origins = frozenset(_return_origin(value) for value in allowed_return_origins)
        if managed_payments and version_date < _MANAGED_PAYMENTS_MINIMUM:
            raise ValueError(
                "Managed Payments requires Stripe API version 2025-03-31.basil or later"
            )
        if managed_payments and connect is not None:
            raise ValueError("Managed Payments does not support Connect")
        revealed = api_key.reveal()
        if not isinstance(revealed, str) or not revealed:
            raise ValueError("Stripe api_key must reveal a non-empty string")
        try:
            revealed.encode("ascii")
            api_version.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("Stripe api_key and api_version must be ASCII") from error
        self._client = client
        self._api_key = api_key
        self._api_version = api_version
        self._managed_payments = managed_payments
        self._connect = connect
        self._return_origins = return_origins
        self.capabilities = BillingCapabilities(
            hosted_checkout=True,
            hosted_portal=True,
            subscriptions=True,
            connect=connect is not None,
            refunds=True,
            account_scoped_prices=(
                connect is not None and isinstance(connect.charges, DirectCharges)
            ),
            merchant=(
                "provider"
                if managed_payments
                else "connected"
                if connect is not None
                and (isinstance(connect.charges, DirectCharges) or connect.charges.on_behalf_of)
                else "deployment"
            ),
        )

    def _headers(self, *, form: bool = True) -> list[tuple[bytes, bytes]]:
        headers = [
            (b"authorization", f"Bearer {self._api_key.reveal()}".encode("ascii")),
            (b"stripe-version", self._api_version.encode("ascii")),
        ]
        if form:
            headers.insert(1, (b"content-type", b"application/x-www-form-urlencoded"))
        return headers

    def compliance_unresolved(self) -> tuple[str, ...]:
        if self._managed_payments:
            return (
                "confirm every Stripe product has a Managed Payments eligible tax code",
                "activate Managed Payments and accept its terms in the Stripe Dashboard",
                "confirm business and product eligibility for Managed Payments",
            )
        if self._connect is not None:
            return ("confirm connected-account onboarding and merchant responsibilities",)
        return ()

    def _return_url(self, value: str, field: str) -> None:
        parsed = _https_url(value, field)
        port = parsed.port
        suffix = "" if port in {None, 443} else f":{port}"
        origin = f"https://{parsed.hostname.lower()}{suffix}"
        if origin not in self._return_origins:
            known = ", ".join(sorted(self._return_origins))
            raise ValueError(
                f"{field} uses undeclared return origin {origin!r}; configured: {known}"
            )

    @staticmethod
    def _account(value: str | None, operation: str) -> str:
        if value is None:
            raise ValueError(f"Connect {operation} requires merchant_account")
        if _ACCOUNT.fullmatch(value) is None:
            raise ValueError("Connect merchant_account must be a Stripe acct_ identifier")
        return value

    async def _post_object(
        self,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...],
        fields: list[tuple[str, str | int]],
        idempotency_key: str,
        operation: str,
    ) -> dict[str, Any]:
        response = await self._client.post(
            target,
            headers=headers,
            body=urlencode(fields).encode("ascii"),
            idempotency_key=idempotency_key,
        )
        if response.status < 200 or response.status >= 300:
            raise StripeError(f"Stripe {operation} failed with HTTP status {response.status}")
        payload = _decode_json(response.body)
        if payload is _INVALID_JSON:
            raise StripeError(f"Stripe {operation} returned invalid JSON")
        if not isinstance(payload, dict):
            raise StripeError(f"Stripe {operation} returned a non-object response")
        return payload

    async def create_checkout(
        self, request: CheckoutRequest, *, idempotency_key: str
    ) -> CheckoutSession:
        self._return_url(request.success_url, "success_url")
        self._return_url(request.cancel_url, "cancel_url")
        headers = self._headers()
        fields: list[tuple[str, str | int]] = [
            ("mode", request.mode),
            ("success_url", request.success_url),
            ("cancel_url", request.cancel_url),
            ("client_reference_id", request.reference),
        ]
        if self._managed_payments:
            fields.append(("managed_payments[enabled]", "true"))
        if request.customer is not None:
            fields.append(("customer", request.customer))
        connect = self._connect
        if connect is not None:
            account = self._account(request.merchant_account, "checkout")
            fee_percent = connect.charges.application_fee_percent
            fee_amount = connect.charges.application_fee_amount
            if request.mode == "payment" and fee_percent is not None and fee_amount is None:
                raise ValueError(
                    "Stripe Connect fee uses application_fee_percent; use "
                    "application_fee_amount for payment mode"
                )
            if request.mode == "subscription" and fee_amount is not None and fee_percent is None:
                raise ValueError(
                    "Stripe Connect fee uses application_fee_amount; use "
                    "application_fee_percent for subscription mode"
                )
            if isinstance(connect.charges, DirectCharges):
                headers.append((b"stripe-account", account.encode("ascii")))
                if (
                    request.mode == "subscription"
                    and connect.charges.application_fee_percent is not None
                ):
                    fields.append(
                        (
                            "subscription_data[application_fee_percent]",
                            connect.charges.application_fee_percent,
                        )
                    )
                if request.mode == "payment" and connect.charges.application_fee_amount is not None:
                    fields.append(
                        (
                            "payment_intent_data[application_fee_amount]",
                            connect.charges.application_fee_amount,
                        )
                    )
            else:
                prefix = (
                    "subscription_data" if request.mode == "subscription" else "payment_intent_data"
                )
                fields.append((f"{prefix}[transfer_data][destination]", account))
                fields.append(("metadata[wreath_merchant_account]", account))
                fields.append((f"{prefix}[metadata][wreath_merchant_account]", account))
                if (
                    request.mode == "subscription"
                    and connect.charges.application_fee_percent is not None
                ):
                    fields.append(
                        (
                            "subscription_data[application_fee_percent]",
                            connect.charges.application_fee_percent,
                        )
                    )
                if request.mode == "payment" and connect.charges.application_fee_amount is not None:
                    fields.append(
                        (
                            "payment_intent_data[application_fee_amount]",
                            connect.charges.application_fee_amount,
                        )
                    )
                if connect.charges.on_behalf_of:
                    fields.append((f"{prefix}[on_behalf_of]", account))
        for index, item in enumerate(request.items):
            fields.append((f"line_items[{index}][price]", item.price))
            fields.append((f"line_items[{index}][quantity]", item.quantity))
        payload = await self._post_object(
            "/v1/checkout/sessions",
            headers=tuple(headers),
            fields=fields,
            idempotency_key=idempotency_key,
            operation="checkout",
        )
        session_id = payload.get("id")
        checkout_url = payload.get("url")
        if not isinstance(session_id, str) or not session_id:
            raise StripeError("Stripe checkout response is missing id")
        if not isinstance(checkout_url, str):
            raise StripeError("Stripe checkout response is missing url")
        parsed_url = urlsplit(checkout_url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "checkout.stripe.com"
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.port not in {None, 443}
        ):
            raise ValueError("Stripe checkout URL must use checkout.stripe.com over HTTPS")
        expires_at = payload.get("expires_at")
        if expires_at is not None and type(expires_at) is not int:
            raise StripeError("Stripe checkout response expires_at must be a timestamp")
        try:
            resolved_expiry = (
                None if expires_at is None else datetime.fromtimestamp(expires_at, UTC)
            )
        except (OverflowError, OSError, ValueError) as error:
            raise StripeError("Stripe checkout response expires_at must be a timestamp") from error
        return CheckoutSession(
            provider="stripe",
            id=session_id,
            url=checkout_url,
            expires_at=resolved_expiry,
        )

    async def create_portal(self, request: PortalRequest, *, idempotency_key: str) -> PortalSession:
        self._return_url(request.return_url, "return_url")
        headers = self._headers()
        fields: list[tuple[str, str | int]] = [
            ("customer", request.customer),
            ("return_url", request.return_url),
        ]
        connect = self._connect
        if connect is not None and isinstance(connect.charges, DirectCharges):
            account = self._account(request.merchant_account, "portal")
            headers.append((b"stripe-account", account.encode("ascii")))
        elif (
            connect is not None
            and isinstance(connect.charges, DestinationCharges)
            and connect.charges.on_behalf_of
        ):
            account = self._account(request.merchant_account, "portal")
            fields.append(("on_behalf_of", account))
        payload = await self._post_object(
            "/v1/billing_portal/sessions",
            headers=tuple(headers),
            fields=fields,
            idempotency_key=idempotency_key,
            operation="customer portal",
        )
        session_id = payload.get("id")
        portal_url = payload.get("url")
        if not isinstance(session_id, str) or not session_id:
            raise StripeError("Stripe customer portal response is missing id")
        if not isinstance(portal_url, str):
            raise StripeError("Stripe customer portal response is missing url")
        parsed = urlsplit(portal_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "billing.stripe.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            raise ValueError("Stripe customer portal URL must use billing.stripe.com over HTTPS")
        return PortalSession(provider="stripe", id=session_id, url=portal_url)

    async def create_refund(self, request: RefundRequest, *, idempotency_key: str) -> Refund:
        headers = self._headers()
        fields: list[tuple[str, str | int]] = [("payment_intent", request.payment)]
        if request.amount is not None:
            fields.append(("amount", request.amount.minor))
        connect = self._connect
        if connect is None:
            if request.merchant_account is not None:
                raise ValueError("merchant_account requires Stripe Connect")
        elif isinstance(connect.charges, DirectCharges):
            account = self._account(request.merchant_account, "refund")
            headers.append((b"stripe-account", account.encode("ascii")))
        else:
            self._account(request.merchant_account, "refund")
        if connect is not None and isinstance(connect.charges, DirectCharges):
            if connect.charges.refund_application_fee:
                fields.append(("refund_application_fee", "true"))
        elif connect is not None and isinstance(connect.charges, DestinationCharges):
            if connect.charges.refunds.reverse_transfer:
                fields.append(("reverse_transfer", "true"))
            if connect.charges.refunds.refund_application_fee:
                fields.append(("refund_application_fee", "true"))
        payload = await self._post_object(
            "/v1/refunds",
            headers=tuple(headers),
            fields=fields,
            idempotency_key=idempotency_key,
            operation="refund",
        )
        refund_id = payload.get("id")
        status = payload.get("status")
        amount = payload.get("amount")
        currency = payload.get("currency")
        if not isinstance(refund_id, str) or not refund_id:
            raise StripeError("Stripe refund response is missing id")
        try:
            state = RefundState(status)
        except (TypeError, ValueError) as error:
            raise StripeError(
                f"Stripe refund response has unsupported status {status!r}"
            ) from error
        if type(amount) is not int or amount < 0:
            raise StripeError("Stripe refund response has invalid amount")
        if not isinstance(currency, str):
            raise StripeError("Stripe refund response has invalid currency")
        try:
            money = Money(currency.upper(), amount)
        except (TypeError, ValueError) as error:
            raise StripeError("Stripe refund response has invalid currency") from error
        return Refund(provider="stripe", id=refund_id, state=state, amount=money)

    async def retrieve_subscription(
        self, subscription_id: str, merchant_account: str | None
    ) -> dict[str, Any]:
        if type(subscription_id) is not str or _SUBSCRIPTION.fullmatch(subscription_id) is None:
            raise ValueError("Stripe subscription_id must be a sub_ identifier")
        headers = self._headers(form=False)
        connect = self._connect
        if connect is not None and isinstance(connect.charges, DirectCharges):
            account = self._account(merchant_account, "subscription retrieval")
            headers.append((b"stripe-account", account.encode("ascii")))
        elif merchant_account is not None:
            raise ValueError(
                "Stripe subscription retrieval merchant_account requires direct Connect"
            )
        response = await self._client.get(
            f"/v1/subscriptions/{subscription_id}", headers=tuple(headers)
        )
        if response.status < 200 or response.status >= 300:
            raise StripeError(
                f"Stripe subscription retrieval failed with HTTP status {response.status}"
            )
        payload = _decode_json(response.body)
        if payload is _INVALID_JSON:
            raise StripeError("Stripe subscription retrieval returned invalid JSON")
        if not isinstance(payload, dict):
            raise StripeError("Stripe subscription retrieval returned a non-object response")
        return payload

    async def retrieve_checkout(
        self, session_id: str, merchant_account: str | None
    ) -> dict[str, Any]:
        if type(session_id) is not str or _CHECKOUT_SESSION.fullmatch(session_id) is None:
            raise ValueError("Stripe session_id must be a cs_ identifier")
        headers = self._headers(form=False)
        connect = self._connect
        if connect is not None and isinstance(connect.charges, DirectCharges):
            account = self._account(merchant_account, "Checkout retrieval")
            headers.append((b"stripe-account", account.encode("ascii")))
        elif merchant_account is not None:
            raise ValueError("Stripe Checkout retrieval merchant_account requires direct Connect")
        response = await self._client.get(
            f"/v1/checkout/sessions/{session_id}", headers=tuple(headers)
        )
        if response.status < 200 or response.status >= 300:
            raise StripeError(
                f"Stripe Checkout retrieval failed with HTTP status {response.status}"
            )
        payload = _decode_json(response.body)
        if payload is _INVALID_JSON:
            raise StripeError("Stripe Checkout retrieval returned invalid JSON")
        if not isinstance(payload, dict):
            raise StripeError("Stripe Checkout retrieval returned a non-object response")
        return payload


class StripeSubscriptionProjection:
    __slots__ = (
        "_catalog",
        "_plan_for_price",
        "_retrieve_subscription",
        "_subject_for",
        "_webhook",
    )

    def __init__(
        self,
        catalog: PlanCatalog,
        *,
        webhook: StripeWebhookPolicy,
        subject_for: Any,
        retrieve_subscription: Any,
        plan_for_price: Any = None,
    ) -> None:
        if not callable(subject_for):
            raise TypeError("Stripe subscription subject_for must be callable")
        if not _async_callable(retrieve_subscription):
            raise TypeError("Stripe subscription retrieve_subscription must be async callable")
        if plan_for_price is not None and not callable(plan_for_price):
            raise TypeError("Stripe subscription plan_for_price must be callable")
        if webhook.scope == "connected_accounts" and plan_for_price is None:
            raise TypeError("Stripe connected subscription projection requires plan_for_price")
        self._catalog = catalog
        self._subject_for = subject_for
        self._retrieve_subscription = retrieve_subscription
        self._plan_for_price = plan_for_price
        self._webhook = webhook

    async def project(self, envelope: WebhookEnvelope) -> SubscriptionSnapshot | None:
        if not envelope.type.startswith("customer.subscription."):
            return None
        payload = _decode_json(envelope.body)
        if payload is _INVALID_JSON:
            raise ValueError("Stripe subscription webhook body is not valid JSON")
        if not isinstance(payload, dict):
            raise ValueError("Stripe subscription webhook body must be an object")
        if payload.get("id") != envelope.id or payload.get("type") != envelope.type:
            raise ValueError("Stripe subscription webhook envelope differs from its body")
        account = self._webhook.validate(envelope, payload)
        data = payload.get("data")
        event_subscription = data.get("object") if isinstance(data, dict) else None
        if not isinstance(event_subscription, dict):
            raise ValueError("Stripe subscription webhook is missing data.object")
        subscription_id = event_subscription.get("id")
        if type(subscription_id) is not str or _SUBSCRIPTION.fullmatch(subscription_id) is None:
            raise ValueError("Stripe subscription webhook has an invalid subscription id")
        subscription = await self._retrieve_subscription(subscription_id, account)
        if not isinstance(subscription, dict):
            raise TypeError("Stripe subscription retrieve_subscription must return an object")
        if subscription.get("id") != subscription_id:
            raise ValueError("Stripe current subscription id differs from webhook resource id")
        customer = subscription.get("customer")
        status = subscription.get("status")
        if not isinstance(customer, str) or not customer:
            raise ValueError("Stripe current subscription is missing customer id")
        subject = self._subject_for(customer, account)
        if not isinstance(subject, str) or not subject:
            raise KeyError(f"no billing subject mapping for Stripe customer {customer!r}")
        projected_account = _projected_account(
            subscription.get("metadata"), account, "subscription"
        )
        items = subscription.get("items")
        entries = items.get("data") if isinstance(items, dict) else None
        if isinstance(items, dict) and items.get("has_more") is True:
            raise ValueError(
                "Stripe current subscription items are truncated; retrieve all "
                "subscription items before projection"
            )
        if (
            isinstance(items, dict)
            and items.get("has_more") is not None
            and type(items.get("has_more")) is not bool
        ):
            raise ValueError("Stripe current subscription items.has_more must be bool")
        if not isinstance(entries, list) or not entries:
            raise ValueError("Stripe current subscription has no price items")
        prices: set[str] = set()
        for entry in entries:
            price = entry.get("price") if isinstance(entry, dict) else None
            price_id = price.get("id") if isinstance(price, dict) else None
            if not isinstance(price_id, str) or not price_id:
                raise ValueError("Stripe current subscription has an invalid price item")
            prices.add(price_id)
        if len(prices) != 1:
            raise ValueError("Stripe current subscription must resolve to exactly one plan price")
        provider_price = next(iter(prices))
        if self._plan_for_price is None:
            plan = self._catalog.for_provider_price(provider_price)
        else:
            plan = self._plan_for_price(
                subject=subject,
                provider_price=provider_price,
                merchant_account=account,
            )
            if not isinstance(plan, Plan):
                raise TypeError("Stripe subscription plan_for_price must return Plan")
        try:
            state = SubscriptionState(status)
        except (TypeError, ValueError) as error:
            raise ValueError(f"unsupported Stripe subscription status {status!r}") from error
        trial_ends_at = self._timestamp(subscription.get("trial_end"), "trial_end")
        return SubscriptionSnapshot(
            provider="stripe",
            id=subscription_id,
            subject=subject,
            plan=plan.sku,
            state=state,
            provider_state=status,
            paid_through=None,
            trial_ends_at=trial_ends_at,
            merchant_account=projected_account,
        )

    @staticmethod
    def _timestamp(value: Any, field: str) -> datetime | None:
        if value is None:
            return None
        if type(value) is not int:
            raise ValueError(f"Stripe subscription webhook {field} must be a Unix timestamp")
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError) as error:
            raise ValueError(
                f"Stripe subscription webhook {field} must be a valid Unix timestamp"
            ) from error


class StripeInvoiceProjection:
    __slots__ = ("_subject_for", "_webhook")

    def __init__(self, *, webhook: StripeWebhookPolicy, subject_for: Any) -> None:
        if not callable(subject_for):
            raise TypeError("Stripe invoice subject_for must be callable")
        self._webhook = webhook
        self._subject_for = subject_for

    def project(self, envelope: WebhookEnvelope) -> SubscriptionPayment | None:
        if envelope.type != "invoice.paid":
            return None
        payload = _decode_json(envelope.body)
        if payload is _INVALID_JSON:
            raise ValueError("Stripe invoice webhook body is not valid JSON")
        if not isinstance(payload, dict):
            raise ValueError("Stripe invoice webhook body must be an object")
        if payload.get("id") != envelope.id or payload.get("type") != envelope.type:
            raise ValueError("Stripe invoice webhook envelope differs from its body")
        account = self._webhook.validate(envelope, payload)
        data = payload.get("data")
        invoice = data.get("object") if isinstance(data, dict) else None
        if not isinstance(invoice, dict):
            raise ValueError("Stripe invoice webhook is missing data.object")
        invoice_id = invoice.get("id")
        customer = invoice.get("customer")
        parent = invoice.get("parent")
        details = parent.get("subscription_details") if isinstance(parent, dict) else None
        subscription = details.get("subscription") if isinstance(details, dict) else None
        if not isinstance(invoice_id, str) or not invoice_id:
            raise ValueError("Stripe invoice webhook is missing invoice id")
        if not isinstance(customer, str) or not customer:
            raise ValueError("Stripe invoice webhook is missing customer id")
        if not isinstance(subscription, str) or not subscription:
            raise ValueError("Stripe invoice webhook is missing subscription id")
        if not isinstance(parent, dict) or parent.get("type") != "subscription_details":
            raise ValueError("Stripe invoice webhook parent must be subscription_details")
        if invoice.get("paid") is not True or invoice.get("status") != "paid":
            raise ValueError("Stripe invoice.paid webhook does not contain a paid invoice")
        subject = self._subject_for(customer, account)
        if not isinstance(subject, str) or not subject:
            raise KeyError(f"no billing subject mapping for Stripe customer {customer!r}")
        projected_account = _projected_account(
            details.get("metadata") if isinstance(details, dict) else None,
            account,
            "invoice",
        )
        lines = invoice.get("lines")
        entries = lines.get("data") if isinstance(lines, dict) else None
        if isinstance(lines, dict) and lines.get("has_more") is True:
            raise ValueError(
                "Stripe paid invoice lines are truncated; retrieve all invoice lines "
                "before projection"
            )
        if (
            isinstance(lines, dict)
            and lines.get("has_more") is not None
            and type(lines.get("has_more")) is not bool
        ):
            raise ValueError("Stripe paid invoice lines.has_more must be bool")
        if not isinstance(entries, list) or not entries:
            raise ValueError("Stripe paid invoice has no subscription line periods")
        period_ends: list[datetime] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Stripe paid invoice has an invalid line")
            line_parent = entry.get("parent")
            line_details = (
                line_parent.get("subscription_item_details")
                if isinstance(line_parent, dict)
                else None
            )
            if (
                not isinstance(line_details, dict)
                or line_details.get("subscription") != subscription
            ):
                continue
            period = entry.get("period")
            period_end = period.get("end") if isinstance(period, dict) else None
            resolved_end = StripeSubscriptionProjection._timestamp(
                period_end, "invoice line period.end"
            )
            if resolved_end is not None:
                period_ends.append(resolved_end)
        if not period_ends:
            raise ValueError("Stripe paid invoice has no matching subscription line periods")
        paid_through = max(period_ends)
        return SubscriptionPayment(
            provider="stripe",
            invoice=invoice_id,
            subscription=subscription,
            subject=subject,
            paid_through=paid_through,
            merchant_account=projected_account,
        )
