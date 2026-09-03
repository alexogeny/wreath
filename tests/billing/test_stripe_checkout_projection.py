from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from wreath.billing.providers.stripe import StripeWebhookPolicy
from wreath.billing.providers.stripe_checkout import StripeCheckoutProjection
from wreath.payments import Money, PaymentSnapshot, PaymentState
from wreath.webhooks import WebhookEnvelope


def envelope(
    event_type: str,
    session: dict[str, Any],
    *,
    account: str | None = None,
    version: str = "2026-08-26.dahlia",
    livemode: bool = False,
) -> WebhookEnvelope:
    payload: dict[str, Any] = {
        "id": "evt_checkout_1",
        "type": event_type,
        "api_version": version,
        "livemode": livemode,
        "data": {"object": session},
    }
    if account is not None:
        payload["account"] = account
    return WebhookEnvelope(
        "evt_checkout_1",
        event_type,
        version,
        datetime(2026, 9, 2, tzinfo=UTC),
        "application/json",
        json.dumps(payload).encode(),
    )


def current_session(**changes: Any) -> dict[str, Any]:
    session: dict[str, Any] = {
        "id": "cs_1",
        "mode": "payment",
        "payment_status": "paid",
        "payment_intent": "pi_1",
        "amount_total": 2_500,
        "currency": "aud",
        "client_reference_id": "order_opaque_1",
        "customer": "cus_acme",
    }
    session.update(changes)
    return session


def projector(
    retrieved: dict[str, Any],
    *,
    scope: Literal["account", "connected_accounts"] = "account",
    calls: list[tuple[str, str | None]] | None = None,
    subjects: list[tuple[str, str | None, str | None]] | None = None,
) -> StripeCheckoutProjection:
    async def retrieve_checkout(session_id: str, account: str | None) -> dict[str, Any]:
        if calls is not None:
            calls.append((session_id, account))
        return retrieved

    def subject_for(reference: str, customer: str | None, account: str | None) -> str | None:
        if subjects is not None:
            subjects.append((reference, customer, account))
        return "organization:acme"

    return StripeCheckoutProjection(
        webhook=StripeWebhookPolicy(
            "2026-08-26.dahlia",
            False,
            scope,
        ),
        retrieve_checkout=retrieve_checkout,
        subject_for=subject_for,
    )


def test_checkout_projection_requires_an_async_retriever() -> None:
    with pytest.raises(TypeError, match="retrieve_checkout must be an async callable"):
        StripeCheckoutProjection(
            webhook=StripeWebhookPolicy("2026-08-26.dahlia", False, "account"),
            retrieve_checkout=lambda session_id, account: {},
            subject_for=lambda reference, customer, account: "organization:acme",
        )


@pytest.mark.asyncio
async def test_paid_checkout_projects_authoritative_payment() -> None:
    calls: list[tuple[str, str | None]] = []
    subjects: list[tuple[str, str | None, str | None]] = []
    projection = projector(current_session(), calls=calls, subjects=subjects)

    payment = await projection.project(
        envelope(
            "checkout.session.completed",
            {"id": "cs_1", "payment_status": "unpaid", "amount_total": 1},
        )
    )

    assert payment == PaymentSnapshot(
        provider="stripe",
        id="pi_1",
        subject="organization:acme",
        reference="order_opaque_1",
        amount=Money("AUD", 2_500),
        state=PaymentState.SUCCEEDED,
        customer="cus_acme",
        merchant_account=None,
    )
    assert calls == [("cs_1", None)]
    assert subjects == [("order_opaque_1", "cus_acme", None)]


@pytest.mark.asyncio
async def test_delayed_checkout_stays_pending_until_authoritative_payment_is_paid() -> None:
    projection = projector(current_session(payment_status="unpaid"))

    payment = await projection.project(
        envelope("checkout.session.completed", {"id": "cs_1", "payment_status": "paid"})
    )

    assert payment is not None
    assert payment.state is PaymentState.PENDING


@pytest.mark.asyncio
async def test_async_success_requires_authoritative_paid_payment_intent() -> None:
    missing_intent = projector(current_session(payment_intent=None))
    unpaid = projector(current_session(payment_status="unpaid"))
    event = envelope("checkout.session.async_payment_succeeded", {"id": "cs_1"})

    with pytest.raises(ValueError, match="paid Checkout Session requires payment_intent"):
        await missing_intent.project(event)
    pending = await unpaid.project(event)

    assert pending is not None
    assert pending.state is PaymentState.PENDING


@pytest.mark.asyncio
async def test_async_failure_never_grants_an_unpaid_payment() -> None:
    payment = await projector(current_session(payment_status="unpaid")).project(
        envelope("checkout.session.async_payment_failed", {"id": "cs_1"})
    )

    assert payment is not None
    assert payment.state is PaymentState.FAILED


@pytest.mark.asyncio
async def test_delayed_async_failure_cannot_downgrade_current_paid_truth() -> None:
    payment = await projector(current_session()).project(
        envelope("checkout.session.async_payment_failed", {"id": "cs_1"})
    )

    assert payment is not None
    assert payment.state is PaymentState.SUCCEEDED


@pytest.mark.asyncio
async def test_subscription_checkout_is_not_a_one_time_payment() -> None:
    payment = await projector(current_session(mode="subscription")).project(
        envelope("checkout.session.completed", {"id": "cs_1"})
    )

    assert payment is None


