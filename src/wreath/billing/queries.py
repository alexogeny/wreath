from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .._pgname import quote_identifier
from ..payments import Money, PaymentSnapshot, PaymentState
from ..subscriptions import (
    AccessPolicy,
    PlanCatalog,
    SubscriptionAccess,
    SubscriptionEntitlements,
    SubscriptionPayment,
    SubscriptionSnapshot,
    SubscriptionState,
)
from .ledger import (
    BillingCommand,
    BillingCommandIdentity,
    BillingCommandState,
)


def _required_text(value: str, field: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"billing {field} must not be empty")


def _row_value(row: Any, key: str, index: int) -> Any:
    try:
        return row[key]
    except KeyError, TypeError:
        return row[index]


@dataclass(frozen=True, slots=True)
class InvoiceCursor:
    paid_through: datetime
    invoice: str

    def __post_init__(self) -> None:
        if not isinstance(self.paid_through, datetime) or self.paid_through.tzinfo is None:
            raise ValueError("billing invoice cursor paid_through must include a timezone")
        _required_text(self.invoice, "invoice cursor invoice")


@dataclass(frozen=True, slots=True)
class InvoicePage:
    items: tuple[SubscriptionPayment, ...]
    next_cursor: InvoiceCursor | None


_SUBSCRIPTION_COLUMNS = (
    "provider,subscription_id,subject,plan,state,provider_state,paid_through,"
    "trial_ends_at,merchant_account"
)
_PAYMENT_COLUMNS = (
    "provider,payment_id,subject,reference,currency,amount_minor,state,customer,merchant_account"
)
_INVOICE_COLUMNS = "provider,invoice_id,subscription_id,subject,paid_through,merchant_account"
_COMMAND_COLUMNS = (
    "provider,operation,idempotency_key,digest,subject,merchant_account,state,"
    "fencing_token,provider_reference,failure_code"
)
_DEFAULT_ACCESS = AccessPolicy()


def _subscription(row: Any) -> SubscriptionSnapshot:
    return SubscriptionSnapshot(
        provider=str(_row_value(row, "provider", 0)),
        id=str(_row_value(row, "subscription_id", 1)),
        subject=str(_row_value(row, "subject", 2)),
        plan=str(_row_value(row, "plan", 3)),
        state=SubscriptionState(_row_value(row, "state", 4)),
        provider_state=str(_row_value(row, "provider_state", 5)),
        paid_through=_row_value(row, "paid_through", 6),
        trial_ends_at=_row_value(row, "trial_ends_at", 7),
        merchant_account=_row_value(row, "merchant_account", 8),
    )


def _payment(row: Any) -> PaymentSnapshot:
    return PaymentSnapshot(
        provider=str(_row_value(row, "provider", 0)),
        id=str(_row_value(row, "payment_id", 1)),
        subject=str(_row_value(row, "subject", 2)),
        reference=str(_row_value(row, "reference", 3)),
        amount=Money(
            str(_row_value(row, "currency", 4)),
            int(_row_value(row, "amount_minor", 5)),
        ),
        state=PaymentState(_row_value(row, "state", 6)),
        customer=_row_value(row, "customer", 7),
        merchant_account=_row_value(row, "merchant_account", 8),
    )


def _invoice(row: Any) -> SubscriptionPayment:
    return SubscriptionPayment(
        provider=str(_row_value(row, "provider", 0)),
        invoice=str(_row_value(row, "invoice_id", 1)),
        subscription=str(_row_value(row, "subscription_id", 2)),
        subject=str(_row_value(row, "subject", 3)),
        paid_through=_row_value(row, "paid_through", 4),
        merchant_account=_row_value(row, "merchant_account", 5),
    )


def _command(row: Any) -> BillingCommand:
    return BillingCommand(
        identity=BillingCommandIdentity(
            provider=str(_row_value(row, "provider", 0)),
            operation=str(_row_value(row, "operation", 1)),
            idempotency_key=str(_row_value(row, "idempotency_key", 2)),
            digest=str(_row_value(row, "digest", 3)),
            subject=str(_row_value(row, "subject", 4)),
            merchant_account=_row_value(row, "merchant_account", 5),
        ),
        state=BillingCommandState(_row_value(row, "state", 6)),
        fencing_token=int(_row_value(row, "fencing_token", 7)),
        provider_reference=_row_value(row, "provider_reference", 8),
        failure_code=_row_value(row, "failure_code", 9),
    )


