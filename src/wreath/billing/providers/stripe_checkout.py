from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from ...payments import Money, PaymentSnapshot, PaymentState
from ...webhooks import WebhookEnvelope
from .stripe import StripeWebhookPolicy, _async_callable, _projected_account

_CHECKOUT_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
    }
)
_INVALID_JSON = object()


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError:
        return _INVALID_JSON


class StripeCheckoutProjection:
    __slots__ = ("_retrieve_checkout", "_subject_for", "_webhook")

    def __init__(
        self,
        *,
        webhook: StripeWebhookPolicy,
        retrieve_checkout: Callable[[str, str | None], Awaitable[dict[str, Any]]],
        subject_for: Callable[[str, str | None, str | None], str | None],
    ) -> None:
        if not isinstance(webhook, StripeWebhookPolicy):
            raise TypeError("Stripe Checkout webhook must be StripeWebhookPolicy")
        if not _async_callable(retrieve_checkout):
            raise TypeError("Stripe Checkout retrieve_checkout must be an async callable")
        if not callable(subject_for):
            raise TypeError("Stripe Checkout subject_for must be callable")
        self._webhook = webhook
        self._retrieve_checkout = retrieve_checkout
        self._subject_for = subject_for

    async def project(self, envelope: WebhookEnvelope) -> PaymentSnapshot | None:
        if envelope.type not in _CHECKOUT_EVENTS:
            return None
        payload = self._payload(envelope)
        account = self._webhook.validate(envelope, payload)
        data = payload.get("data")
        event_session = data.get("object") if isinstance(data, dict) else None
        if not isinstance(event_session, dict):
            raise ValueError("Stripe Checkout webhook is missing data.object")
        session_id = event_session.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Stripe Checkout webhook is missing session id")

        session = await self._retrieve_checkout(session_id, account)
        if not isinstance(session, dict):
            raise TypeError("Stripe retrieve_checkout must return a Checkout Session object")
        if session.get("id") != session_id:
            raise ValueError("Stripe Checkout retrieved id differs from webhook session id")
        if session.get("mode") == "subscription":
            return None
        payment_id, payment_status, amount, reference, customer = self._validated_session(
            session_id, session
        )
        subject = self._subject_for(reference, customer, account)
        if not isinstance(subject, str) or not subject:
            raise KeyError("no billing subject mapping for Stripe Checkout reference")
        projected_account = _projected_account(session.get("metadata"), account, "Checkout Session")

        if payment_status in {"paid", "no_payment_required"}:
            state = PaymentState.SUCCEEDED
        elif envelope.type == "checkout.session.async_payment_failed":
            state = PaymentState.FAILED
        else:
            state = PaymentState.PENDING
        return PaymentSnapshot(
            provider="stripe",
            id=payment_id,
            subject=subject,
            reference=reference,
            amount=amount,
            state=state,
            customer=customer,
            merchant_account=projected_account,
        )

    @staticmethod
    def _payload(envelope: WebhookEnvelope) -> dict[str, Any]:
        payload = _decode_json(envelope.body)
        if payload is _INVALID_JSON:
            raise ValueError("Stripe Checkout webhook body is not valid JSON")
        if not isinstance(payload, dict):
            raise ValueError("Stripe Checkout webhook body must be an object")
        if payload.get("id") != envelope.id or payload.get("type") != envelope.type:
            raise ValueError("Stripe Checkout webhook envelope differs from its body")
        return payload

    @staticmethod
    def _validated_session(
        session_id: str, session: dict[str, Any]
    ) -> tuple[str, str, Money, str, str | None]:
        if session.get("id") != session_id:
            raise ValueError("Stripe Checkout retrieved id differs from webhook session id")
        if session.get("mode") != "payment":
            raise ValueError("Stripe Checkout mode must be 'payment'")
        payment_status = session.get("payment_status")
        if payment_status not in {"paid", "unpaid", "no_payment_required"}:
            raise ValueError("Stripe Checkout has unsupported payment_status")
        amount_total = session.get("amount_total")
        if type(amount_total) is not int or amount_total < 0:
            raise ValueError(
                "Stripe Checkout amount_total must be non-negative integer minor units"
            )
        payment_id = session.get("payment_intent")
        if payment_status == "no_payment_required":
            if amount_total != 0:
                raise ValueError("no_payment_required Checkout Session must have zero total")
            if payment_id is not None:
                raise ValueError(
                    "no_payment_required Checkout Session must not have payment_intent"
                )
            payment_id = session_id
        elif not isinstance(payment_id, str) or not payment_id:
            if payment_status == "paid":
                raise ValueError("paid Checkout Session requires payment_intent")
            raise ValueError("Stripe Checkout Session is missing payment_intent")
        currency = session.get("currency")
        if (
            not isinstance(currency, str)
            or len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
            or not currency.islower()
        ):
            raise ValueError("Stripe Checkout currency must be three lowercase ASCII letters")
        reference = session.get("client_reference_id")
        if not isinstance(reference, str) or not reference:
            raise ValueError("Stripe Checkout client_reference_id must be a non-empty string")
        customer = session.get("customer")
        if customer is not None and (not isinstance(customer, str) or not customer):
            raise ValueError("Stripe Checkout customer must be a non-empty string or null")
        line_items = session.get("line_items")
        if line_items is not None:
            if not isinstance(line_items, dict):
                raise ValueError("Stripe Checkout line_items must be an object")
            has_more = line_items.get("has_more")
            if has_more is True:
                raise ValueError(
                    "Stripe Checkout line items are truncated; retrieve all Checkout "
                    "line items before inspection"
                )
            if has_more is not None and type(has_more) is not bool:
                raise ValueError("Stripe Checkout line_items.has_more must be bool")
        return (
            payment_id,
            payment_status,
            Money(currency.upper(), amount_total),
            reference,
            customer,
        )


__all__ = ["StripeCheckoutProjection"]
