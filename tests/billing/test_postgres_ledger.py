from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from wreath.billing.ledger import (
    BillingCommand,
    BillingCommandIdentity,
    BillingCommandState,
    PostgresBillingLedger,
)
from wreath.payments import Money, PaymentSnapshot, PaymentState
from wreath.subscriptions import (
    SubscriptionPayment,
    SubscriptionSnapshot,
    SubscriptionState,
)


class _Result:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def execute(self) -> None:
        return None

    async def fetchrow(self) -> Any:
        return self._session.rows.pop(0)

    async def fetchval(self) -> Any:
        return self._session.values.pop(0)


@dataclass
class _Session:
    rows: list[Any] = field(default_factory=list)
    values: list[Any] = field(default_factory=list)
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def raw(self, sql: str, *parameters: Any) -> _Result:
        self.calls.append((sql, parameters))
        return _Result(self)


def _identity(*, digest: str = "a" * 64) -> BillingCommandIdentity:
    return BillingCommandIdentity(
        provider="stripe",
        operation="checkout",
        idempotency_key="checkout-order-41",
        digest=digest,
        subject="organization:acme",
        merchant_account="acct_acme",
    )


def _command(*, state: BillingCommandState, fence: int = 7) -> BillingCommand:
    return BillingCommand(
        identity=_identity(),
        state=state,
        fencing_token=fence,
    )


def _command_row(
    *,
    state: str = "pending",
    digest: str = "a" * 64,
    fence: int = 0,
    provider_reference: str | None = None,
) -> dict[str, Any]:
    return {
        "provider": "stripe",
        "operation": "checkout",
        "idempotency_key": "checkout-order-41",
        "digest": digest,
        "subject": "organization:acme",
        "merchant_account": "acct_acme",
        "state": state,
        "fencing_token": fence,
        "provider_reference": provider_reference,
        "failure_code": None,
    }


def test_schema_claims_commands_payments_subscriptions_and_invoices_without_provider_payloads() -> (
    None
):
    component = PostgresBillingLedger().component()

    assert component.name == "billing-ledger"
    assert component.schema == "wreath"
    assert component.relations == (
        "billing_commands",
        "billing_payments",
        "billing_subscriptions",
        "billing_invoices",
        "billing_reconciliation",
    )
    sql = component.sql().lower()
    assert "primary key (provider, operation, idempotency_key)" in sql
    assert "primary key (provider, payment_id)" in sql
    assert "primary key (provider, subscription_id)" in sql
    assert "primary key (provider, invoice_id)" in sql
    assert "primary key (provider, merchant_account)" in sql
    assert "(subject,provider,paid_through desc,invoice_id desc)" in sql
    assert "(subject,provider,subscription_id,paid_through desc,invoice_id desc)" in sql
    assert "for update skip locked" not in sql
    for forbidden in ("secret", "api_key", "raw_body", "provider_body", "payload"):
        assert forbidden not in sql


async def test_reconciliation_cursor_uses_compare_and_swap() -> None:
    ledger = PostgresBillingLedger()
    loading = _Session(values=["page_7"])

    assert (
        await ledger.load(
            loading,
            provider="stripe",
            merchant_account="acct_acme",
        )
        == "page_7"
    )
    assert "billing_reconciliation" in loading.calls[0][0]
    assert loading.calls[0][1] == ("stripe", "acct_acme")

    advancing = _Session(values=[True])
    assert await ledger.advance(
        advancing,
        provider="stripe",
        merchant_account="acct_acme",
        expected="page_7",
        cursor="page_8",
    )
    sql, parameters = advancing.calls[0]
    assert "ON CONFLICT (provider,merchant_account) DO UPDATE" in sql
    assert "IS NOT DISTINCT FROM $4" in sql
    assert parameters == ("stripe", "acct_acme", "page_8", "page_7")


async def test_checkout_payment_projection_is_durable_and_monotonic() -> None:
    payment = PaymentSnapshot(
        provider="stripe",
        id="pi_1",
        subject="organization:acme",
        reference="order-41",
        amount=Money("USD", 2900),
        state=PaymentState.SUCCEEDED,
        customer="cus_1",
        merchant_account="acct_acme",
    )
    session = _Session(rows=[{"payment_id": "pi_1"}])

    await PostgresBillingLedger().apply_checkout(session, payment)

    sql, parameters = session.calls[0]
    assert 'INSERT INTO "wreath"."billing_payments"' in sql
    assert "state='succeeded'" in sql
    assert parameters == (
        "stripe",
        "pi_1",
        "organization:acme",
        "order-41",
        "USD",
        2900,
        "succeeded",
        "cus_1",
        "acct_acme",
    )


