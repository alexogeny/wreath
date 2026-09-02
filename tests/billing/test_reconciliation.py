from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from wreath.billing.operations import BillingOperations
from wreath.billing.reconciliation import (
    ReconciliationPage,
    StripeReconciliation,
)
from wreath.payments import Money, PaymentSnapshot, PaymentState
from wreath.subscriptions import (
    SubscriptionPayment,
    SubscriptionSnapshot,
    SubscriptionState,
)


class Jobs:
    def __init__(self) -> None:
        self.tasks: dict[str, Any] = {}
        self.schedules: list[tuple[str, str, tuple[Any, ...]]] = []
        self.enqueues: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def task(self, name: str, **options: Any) -> Any:
        def register(handler: Any) -> Any:
            self.tasks[name] = handler
            return handler

        return register

    def schedule(self, task: str, *, cron: str, args: tuple[Any, ...]) -> None:
        self.schedules.append((task, cron, args))

    async def enqueue(self, task: str, *args: Any, **options: Any) -> int:
        self.enqueues.append((task, args, options))
        return len(self.enqueues)


class State:
    def __init__(self, cursor: str | None = None) -> None:
        self.cursor = cursor
        self.loads: list[tuple[str, str | None]] = []
        self.advances: list[tuple[str, str | None, str | None, str | None]] = []
        self.accept_advance = True

    async def load(
        self, session: object, *, provider: str, merchant_account: str | None
    ) -> str | None:
        self.loads.append((provider, merchant_account))
        return self.cursor

    async def advance(
        self,
        session: object,
        *,
        provider: str,
        merchant_account: str | None,
        expected: str | None,
        cursor: str | None,
    ) -> bool:
        assert isinstance(session, Session) and session.in_transaction
        self.advances.append((provider, merchant_account, expected, cursor))
        if not self.accept_advance:
            return False
        self.cursor = cursor
        return True


class Ledger:
    def __init__(self) -> None:
        self.values: list[tuple[str, object, object]] = []

    async def apply_checkout(self, session: object, value: PaymentSnapshot) -> None:
        assert isinstance(session, Session) and session.in_transaction
        self.values.append(("checkout", session, value))

    async def apply_subscription(self, session: object, value: SubscriptionSnapshot) -> None:
        assert isinstance(session, Session) and session.in_transaction
        self.values.append(("subscription", session, value))

    async def apply_payment(self, session: object, value: SubscriptionPayment) -> None:
        assert isinstance(session, Session) and session.in_transaction
        self.values.append(("invoice", session, value))


class Sessions:
    def __init__(self) -> None:
        self.opened: list[Session] = []

    @asynccontextmanager
    async def __call__(self) -> Any:
        session = Session()
        self.opened.append(session)
        yield session


class Transaction:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def __aenter__(self) -> None:
        self.session.in_transaction = True
        self.session.transactions += 1

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object,
    ) -> None:
        self.session.in_transaction = False
        self.session.committed = error_type is None
        self.session.rolled_back = error_type is not None


class Session:
    def __init__(self) -> None:
        self.in_transaction = False
        self.transactions = 0
        self.committed = False
        self.rolled_back = False

    def begin(self) -> Transaction:
        return Transaction(self)


def resources() -> tuple[PaymentSnapshot, SubscriptionSnapshot, SubscriptionPayment]:
    return (
        PaymentSnapshot(
            provider="stripe",
            id="pi_1",
            subject="organization:acme",
            reference="order_1",
            amount=Money("AUD", 2_500),
            state=PaymentState.SUCCEEDED,
            customer="cus_acme",
        ),
        SubscriptionSnapshot(
            provider="stripe",
            id="sub_1",
            subject="organization:acme",
            plan="pro",
            state=SubscriptionState.ACTIVE,
            provider_state="active",
        ),
        SubscriptionPayment(
            provider="stripe",
            invoice="in_1",
            subscription="sub_1",
            subject="organization:acme",
            paid_through=datetime(2026, 10, 1, tzinfo=UTC),
        ),
    )


