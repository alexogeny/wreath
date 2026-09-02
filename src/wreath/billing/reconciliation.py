from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..payments import PaymentSnapshot
from ..subscriptions import SubscriptionPayment, SubscriptionSnapshot
from .operations import BillingOperations

ReconciliationResource = PaymentSnapshot | SubscriptionSnapshot | SubscriptionPayment


@dataclass(frozen=True, slots=True)
class ReconciliationPage:
    resources: tuple[ReconciliationResource, ...]
    cursor: str | None
    has_more: bool

    def __post_init__(self) -> None:
        if type(self.resources) is not tuple:
            raise TypeError("Stripe reconciliation resources must be a tuple")
        if self.cursor is not None and (type(self.cursor) is not str or not self.cursor):
            raise ValueError("Stripe reconciliation cursor must be a non-empty string or None")
        if type(self.has_more) is not bool:
            raise TypeError("Stripe reconciliation has_more must be bool")
        if self.has_more and not self.resources:
            raise ValueError("Stripe reconciliation has_more page must contain resources")
        if self.has_more and self.cursor is None:
            raise ValueError("Stripe reconciliation has_more page requires a cursor")
        for resource in self.resources:
            if not isinstance(
                resource,
                PaymentSnapshot | SubscriptionSnapshot | SubscriptionPayment,
            ):
                raise TypeError(
                    "Stripe reconciliation resources must be payment or subscription projections"
                )
            if resource.provider != "stripe":
                raise ValueError("Stripe reconciliation resources must use provider 'stripe'")


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    name: str
    merchant_account: str | None
    cursor: str | None
    pages_completed: int
    resources_applied: int
    failures: int
    running: int


def _capability(owner: Any, field: str, methods: tuple[str, ...]) -> None:
    if not all(callable(getattr(owner, method, None)) for method in methods):
        names = ", ".join(f"{method}()" for method in methods)
        raise TypeError(f"Stripe reconciliation {field} must provide {names}")


def _merchant_account(value: str | None) -> None:
    if value is not None and (type(value) is not str or not value):
        raise ValueError(
            "Stripe reconciliation merchant account must be a non-empty string or None"
        )


