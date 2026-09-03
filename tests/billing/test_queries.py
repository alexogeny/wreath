from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from wreath.billing.ledger import BillingCommandState
from wreath.billing.queries import (
    InvoiceCursor,
    PostgresBillingQueries,
    PostgresSubscriptionEntitlements,
)
from wreath.payments import Money, PaymentSnapshot, PaymentState
from wreath.subscriptions import (
    AccessPolicy,
    Plan,
    PlanCatalog,
    SubscriptionAccess,
    SubscriptionPayment,
    SubscriptionSnapshot,
    SubscriptionState,
)


class _Result:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def fetchrow(self) -> Any:
        return self._session.rows.pop(0)

    async def fetch(self) -> list[Any]:
        return self._session.pages.pop(0)


@dataclass
class _Session:
    rows: list[Any] = field(default_factory=list)
    pages: list[list[Any]] = field(default_factory=list)
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    entered: int = 0
    exited: int = 0

    async def __aenter__(self) -> _Session:
        self.entered += 1
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exited += 1

    def raw(self, sql: str, *parameters: Any) -> _Result:
        self.calls.append((sql, parameters))
        return _Result(self)


def _subscription_row(*, subject: str = "organization:acme") -> dict[str, Any]:
    return {
        "provider": "stripe",
        "subscription_id": "sub_1",
        "subject": subject,
        "plan": "pro",
        "state": "active",
        "provider_state": "active",
        "paid_through": datetime(2026, 10, 1, tzinfo=UTC),
        "trial_ends_at": None,
        "merchant_account": "acct_acme",
    }


def _invoice_row(invoice: str, paid_through: datetime) -> dict[str, Any]:
    return {
        "provider": "stripe",
        "invoice_id": invoice,
        "subscription_id": "sub_1",
        "subject": "organization:acme",
        "paid_through": paid_through,
        "merchant_account": "acct_acme",
    }


async def test_subject_scoped_reads_return_normalized_models() -> None:
    paid = datetime(2026, 10, 1, tzinfo=UTC)
    session = _Session(
        rows=[
            _subscription_row(),
            {
                "provider": "stripe",
                "payment_id": "pi_1",
                "subject": "organization:acme",
                "reference": "order-1",
                "currency": "USD",
                "amount_minor": 2900,
                "state": "succeeded",
                "customer": "cus_1",
                "merchant_account": "acct_acme",
            },
            _invoice_row("in_1", paid),
            {
                "provider": "stripe",
                "operation": "refund",
                "idempotency_key": "refund-1",
                "digest": "a" * 64,
                "subject": "organization:acme",
                "merchant_account": "acct_acme",
                "state": "confirmed",
                "fencing_token": 2,
                "provider_reference": "re_1",
                "failure_code": None,
            },
        ]
    )
    queries = PostgresBillingQueries(lambda: session, provider="stripe")

    subscription = await queries.subscription("organization:acme")
    payment = await queries.payment("organization:acme", "pi_1")
    invoice = await queries.invoice("organization:acme", "in_1")
    command = await queries.command("organization:acme", "refund", "refund-1")

    assert subscription == SubscriptionSnapshot(
        "stripe",
        "sub_1",
        "organization:acme",
        "pro",
        SubscriptionState.ACTIVE,
        "active",
        paid,
        None,
        "acct_acme",
    )
    assert payment == PaymentSnapshot(
        "stripe",
        "pi_1",
        "organization:acme",
        "order-1",
        Money("USD", 2900),
        PaymentState.SUCCEEDED,
        "cus_1",
        "acct_acme",
    )
    assert invoice == SubscriptionPayment(
        "stripe", "in_1", "sub_1", "organization:acme", paid, "acct_acme"
    )
    assert command is not None
    assert command.state is BillingCommandState.CONFIRMED
    assert command.identity.subject == "organization:acme"
    assert all("subject=$1" in sql for sql, _ in session.calls)
    assert all(parameters[0] == "organization:acme" for _, parameters in session.calls)
    assert session.entered == session.exited == 4


async def test_missing_or_other_subject_rows_are_not_returned() -> None:
    session = _Session(rows=[None, None, None, None])
    queries = PostgresBillingQueries(lambda: session, provider="stripe")

    assert await queries.subscription("organization:globex") is None
    assert await queries.payment("organization:globex", "pi_1") is None
    assert await queries.invoice("organization:globex", "in_1") is None
    assert await queries.command("organization:globex", "refund", "refund-1") is None


async def test_subscription_and_invoice_reads_use_the_subject_merchant_account() -> None:
    session = _Session(rows=[None, None])
    queries = PostgresBillingQueries(
        lambda: session,
        provider="stripe",
        merchant_account_for=lambda subject: {
            "organization:acme": "acct_acme",
            "organization:globex": "acct_globex",
        }[subject],
    )

    await queries.subscription("organization:acme")
    await queries.invoice("organization:globex", "in_1")

    subscription_sql, subscription_parameters = session.calls[0]
    invoice_sql, invoice_parameters = session.calls[1]
    assert "merchant_account IS NOT DISTINCT FROM $3" in subscription_sql
    assert subscription_parameters == ("organization:acme", "stripe", "acct_acme")
    assert "merchant_account IS NOT DISTINCT FROM $3" in invoice_sql
    assert invoice_parameters == (
        "organization:globex",
        "stripe",
        "acct_globex",
        "in_1",
    )