def configured(
    retrieve: Any,
    *,
    state: State | None = None,
    operations: BillingOperations | None = None,
    merchant_accounts: tuple[str | None, ...] = (None,),
) -> tuple[StripeReconciliation, Jobs, State, Ledger, Sessions]:
    jobs = Jobs()
    durable = state or State("cs_previous")
    ledger = Ledger()
    sessions = Sessions()
    reconciliation = StripeReconciliation(
        "commerce",
        jobs=jobs,
        session_factory=sessions,
        state=durable,
        ledger=ledger,
        retrieve_page=retrieve,
        merchant_accounts=merchant_accounts,
        cron="*/10 * * * *",
        operations=operations,
    )
    return reconciliation, jobs, durable, ledger, sessions


@pytest.mark.asyncio
async def test_reconciliation_loads_durable_cursor_and_atomically_applies_page() -> None:
    calls: list[tuple[str | None, str | None, int]] = []

    async def retrieve_page(
        *, cursor: str | None, merchant_account: str | None, limit: int
    ) -> ReconciliationPage:
        calls.append((cursor, merchant_account, limit))
        return ReconciliationPage(resources(), cursor="cs_current", has_more=False)

    reconciliation, jobs, state, ledger, sessions = configured(retrieve_page)

    snapshot = await reconciliation.run_once()

    assert calls == [("cs_previous", None, 100)]
    assert [kind for kind, _, _ in ledger.values] == [
        "checkout",
        "subscription",
        "invoice",
    ]
    assert len({session for _, session, _ in ledger.values}) == 1
    assert state.advances == [("stripe", None, "cs_previous", "cs_current")]
    assert len(sessions.opened) == 2
    assert sessions.opened[1].transactions == 1
    assert sessions.opened[1].committed
    assert snapshot.cursor == "cs_current"
    assert snapshot.resources_applied == 3
    assert snapshot.pages_completed == 1
    assert snapshot.failures == 0
    assert snapshot.running == 0
    assert jobs.schedules == [("billing_commerce_reconcile_schedule", "*/10 * * * *", ())]


@pytest.mark.asyncio
async def test_request_coalesces_by_merchant_scope() -> None:
    async def retrieve_page(**options: Any) -> ReconciliationPage:
        return ReconciliationPage((), cursor=options["cursor"], has_more=False)

    reconciliation, jobs, _, _, _ = configured(
        retrieve_page,
        merchant_accounts=("acct_acme",),
    )

    await reconciliation.request("acct_acme")
    await reconciliation.request("acct_acme")

    first = jobs.enqueues[0]
    assert first[0:2] == ("billing_commerce_reconcile", ("acct_acme",))
    assert first[2]["coalesce"] is True
    assert first[2]["key"] == jobs.enqueues[1][2]["key"]


@pytest.mark.asyncio
async def test_more_pages_enqueue_a_cursor_specific_continuation() -> None:
    async def retrieve_page(**options: Any) -> ReconciliationPage:
        payment = replace(resources()[0], merchant_account="acct_acme")
        return ReconciliationPage((payment,), cursor="cs_next", has_more=True)

    reconciliation, jobs, _, _, _ = configured(retrieve_page)

    await reconciliation.run_once("acct_acme")

    task, args, options = jobs.enqueues[-1]
    assert task == "billing_commerce_reconcile"
    assert args == ("acct_acme",)
    assert options["coalesce"] is True
    assert "cs_next" not in options["key"]


@pytest.mark.asyncio
async def test_one_schedule_drives_every_merchant_without_job_key_collisions() -> None:
    calls: list[str | None] = []

    async def retrieve_page(**options: Any) -> ReconciliationPage:
        calls.append(options["merchant_account"])
        return ReconciliationPage((), cursor=options["cursor"], has_more=False)

    _, jobs, _, _, _ = configured(
        retrieve_page,
        merchant_accounts=("acct_acme", "acct_beta"),
    )

    await jobs.tasks["billing_commerce_reconcile_schedule"](object())

    assert calls == ["acct_acme", "acct_beta"]
    assert jobs.schedules == [("billing_commerce_reconcile_schedule", "*/10 * * * *", ())]