@pytest.mark.asyncio
async def test_unknown_subject_mapping_is_refused() -> None:
    async def retrieve_checkout(session_id: str, account: str | None) -> dict[str, Any]:
        return current_session()

    projection = StripeCheckoutProjection(
        webhook=StripeWebhookPolicy("2026-08-26.dahlia", False, "account"),
        retrieve_checkout=retrieve_checkout,
        subject_for=lambda reference, customer, account: None,
    )

    with pytest.raises(KeyError, match="no billing subject mapping for Stripe Checkout"):
        await projection.project(envelope("checkout.session.completed", {"id": "cs_1"}))


@pytest.mark.asyncio
async def test_connected_checkout_requires_and_propagates_account() -> None:
    calls: list[tuple[str, str | None]] = []
    subjects: list[tuple[str, str | None, str | None]] = []
    projection = projector(
        current_session(),
        scope="connected_accounts",
        calls=calls,
        subjects=subjects,
    )

    with pytest.raises(ValueError, match="connected-account webhook requires account"):
        await projection.project(envelope("checkout.session.completed", {"id": "cs_1"}))
    payment = await projection.project(
        envelope(
            "checkout.session.completed",
            {"id": "cs_1"},
            account="acct_acme",
        )
    )

    assert payment is not None
    assert payment.merchant_account == "acct_acme"
    assert calls == [("cs_1", "acct_acme")]
    assert subjects == [("order_opaque_1", "cus_acme", "acct_acme")]


@pytest.mark.asyncio
async def test_destination_checkout_recovers_its_original_account_from_metadata() -> None:
    payment = await projector(
        current_session(metadata={"wreath_merchant_account": "acct_destination"})
    ).project(envelope("checkout.session.completed", {"id": "cs_1"}))

    assert payment is not None
    assert payment.merchant_account == "acct_destination"


@pytest.mark.asyncio
async def test_environment_is_validated_before_authoritative_retrieval() -> None:
    calls: list[tuple[str, str | None]] = []
    projection = projector(current_session(), calls=calls)

    with pytest.raises(ValueError, match="event version"):
        await projection.project(
            envelope(
                "checkout.session.completed",
                {"id": "cs_1"},
                version="2025-12-15.clover",
            )
        )

    assert calls == []


@pytest.mark.asyncio
async def test_retrieved_checkout_fields_are_strictly_validated() -> None:
    invalid = (
        ({"id": "cs_other"}, "retrieved id differs"),
        ({"amount_total": True}, "amount_total"),
        ({"currency": "a$"}, "currency"),
        ({"client_reference_id": None}, "client_reference_id"),
        ({"customer": 1}, "customer"),
    )
    event = envelope("checkout.session.completed", {"id": "cs_1"})

    for changes, message in invalid:
        with pytest.raises(ValueError, match=message):
            await projector(current_session(**changes)).project(event)


@pytest.mark.parametrize("currency", [None, "us", "usd1", "üsd", "u$d", "USD"])
@pytest.mark.asyncio
async def test_checkout_currency_requires_three_lowercase_ascii_letters(
    currency: object,
) -> None:
    event = envelope("checkout.session.completed", {"id": "cs_1"})

    with pytest.raises(ValueError) as caught:
        await projector(current_session(currency=currency)).project(event)
    assert str(caught.value) == "Stripe Checkout currency must be three lowercase ASCII letters"


@pytest.mark.parametrize("customer", ["", 1])
@pytest.mark.asyncio
async def test_checkout_customer_requires_non_empty_text_or_null(customer: object) -> None:
    event = envelope("checkout.session.completed", {"id": "cs_1"})

    with pytest.raises(ValueError) as caught:
        await projector(current_session(customer=customer)).project(event)
    assert str(caught.value) == "Stripe Checkout customer must be a non-empty string or null"


@pytest.mark.asyncio
async def test_checkout_customer_may_be_null() -> None:
    event = envelope("checkout.session.completed", {"id": "cs_1"})

    payment = await projector(current_session(customer=None)).project(event)

    assert payment is not None
    assert payment.customer is None


@pytest.mark.asyncio
async def test_zero_total_checkout_is_a_succeeded_checkout_fact() -> None:
    payment = await projector(
        current_session(
            payment_status="no_payment_required",
            payment_intent=None,
            amount_total=0,
        )
    ).project(envelope("checkout.session.completed", {"id": "cs_1"}))

    assert payment is not None
    assert payment.id == "cs_1"
    assert payment.amount == Money("AUD", 0)
    assert payment.state is PaymentState.SUCCEEDED


@pytest.mark.asyncio
async def test_truncated_retrieved_line_items_are_refused() -> None:
    projection = projector(current_session(line_items={"data": [], "has_more": True}))

    with pytest.raises(ValueError, match="truncated.*retrieve all Checkout line items"):
        await projection.project(envelope("checkout.session.completed", {"id": "cs_1"}))


@pytest.mark.asyncio
async def test_invalid_body_is_not_retained_by_projection_error() -> None:
    secret = b"raw-checkout-cardholder-secret"
    invalid = WebhookEnvelope(
        "evt_checkout_1",
        "checkout.session.completed",
        "2026-08-26.dahlia",
        datetime(2026, 9, 2, tzinfo=UTC),
        "application/json",
        secret,
    )
    projection = projector(current_session())

    with pytest.raises(ValueError, match="not valid JSON") as caught:
        await projection.project(invalid)

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret.decode() not in rendered


@pytest.mark.asyncio
async def test_unrelated_webhook_is_ignored_without_retrieval() -> None:
    calls: list[tuple[str, str | None]] = []
    projection = projector(current_session(), calls=calls)

    payment = await projection.project(envelope("invoice.paid", {"id": "in_1"}))

    assert payment is None
    assert calls == []