async def test_invoice_listing_is_keyset_paginated_and_bounded() -> None:
    newest = datetime(2026, 10, 3, tzinfo=UTC)
    middle = datetime(2026, 10, 2, tzinfo=UTC)
    oldest = datetime(2026, 10, 1, tzinfo=UTC)
    session = _Session(
        pages=[
            [
                _invoice_row("in_3", newest),
                _invoice_row("in_2", middle),
                _invoice_row("in_1", oldest),
            ],
            [_invoice_row("in_1", oldest)],
            [],
        ]
    )
    queries = PostgresBillingQueries(lambda: session, provider="stripe")

    first = await queries.invoices("organization:acme", limit=2)
    assert [invoice.invoice for invoice in first.items] == ["in_3", "in_2"]
    assert first.next_cursor == InvoiceCursor(middle, "in_2")
    sql, parameters = session.calls[0]
    assert "subject=$1" in sql
    assert "subscription_id" not in sql.split("WHERE", 1)[1]
    assert "(paid_through,invoice_id) < ($4::timestamptz,$5::text)" in sql
    assert "ORDER BY paid_through DESC,invoice_id DESC LIMIT $6" in sql
    assert parameters == ("organization:acme", "stripe", None, None, None, 3)

    second = await queries.invoices(
        "organization:acme",
        cursor=first.next_cursor,
        limit=2,
    )
    assert [invoice.invoice for invoice in second.items] == ["in_1"]
    assert second.next_cursor is None
    assert session.calls[1][1] == (
        "organization:acme",
        "stripe",
        None,
        middle,
        "in_2",
        3,
    )

    await queries.invoices("organization:acme", subscription="sub_1", limit=2)
    scoped_sql, scoped_parameters = session.calls[2]
    assert "subscription_id=$4" in scoped_sql
    assert "($5::timestamptz IS NULL" in scoped_sql
    assert scoped_parameters == (
        "organization:acme",
        "stripe",
        None,
        "sub_1",
        None,
        None,
        3,
    )


@pytest.mark.parametrize("limit", [True, 0, -1, 101, 1.5])
async def test_invoice_limit_refuses_unbounded_or_invalid_values(limit: Any) -> None:
    queries = PostgresBillingQueries(lambda: _Session(), provider="stripe")

    with pytest.raises((TypeError, ValueError), match="invoice limit.*1 through 100"):
        await queries.invoices("organization:acme", subscription="sub_1", limit=limit)


async def test_read_scope_and_cursor_are_validated_before_database_io() -> None:
    session = _Session()
    queries = PostgresBillingQueries(lambda: session, provider="stripe")

    with pytest.raises(ValueError, match="billing subject must not be empty"):
        await queries.subscription("")
    with pytest.raises(ValueError, match="invoice cursor.*timezone"):
        InvoiceCursor(datetime(2026, 1, 1), "in_1")
    with pytest.raises(ValueError, match="invoice cursor invoice must not be empty"):
        InvoiceCursor(datetime(2026, 1, 1, tzinfo=UTC), "")
    assert session.calls == []


@dataclass(frozen=True)
class _Identity:
    id: str
    tenant: str


async def test_entitlement_adapter_reads_one_subject_snapshot_per_resolution() -> None:
    session = _Session(rows=[_subscription_row()])
    adapter = PostgresSubscriptionEntitlements(
        PostgresBillingQueries(lambda: session, provider="stripe"),
        PlanCatalog(Plan("pro", "price_pro", frozenset({"api", "export"}))),
        subject_for=lambda identity: f"organization:{identity.tenant}",
        access=AccessPolicy(frozenset({SubscriptionState.ACTIVE})),
        now=lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )

    access = await adapter.resolve(_Identity("agent", "acme"))

    assert access == SubscriptionAccess("pro", frozenset({"api", "export"}))
    assert adapter.names() == frozenset({"api", "export"})
    assert len(session.calls) == 1
    assert session.calls[0][1] == ("organization:acme", "stripe", None)


async def test_entitlement_adapter_does_not_trust_identity_id_as_ledger_scope() -> None:
    session = _Session(rows=[None])
    adapter = PostgresSubscriptionEntitlements(
        PostgresBillingQueries(lambda: session, provider="stripe"),
        PlanCatalog(Plan("pro", "price_pro", frozenset({"api"}))),
        subject_for=lambda identity: f"organization:{identity.tenant}",
    )

    assert await adapter.resolve(_Identity("organization:globex", "acme")) == SubscriptionAccess(
        None, frozenset()
    )
    assert session.calls[0][1] == ("organization:acme", "stripe", None)