class PostgresBillingQueries:
    __slots__ = (
        "_commands",
        "_invoices",
        "_merchant_account_for",
        "_payments",
        "_provider",
        "_session_factory",
        "_subscriptions",
    )

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        provider: str,
        merchant_account_for: Callable[[str], str | None] | None = None,
        schema: str = "wreath",
    ) -> None:
        if not callable(session_factory):
            raise TypeError("billing query session_factory must be callable")
        _required_text(provider, "query provider")
        if merchant_account_for is not None and not callable(merchant_account_for):
            raise TypeError("billing query merchant_account_for must be callable or None")
        quoted = quote_identifier(schema, reject_quote=True)
        self._session_factory = session_factory
        self._provider = provider
        self._merchant_account_for = merchant_account_for
        self._commands = f'{quoted}."billing_commands"'
        self._payments = f'{quoted}."billing_payments"'
        self._subscriptions = f'{quoted}."billing_subscriptions"'
        self._invoices = f'{quoted}."billing_invoices"'

    def _merchant_account(self, subject: str) -> str | None:
        resolver = self._merchant_account_for
        account = None if resolver is None else resolver(subject)
        if account is not None:
            _required_text(account, "query merchant account")
        return account

    async def subscription(self, subject: str) -> SubscriptionSnapshot | None:
        _required_text(subject, "subject")
        merchant_account = self._merchant_account(subject)
        async with self._session_factory() as session:
            row = await session.raw(
                f"SELECT {_SUBSCRIPTION_COLUMNS} FROM {self._subscriptions} "
                "WHERE subject=$1 AND provider=$2 "
                "AND merchant_account IS NOT DISTINCT FROM $3 "
                "AND plan IS NOT NULL AND state IS NOT NULL "
                "AND provider_state IS NOT NULL ORDER BY updated_at DESC,subscription_id DESC "
                "LIMIT 1",
                subject,
                self._provider,
                merchant_account,
            ).fetchrow()
        return None if row is None else _subscription(row)

    async def payment(
        self,
        subject: str,
        payment: str,
    ) -> PaymentSnapshot | None:
        _required_text(subject, "subject")
        _required_text(payment, "payment id")
        merchant_account = self._merchant_account(subject)
        async with self._session_factory() as session:
            row = await session.raw(
                f"SELECT {_PAYMENT_COLUMNS} FROM {self._payments} "
                "WHERE subject=$1 AND provider=$2 "
                "AND merchant_account IS NOT DISTINCT FROM $3 AND payment_id=$4",
                subject,
                self._provider,
                merchant_account,
                payment,
            ).fetchrow()
        return None if row is None else _payment(row)

    async def invoice(
        self,
        subject: str,
        invoice: str,
    ) -> SubscriptionPayment | None:
        _required_text(subject, "subject")
        _required_text(invoice, "invoice id")
        merchant_account = self._merchant_account(subject)
        async with self._session_factory() as session:
            row = await session.raw(
                f"SELECT {_INVOICE_COLUMNS} FROM {self._invoices} "
                "WHERE subject=$1 AND provider=$2 "
                "AND merchant_account IS NOT DISTINCT FROM $3 AND invoice_id=$4",
                subject,
                self._provider,
                merchant_account,
                invoice,
            ).fetchrow()
        return None if row is None else _invoice(row)

    async def command(
        self,
        subject: str,
        operation: str,
        idempotency_key: str,
    ) -> BillingCommand | None:
        _required_text(subject, "subject")
        _required_text(idempotency_key, "command idempotency_key")
        _required_text(operation, "command operation")
        merchant_account = self._merchant_account(subject)
        async with self._session_factory() as session:
            row = await session.raw(
                f"SELECT {_COMMAND_COLUMNS} FROM {self._commands} "
                "WHERE subject=$1 AND provider=$2 "
                "AND merchant_account IS NOT DISTINCT FROM $3 "
                "AND operation=$4 AND idempotency_key=$5",
                subject,
                self._provider,
                merchant_account,
                operation,
                idempotency_key,
            ).fetchrow()
        return None if row is None else _command(row)

    async def invoices(
        self,
        subject: str,
        *,
        subscription: str | None = None,
        cursor: InvoiceCursor | None = None,
        limit: int = 20,
    ) -> InvoicePage:
        _required_text(subject, "subject")
        if subscription is not None:
            _required_text(subscription, "subscription id")
        if cursor is not None and not isinstance(cursor, InvoiceCursor):
            raise TypeError("billing invoice cursor must be InvoiceCursor or None")
        if type(limit) is not int:
            raise TypeError("billing invoice limit must be an integer from 1 through 100")
        if not 1 <= limit <= 100:
            raise ValueError("billing invoice limit must be from 1 through 100")
        paid_through = None if cursor is None else cursor.paid_through
        invoice = None if cursor is None else cursor.invoice
        merchant_account = self._merchant_account(subject)
        async with self._session_factory() as session:
            if subscription is None:
                rows = await session.raw(
                    f"SELECT {_INVOICE_COLUMNS} FROM {self._invoices} "
                    "WHERE subject=$1 AND provider=$2 "
                    "AND merchant_account IS NOT DISTINCT FROM $3 AND "
                    "($4::timestamptz IS NULL OR "
                    "(paid_through,invoice_id) < ($4::timestamptz,$5::text)) "
                    "ORDER BY paid_through DESC,invoice_id DESC LIMIT $6",
                    subject,
                    self._provider,
                    merchant_account,
                    paid_through,
                    invoice,
                    limit + 1,
                ).fetch()
            else:
                rows = await session.raw(
                    f"SELECT {_INVOICE_COLUMNS} FROM {self._invoices} "
                    "WHERE subject=$1 AND provider=$2 "
                    "AND merchant_account IS NOT DISTINCT FROM $3 "
                    "AND subscription_id=$4 AND "
                    "($5::timestamptz IS NULL OR "
                    "(paid_through,invoice_id) < ($5::timestamptz,$6::text)) "
                    "ORDER BY paid_through DESC,invoice_id DESC LIMIT $7",
                    subject,
                    self._provider,
                    merchant_account,
                    subscription,
                    paid_through,
                    invoice,
                    limit + 1,
                ).fetch()
        items = tuple(_invoice(row) for row in rows[:limit])
        if len(rows) <= limit:
            return InvoicePage(items, None)
        last = items[-1]
        return InvoicePage(items, InvoiceCursor(last.paid_through, last.invoice))