class StripeReconciliation:
    __slots__ = (
        "_cursor",
        "_failures",
        "_jobs",
        "_ledger",
        "_limit",
        "_lock",
        "_merchant_account",
        "_merchant_accounts",
        "_operations",
        "_pages_completed",
        "_resources_applied",
        "_retrieve_page",
        "_running",
        "_session_factory",
        "_scheduled_task",
        "_state",
        "_task",
        "name",
    )

    def __init__(
        self,
        name: str,
        *,
        jobs: Any,
        session_factory: Any,
        state: Any,
        ledger: Any,
        retrieve_page: Any,
        merchant_accounts: tuple[str | None, ...] = (None,),
        cron: str | None = "*/15 * * * *",
        limit: int = 100,
        operations: BillingOperations | None = None,
    ) -> None:
        if type(name) is not str or not name:
            raise ValueError("Stripe reconciliation name must not be empty")
        _capability(jobs, "jobs", ("task", "enqueue"))
        if not callable(session_factory):
            raise TypeError("Stripe reconciliation session factory must be callable")
        _capability(state, "state", ("load", "advance"))
        _capability(
            ledger,
            "ledger",
            ("apply_checkout", "apply_subscription", "apply_payment"),
        )
        if not callable(retrieve_page):
            raise TypeError("Stripe reconciliation retrieve page must be callable")
        if type(merchant_accounts) is not tuple or not merchant_accounts:
            raise ValueError("Stripe reconciliation merchant_accounts must be a non-empty tuple")
        for account in merchant_accounts:
            _merchant_account(account)
        if len(set(merchant_accounts)) != len(merchant_accounts):
            raise ValueError("Stripe reconciliation merchant_accounts must be unique")
        if cron is not None and (type(cron) is not str or not cron):
            raise ValueError("Stripe reconciliation cron must be a non-empty string or None")
        if cron is not None:
            _capability(jobs, "jobs", ("schedule",))
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Stripe reconciliation limit must be an integer from 1 through 100")
        if operations is not None and not isinstance(operations, BillingOperations):
            raise TypeError("Stripe reconciliation operations must be BillingOperations or None")

        self.name = name
        self._jobs = jobs
        self._session_factory = session_factory
        self._state = state
        self._ledger = ledger
        self._retrieve_page = retrieve_page
        self._limit = limit
        self._operations = operations
        self._merchant_accounts = merchant_accounts
        self._lock = threading.Lock()
        self._merchant_account: str | None = None
        self._cursor: str | None = None
        self._pages_completed = 0
        self._resources_applied = 0
        self._failures = 0
        self._running = 0
        self._task = f"billing_{name}_reconcile"
        self._scheduled_task = f"{self._task}_schedule"
        jobs.task(self._task, retries=5)(self._run_job)
        if cron is not None:
            jobs.task(self._scheduled_task, retries=5)(self._run_scheduled)
            jobs.schedule(self._scheduled_task, cron=cron, args=())

    async def request(self, merchant_account: str | None = None) -> int | None:
        _merchant_account(merchant_account)
        return await self._jobs.enqueue(
            self._task,
            merchant_account,
            key=self._key(merchant_account, f"request:{int(time.time() // 60)}"),
            coalesce=True,
        )

    async def _run_job(self, _context: Any, merchant_account: str | None) -> None:
        await self.run_once(merchant_account)

    async def _run_scheduled(self, _context: Any) -> None:
        for merchant_account in self._merchant_accounts:
            await self.run_once(merchant_account)

    async def run_once(self, merchant_account: str | None = None) -> ReconciliationSnapshot:
        _merchant_account(merchant_account)
        with self._lock:
            self._running += 1
        completed = False
        try:
            async with self._session_factory() as session:
                cursor = await self._state.load(
                    session,
                    provider="stripe",
                    merchant_account=merchant_account,
                )
            if cursor is not None and (type(cursor) is not str or not cursor):
                raise TypeError(
                    "Stripe reconciliation state load() must return a non-empty cursor or None"
                )
            page = await self._retrieve_page(
                cursor=cursor,
                merchant_account=merchant_account,
                limit=self._limit,
            )
            if not isinstance(page, ReconciliationPage):
                raise TypeError(
                    "Stripe reconciliation retrieve_page must return ReconciliationPage"
                )
            if page.has_more and page.cursor == cursor:
                raise ValueError("Stripe reconciliation cursor did not advance on a has_more page")
            if len(page.resources) > self._limit:
                raise ValueError(
                    "Stripe reconciliation provider returned more resources than the page limit"
                )
            foreign = next(
                (
                    resource.merchant_account
                    for resource in page.resources
                    if resource.merchant_account != merchant_account
                ),
                merchant_account,
            )
            if foreign != merchant_account:
                raise ValueError(
                    f"Stripe reconciliation resource merchant account {foreign!r} differs "
                    f"from requested merchant account {merchant_account!r}"
                )
            async with self._session_factory() as session:
                async with session.begin():
                    for resource in page.resources:
                        await self._apply(session, resource)
                    advanced = await self._state.advance(
                        session,
                        provider="stripe",
                        merchant_account=merchant_account,
                        expected=cursor,
                        cursor=page.cursor,
                    )
                    if advanced is not True:
                        raise RuntimeError("stale Stripe reconciliation cursor")
            with self._lock:
                self._merchant_account = merchant_account
                self._cursor = page.cursor
                self._pages_completed += 1
                self._resources_applied += len(page.resources)
            if self._operations is not None:
                self._operations.reconciliation_completed()
            if page.has_more:
                await self._jobs.enqueue(
                    self._task,
                    merchant_account,
                    key=self._key(merchant_account, page.cursor),
                    coalesce=True,
                )
            completed = True
        finally:
            with self._lock:
                self._running -= 1
                if not completed:
                    self._failures += 1
            if not completed and self._operations is not None:
                self._operations.reconciliation_failed()
        return self.snapshot()

    async def _apply(self, session: Any, resource: ReconciliationResource) -> None:
        if isinstance(resource, PaymentSnapshot):
            await self._ledger.apply_checkout(session, resource)
        elif isinstance(resource, SubscriptionSnapshot):
            await self._ledger.apply_subscription(session, resource)
        else:
            await self._ledger.apply_payment(session, resource)

    def snapshot(self) -> ReconciliationSnapshot:
        with self._lock:
            return ReconciliationSnapshot(
                name=self.name,
                merchant_account=self._merchant_account,
                cursor=self._cursor,
                pages_completed=self._pages_completed,
                resources_applied=self._resources_applied,
                failures=self._failures,
                running=self._running,
            )

    def _key(self, merchant_account: str | None, cursor: str) -> str:
        value = f"{self.name}\0{merchant_account or ''}\0{cursor}".encode()
        return f"billing-reconcile:{hashlib.sha256(value).hexdigest()}"


__all__ = [
    "ReconciliationPage",
    "ReconciliationSnapshot",
    "StripeReconciliation",
]