def test_command_identity_is_immutable_and_digest_is_a_sha256_hex_value() -> None:
    with pytest.raises(ValueError, match="digest.*64 lower-case hexadecimal"):
        _identity(digest="not-a-digest")

    with pytest.raises(ValueError, match="subject must not be empty"):
        BillingCommandIdentity(
            provider="stripe",
            operation="refund",
            idempotency_key="refund-1",
            digest="b" * 64,
            subject="",
        )


async def test_registering_a_new_command_returns_the_pending_durable_row() -> None:
    session = _Session(rows=[_command_row()])

    command = await PostgresBillingLedger().register_command(session, _identity())

    assert command == _command(state=BillingCommandState.PENDING, fence=0)
    sql, parameters = session.calls[0]
    assert 'INSERT INTO "wreath"."billing_commands"' in sql
    assert "ON CONFLICT (provider,operation,idempotency_key) DO NOTHING" in sql
    assert parameters == (
        "stripe",
        "checkout",
        "checkout-order-41",
        "a" * 64,
        "organization:acme",
        "acct_acme",
    )


async def test_duplicate_command_is_idempotent_but_a_changed_digest_is_refused() -> None:
    same = _Session(rows=[None, _command_row(state="confirmed", provider_reference="cs_1")])
    command = await PostgresBillingLedger().register_command(same, _identity())
    assert command.state is BillingCommandState.CONFIRMED
    assert command.provider_reference == "cs_1"

    changed = _Session(rows=[None, _command_row(digest="b" * 64)])
    with pytest.raises(ValueError, match="contradicts its immutable digest"):
        await PostgresBillingLedger().register_command(changed, _identity())


async def test_unknown_is_terminal_and_register_does_not_create_a_new_command() -> None:
    session = _Session(rows=[None, _command_row(state="unknown")])

    command = await PostgresBillingLedger().register_command(session, _identity())

    assert command.state is BillingCommandState.UNKNOWN
    assert len(session.calls) == 2
    assert "UPDATE" not in session.calls[1][0]


async def test_claim_uses_skip_locked_and_advances_the_fence_in_the_same_update() -> None:
    session = _Session(rows=[_command_row(state="leased", fence=8)])

    command = await PostgresBillingLedger().claim_command(
        session,
        lease_owner="billing-worker-1",
        lease_seconds=30,
    )

    assert command == _command(state=BillingCommandState.LEASED, fence=8)
    sql, parameters = session.calls[0]
    assert "FOR UPDATE SKIP LOCKED LIMIT 1" in sql
    assert "UPDATE \"wreath\".\"billing_commands\" AS b" in sql
    assert "fencing_token=b.fencing_token+1" in sql
    assert "WHERE b.command_id=c.command_id" in sql
    assert "state IN ('pending','leased')" in sql
    assert "sending" not in sql.split("RETURNING", 1)[0]
    assert parameters == ("billing-worker-1", 30)


@pytest.mark.parametrize(
    ("method", "source", "target"),
    [
        ("mark_sending", "leased", "sending"),
        ("mark_confirmed", "sending", "confirmed"),
        ("mark_failed", "sending", "failed"),
        ("mark_unknown", "sending", "unknown"),
    ],
)
async def test_every_command_transition_is_fenced(
    method: str,
    source: str,
    target: str,
) -> None:
    session = _Session(values=[1])
    ledger = PostgresBillingLedger()
    command = _command(state=BillingCommandState(source))
    options: dict[str, Any] = {}
    if method == "mark_confirmed":
        options["provider_reference"] = "cs_1"
    elif method in {"mark_failed", "mark_unknown"}:
        options["failure_code"] = "card_declined"

    updated = await getattr(ledger, method)(session, command, **options)

    assert updated.state is BillingCommandState(target)
    sql, parameters = session.calls[0]
    assert "fencing_token=$4" in sql
    assert f"state='{source}'" in sql
    assert parameters[:4] == (
        "stripe",
        "checkout",
        "checkout-order-41",
        7,
    )


async def test_a_stale_command_transition_is_refused() -> None:
    session = _Session(values=[None])

    with pytest.raises(RuntimeError, match="stale billing command fencing token"):
        await PostgresBillingLedger().mark_sending(
            session,
            _command(state=BillingCommandState.LEASED),
        )