class PostgresSubscriptionEntitlements:
    __slots__ = ("_access", "_catalog", "_now", "_queries", "_subject_for")

    def __init__(
        self,
        queries: PostgresBillingQueries,
        catalog: PlanCatalog,
        *,
        subject_for: Callable[[Any], str],
        access: AccessPolicy = _DEFAULT_ACCESS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(queries, PostgresBillingQueries):
            raise TypeError("billing entitlement queries must be PostgresBillingQueries")
        if not isinstance(catalog, PlanCatalog):
            raise TypeError("billing entitlement catalog must be PlanCatalog")
        if not callable(subject_for):
            raise TypeError("billing entitlement subject_for must be callable")
        if not isinstance(access, AccessPolicy):
            raise TypeError("billing entitlement access must be AccessPolicy")
        if now is not None and not callable(now):
            raise TypeError("billing entitlement now must be callable or None")
        self._queries = queries
        self._catalog = catalog
        self._subject_for = subject_for
        self._access = access
        self._now = now

    async def resolve(self, identity: Any) -> SubscriptionAccess:
        subject = self._subject_for(identity)
        _required_text(subject, "entitlement subject")
        snapshot = await self._queries.subscription(subject)
        options: dict[str, Any] = {"access": self._access}
        if self._now is not None:
            options["now"] = self._now
        resolver = SubscriptionEntitlements(
            self._catalog,
            subscription_for=lambda resolved_identity: snapshot,
            **options,
        )
        return resolver.resolve(identity)

    async def resolve_request(self, request: Any) -> SubscriptionAccess:
        identity = getattr(request, "identity", None)
        if identity is None:
            return SubscriptionAccess(None, frozenset())
        return await self.resolve(identity)

    async def entitlements(self, identity: Any) -> frozenset[str]:
        return (await self.resolve(identity)).entitlements

    async def plan_for(self, identity: Any) -> str | None:
        return (await self.resolve(identity)).plan

    def names(self) -> frozenset[str]:
        return self._catalog.entitlement_names


__all__ = [
    "InvoiceCursor",
    "InvoicePage",
    "PostgresBillingQueries",
    "PostgresSubscriptionEntitlements",
]