@pytest.mark.asyncio
async def test_stale_cursor_refuses_page_and_counts_failure() -> None:
    state = State("cs_previous")
    state.accept_advance = False
    operations = BillingOperations("commerce")

    async def retrieve_page(**options: Any) -> ReconciliationPage:
        return ReconciliationPage(resources()[:1], cursor="cs_next", has_more=False)

    reconciliation, _, _, ledger, sessions = configured(
        retrieve_page,
        state=state,
        operations=operations,
    )

    with pytest.raises(RuntimeError, match="stale Stripe reconciliation cursor"):
        await reconciliation.run_once()

    assert reconciliation.snapshot().failures == 1
    assert operations.counters().values["reconciliation_failures"] == 1
    assert ledger.values
    assert sessions.opened[1].rolled_back


@pytest.mark.asyncio
async def test_job_handler_runs_the_same_reconciliation_boundary() -> None:
    async def retrieve_page(**options: Any) -> ReconciliationPage:
        return ReconciliationPage((), cursor=options["cursor"], has_more=False)

    reconciliation, jobs, _, _, _ = configured(retrieve_page)

    await jobs.tasks["billing_commerce_reconcile"](object(), None)

    assert reconciliation.snapshot().pages_completed == 1


def test_reconciliation_refuses_missing_capabilities_at_construction() -> None:
    async def retrieve_page(**options: Any) -> ReconciliationPage:
        return ReconciliationPage((), cursor=None, has_more=False)

    complete = {
        "jobs": Jobs(),
        "session_factory": Sessions(),
        "state": State(),
        "ledger": Ledger(),
        "retrieve_page": retrieve_page,
    }
    missing = {
        "jobs": object(),
        "session_factory": object(),
        "state": object(),
        "ledger": object(),
        "retrieve_page": object(),
    }
    for field, value in missing.items():
        options: dict[str, Any] = {**complete, field: value}
        with pytest.raises(TypeError, match=field.replace("_", " ")):
            StripeReconciliation("commerce", **options)


def test_reconciliation_page_refuses_unbounded_or_stuck_pagination() -> None:
    with pytest.raises(ValueError, match="has_more page must contain resources"):
        ReconciliationPage((), cursor="cs_1", has_more=True)
    with pytest.raises(ValueError, match="cursor"):
        ReconciliationPage(resources()[:1], cursor="", has_more=False)

    complete = {
        "jobs": Jobs(),
        "session_factory": Sessions(),
        "state": State(),
        "ledger": Ledger(),
        "retrieve_page": lambda **options: None,
        "cron": None,
    }
    for limit in (0, 101):
        with pytest.raises(ValueError, match="limit.*1 through 100"):
            StripeReconciliation("commerce", **complete, limit=limit)


@pytest.mark.asyncio
async def test_reconciliation_refuses_a_projection_from_another_merchant_scope() -> None:
    payment = resources()[0]
    foreign = PaymentSnapshot(
        provider=payment.provider,
        id=payment.id,
        subject=payment.subject,
        reference=payment.reference,
        amount=payment.amount,
        state=payment.state,
        customer=payment.customer,
        merchant_account="acct_beta",
    )

    async def retrieve_page(**options: Any) -> ReconciliationPage:
        return ReconciliationPage((foreign,), cursor="cs_next", has_more=False)

    reconciliation, _, state, ledger, _ = configured(
        retrieve_page,
        merchant_accounts=("acct_acme",),
    )

    with pytest.raises(ValueError, match="merchant account.*acct_beta.*acct_acme"):
        await reconciliation.run_once("acct_acme")

    assert ledger.values == []
    assert state.advances == []