async def test_expired_sending_commands_become_terminal_unknown_and_bump_the_fence() -> None:
    session = _Session(values=[2])

    settled = await PostgresBillingLedger().settle_expired_sending(session, limit=25)

    assert settled == 2
    sql, parameters = session.calls[0]
    assert "FOR UPDATE SKIP LOCKED LIMIT $1" in sql
    assert "state='sending'" in sql
    assert "state='unknown'" in sql
    assert "fencing_token=c.fencing_token+1" in sql
    assert "failure_code='lease_expired_after_send'" in sql
    assert parameters == (25,)


def _snapshot(*, subject: str = "organization:acme", account: str = "acct_acme") -> tuple:
    return (
        SubscriptionSnapshot(
            provider="stripe",
            id="sub_1",
            subject=subject,
            plan="pro",
            state=SubscriptionState.ACTIVE,
            provider_state="active",
            paid_through=datetime(2026, 10, 1, tzinfo=UTC),
        ),
        account,
    )


def _payment(
    *,
    invoice: str = "in_1",
    subject: str = "organization:acme",
    paid_through: datetime = datetime(2026, 11, 1, tzinfo=UTC),
) -> SubscriptionPayment:
    return SubscriptionPayment(
        provider="stripe",
        invoice=invoice,
        subscription="sub_1",
        subject=subject,
        paid_through=paid_through,
    )


async def test_payment_before_snapshot_is_preserved_and_merged_monotonically() -> None:
    ledger = PostgresBillingLedger()
    owner = {"subject": "organization:acme", "merchant_account": "acct_acme"}
    payment_session = _Session(rows=[owner, {"inserted": 1}])

    await ledger.apply_payment(payment_session, _payment(), merchant_account="acct_acme")

    assert 'INSERT INTO "wreath"."billing_subscriptions"' in payment_session.calls[0][0]
    assert 'INSERT INTO "wreath"."billing_invoices"' in payment_session.calls[1][0]
    assert "GREATEST" in payment_session.calls[2][0]

    snapshot, account = _snapshot()
    snapshot_session = _Session(rows=[owner])
    await ledger.apply_subscription(snapshot_session, snapshot, merchant_account=account)

    statement = snapshot_session.calls[-1][0]
    assert "SELECT max(paid_through)" in statement
    assert "GREATEST" in statement


async def test_duplicate_invoice_is_idempotent_but_a_contradiction_is_refused() -> None:
    payment = _payment()
    same = _Session(
        rows=[
            {"subject": "organization:acme", "merchant_account": "acct_acme"},
            None,
            {
                "subscription_id": "sub_1",
                "subject": "organization:acme",
                "merchant_account": "acct_acme",
                "paid_through": payment.paid_through,
            },
            None,
        ]
    )
    await PostgresBillingLedger().apply_payment(same, payment, merchant_account="acct_acme")
    assert len(same.calls) == 4

    changed = _Session(
        rows=[
            {"subject": "organization:acme", "merchant_account": "acct_acme"},
            None,
            {
                "subscription_id": "sub_other",
                "subject": "organization:acme",
                "merchant_account": "acct_acme",
                "paid_through": payment.paid_through,
            },
        ]
    )
    with pytest.raises(ValueError, match="invoice 'in_1' contradicts its first value"):
        await PostgresBillingLedger().apply_payment(
            changed,
            payment,
            merchant_account="acct_acme",
        )


@pytest.mark.parametrize(
    "existing",
    [
        (
            "subscription",
            {"subject": "organization:other", "merchant_account": "acct_acme"},
        ),
        (
            "subscription",
            {"subject": "organization:acme", "merchant_account": "acct_other"},
        ),
    ],
)
async def test_subscription_projection_refuses_cross_subject_or_account_ownership(
    existing: dict[str, Any],
) -> None:
    snapshot, account = _snapshot()
    session = _Session(rows=[None, existing])

    with pytest.raises(ValueError, match="subscription 'sub_1'.*ownership"):
        await PostgresBillingLedger().apply_subscription(
            session,
            snapshot,
            merchant_account=account,
        )


async def test_payment_projection_refuses_cross_subject_or_account_ownership() -> None:
    session = _Session(
        rows=[
            None,
            {"subject": "organization:other", "merchant_account": "acct_acme"},
        ]
    )

    with pytest.raises(ValueError, match="subscription 'sub_1'.*ownership"):
        await PostgresBillingLedger().apply_payment(
            session,
            _payment(),
            merchant_account="acct_acme",
        )
