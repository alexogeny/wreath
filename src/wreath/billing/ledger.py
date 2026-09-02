from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .._leased import claim_sql
from .._pgname import quote_identifier
from ..payments import PaymentSnapshot
from ..schema import Component, Step
from ..subscriptions import SubscriptionPayment, SubscriptionSnapshot

_DIGEST = re.compile(r"[0-9a-f]{64}")


class BillingCommandState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SENDING = "sending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


def _required_text(value: str, field: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"billing command {field} must not be empty")


def _merchant_account(value: str | None) -> None:
    if value is not None and (type(value) is not str or not value):
        raise ValueError("billing merchant_account must be None or a non-empty string")


@dataclass(frozen=True, slots=True)
class BillingCommandIdentity:
    provider: str
    operation: str
    idempotency_key: str
    digest: str
    subject: str
    merchant_account: str | None = None

    def __post_init__(self) -> None:
        for field in ("provider", "operation", "idempotency_key", "subject"):
            _required_text(getattr(self, field), field)
        if type(self.digest) is not str or _DIGEST.fullmatch(self.digest) is None:
            raise ValueError("billing command digest must be 64 lower-case hexadecimal characters")
        _merchant_account(self.merchant_account)


@dataclass(frozen=True, slots=True)
class BillingCommand:
    identity: BillingCommandIdentity
    state: BillingCommandState
    fencing_token: int
    provider_reference: str | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, BillingCommandIdentity):
            raise TypeError("billing command identity must be BillingCommandIdentity")
        if not isinstance(self.state, BillingCommandState):
            raise TypeError("billing command state must be BillingCommandState")
        if type(self.fencing_token) is not int or self.fencing_token < 0:
            raise ValueError("billing command fencing_token must be a non-negative integer")
        for field in ("provider_reference", "failure_code"):
            value = getattr(self, field)
            if value is not None and (type(value) is not str or not value):
                raise ValueError(f"billing command {field} must be None or a non-empty string")


def _row_value(row: Any, key: str, index: int) -> Any:
    try:
        return row[key]
    except KeyError, TypeError:
        return row[index]


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


_COMMAND_COLUMNS = (
    "provider,operation,idempotency_key,digest,subject,merchant_account,state,"
    "fencing_token,provider_reference,failure_code"
)


class PostgresBillingLedger:
    __slots__ = (
        "_commands",
        "_component_name",
        "_invoices",
        "_payments",
        "_reconciliation",
        "_schema",
        "_subscriptions",
    )

    def __init__(
        self,
        *,
        schema: str = "wreath",
        component_name: str = "billing-ledger",
    ) -> None:
        if type(component_name) is not str or not component_name:
            raise ValueError("billing ledger component_name must be a non-empty string")
        quoted = quote_identifier(schema, reject_quote=True)
        self._component_name = component_name
        self._schema = schema
        self._commands = f'{quoted}."billing_commands"'
        self._payments = f'{quoted}."billing_payments"'
        self._subscriptions = f'{quoted}."billing_subscriptions"'
        self._invoices = f'{quoted}."billing_invoices"'
        self._reconciliation = f'{quoted}."billing_reconciliation"'

    def component(self) -> Component:
        commands = self._commands
        payments = self._payments
        subscriptions = self._subscriptions
        invoices = self._invoices
        reconciliation = self._reconciliation
        return Component(
            name=self._component_name,
            schema=self._schema,
            relations=(
                "billing_commands",
                "billing_payments",
                "billing_subscriptions",
                "billing_invoices",
                "billing_reconciliation",
            ),
            steps=(
                Step(
                    version=1,
                    statements=(
                        f"CREATE TABLE IF NOT EXISTS {commands} (\n"
                        "  command_id bigint GENERATED ALWAYS AS IDENTITY UNIQUE,\n"
                        "  provider text NOT NULL,\n"
                        "  operation text NOT NULL,\n"
                        "  idempotency_key text NOT NULL,\n"
                        "  digest char(64) NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),\n"
                        "  subject text NOT NULL,\n"
                        "  merchant_account text,\n"
                        "  state text NOT NULL DEFAULT 'pending' CHECK (state IN "
                        "('pending','leased','sending','confirmed','failed','unknown')),\n"
                        "  lease_owner text,\n"
                        "  lease_expires_at timestamptz,\n"
                        "  fencing_token bigint NOT NULL DEFAULT 0,\n"
                        "  provider_reference text,\n"
                        "  failure_code text,\n"
                        "  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
                        "  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
                        "  completed_at timestamptz,\n"
                        "  PRIMARY KEY (provider, operation, idempotency_key)\n"
                        ")",
                        f"CREATE INDEX IF NOT EXISTS billing_commands_claim_idx ON {commands} "
                        "(created_at) WHERE state IN ('pending','leased')",
                        f"CREATE TABLE IF NOT EXISTS {payments} (\n"
                        "  provider text NOT NULL,\n"
                        "  payment_id text NOT NULL,\n"
                        "  subject text NOT NULL,\n"
                        "  reference text NOT NULL,\n"
                        "  currency char(3) NOT NULL,\n"
                        "  amount_minor bigint NOT NULL CHECK (amount_minor >= 0),\n"
                        "  state text NOT NULL CHECK (state IN ('pending','succeeded','failed')),\n"
                        "  customer text,\n"
                        "  merchant_account text,\n"
                        "  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
                        "  PRIMARY KEY (provider, payment_id)\n"
                        ")",
                        f"CREATE UNIQUE INDEX IF NOT EXISTS billing_payments_reference_idx ON "
                        f"{payments} (provider,reference,merchant_account) NULLS NOT DISTINCT",
                        f"CREATE TABLE IF NOT EXISTS {subscriptions} (\n"
                        "  provider text NOT NULL,\n"
                        "  subscription_id text NOT NULL,\n"
                        "  subject text NOT NULL,\n"
                        "  merchant_account text,\n"
                        "  plan text,\n"
                        "  state text,\n"
                        "  provider_state text,\n"
                        "  paid_through timestamptz,\n"
                        "  trial_ends_at timestamptz,\n"
                        "  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
                        "  PRIMARY KEY (provider, subscription_id)\n"
                        ")",
                        f"CREATE INDEX IF NOT EXISTS billing_subscriptions_subject_idx ON "
                        f"{subscriptions} (subject,provider)",
                        f"CREATE TABLE IF NOT EXISTS {invoices} (\n"
                        "  provider text NOT NULL,\n"
                        "  invoice_id text NOT NULL,\n"
                        "  subscription_id text NOT NULL,\n"
                        "  subject text NOT NULL,\n"
                        "  merchant_account text,\n"
                        "  paid_through timestamptz NOT NULL,\n"
                        "  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
                        "  PRIMARY KEY (provider, invoice_id)\n"
                        ")",
                        f"CREATE INDEX IF NOT EXISTS billing_invoices_subscription_idx ON "
                        f"{invoices} "
                        "(subject,provider,subscription_id,paid_through DESC,invoice_id DESC)",
                        f"CREATE INDEX IF NOT EXISTS billing_invoices_subject_idx ON "
                        f"{invoices} (subject,provider,paid_through DESC,invoice_id DESC)",
                        f"CREATE TABLE IF NOT EXISTS {reconciliation} (\n"
                        "  provider text NOT NULL,\n"
                        "  merchant_account text NOT NULL,\n"
                        "  cursor text,\n"
                        "  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
                        "  PRIMARY KEY (provider, merchant_account)\n"
                        ")",
                    ),
                ),
            ),
        )

    def schema_sql(self) -> str:
        return self.component().sql()

    async def load(
        self,
        session: Any,
        *,
        provider: str,
        merchant_account: str | None,
    ) -> str | None:
        _required_text(provider, "reconciliation provider")
        _merchant_account(merchant_account)
        return await session.raw(
            f"SELECT cursor FROM {self._reconciliation} "
            "WHERE provider=$1 AND merchant_account=COALESCE($2,'')",
            provider,
            merchant_account,
        ).fetchval()

    async def advance(
        self,
        session: Any,
        *,
        provider: str,
        merchant_account: str | None,
        expected: str | None,
        cursor: str | None,
    ) -> bool:
        _required_text(provider, "reconciliation provider")
        _merchant_account(merchant_account)
        for field, value in (("expected cursor", expected), ("cursor", cursor)):
            if value is not None and (type(value) is not str or not value):
                raise ValueError(f"billing reconciliation {field} must not be empty")
        advanced = await session.raw(
            f"INSERT INTO {self._reconciliation} AS current "
            "(provider,merchant_account,cursor) "
            "SELECT $1,COALESCE($2,''),$3 WHERE $4::text IS NULL "
            "ON CONFLICT (provider,merchant_account) DO UPDATE "
            "SET cursor=EXCLUDED.cursor,updated_at=clock_timestamp() "
            "WHERE current.cursor IS NOT DISTINCT FROM $4 RETURNING TRUE",
            provider,
            merchant_account,
            cursor,
            expected,
        ).fetchval()
        return advanced is True

    async def register_command(
        self,
        session: Any,
        identity: BillingCommandIdentity,
    ) -> BillingCommand:
        if not isinstance(identity, BillingCommandIdentity):
            raise TypeError("billing command must use BillingCommandIdentity")
        row = await session.raw(
            f"INSERT INTO {self._commands} "
            "(provider,operation,idempotency_key,digest,subject,merchant_account) "
            "VALUES ($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT (provider,operation,idempotency_key) DO NOTHING "
            f"RETURNING {_COMMAND_COLUMNS}",
            identity.provider,
            identity.operation,
            identity.idempotency_key,
            identity.digest,
            identity.subject,
            identity.merchant_account,
        ).fetchrow()
        if row is None:
            row = await session.raw(
                f"SELECT {_COMMAND_COLUMNS} FROM {self._commands} "
                "WHERE provider=$1 AND operation=$2 AND idempotency_key=$3",
                identity.provider,
                identity.operation,
                identity.idempotency_key,
            ).fetchrow()
        if row is None:
            raise RuntimeError("billing command disappeared after its idempotency conflict")
        found = _command(row)
        if found.identity.digest != identity.digest:
            raise ValueError(
                f"billing command {identity.idempotency_key!r} contradicts its immutable digest"
            )
        if found.identity.subject != identity.subject:
            raise ValueError(
                f"billing command {identity.idempotency_key!r} contradicts its immutable subject"
            )
        if found.identity.merchant_account != identity.merchant_account:
            raise ValueError(
                f"billing command {identity.idempotency_key!r} contradicts its immutable "
                "merchant account"
            )
        return found

    async def claim_command(
        self,
        session: Any,
        *,
        lease_owner: str,
        lease_seconds: float,
    ) -> BillingCommand | None:
        if type(lease_owner) is not str or not lease_owner:
            raise ValueError("billing command lease_owner must not be empty")
        if type(lease_seconds) not in {int, float} or lease_seconds <= 0:
            raise ValueError("billing command lease_seconds must be positive")
        sql = claim_sql(
            self._commands,
            key="command_id",
            alias="AS b",
            predicate=(
                "state IN ('pending','leased') AND (state='pending' OR "
                "lease_expires_at < clock_timestamp())"
            ),
            order="created_at",
            limit="1",
            assignments=(
                "state='leased',lease_owner=$1,lease_expires_at=clock_timestamp()+"
                "$2::float8*interval '1 second',fencing_token=b.fencing_token+1,"
                "updated_at=clock_timestamp()"
            ),
            returning=f"{_COMMAND_COLUMNS}",
        )
        row = await session.raw(sql, lease_owner, lease_seconds).fetchrow()
        return None if row is None else _command(row)

    async def settle_expired_sending(self, session: Any, *, limit: int = 1000) -> int:
        if type(limit) is not int or limit <= 0:
            raise ValueError("billing expired-sending limit must be a positive integer")
        settled = await session.raw(
            f"WITH expired AS (SELECT command_id FROM {self._commands} "
            "WHERE state='sending' AND lease_expires_at < clock_timestamp() "
            "ORDER BY lease_expires_at FOR UPDATE SKIP LOCKED LIMIT $1), "
            f"changed AS (UPDATE {self._commands} AS c SET state='unknown',"
            "failure_code='lease_expired_after_send',lease_owner=NULL,"
            "lease_expires_at=NULL,fencing_token=c.fencing_token+1,"
            "completed_at=clock_timestamp(),updated_at=clock_timestamp() "
            "FROM expired AS e WHERE c.command_id=e.command_id RETURNING 1) "
            "SELECT count(*) FROM changed",
            limit,
        ).fetchval()
        return int(settled or 0)

    async def mark_sending(self, session: Any, command: BillingCommand) -> BillingCommand:
        return await self._transition(
            session,
            command,
            source=BillingCommandState.LEASED,
            target=BillingCommandState.SENDING,
            assignments="state='sending',updated_at=clock_timestamp()",
        )

    async def mark_confirmed(
        self,
        session: Any,
        command: BillingCommand,
        *,
        provider_reference: str,
    ) -> BillingCommand:
        _required_text(provider_reference, "provider_reference")
        return await self._transition(
            session,
            command,
            source=BillingCommandState.SENDING,
            target=BillingCommandState.CONFIRMED,
            assignments=(
                "state='confirmed',provider_reference=$5,lease_owner=NULL,"
                "lease_expires_at=NULL,completed_at=clock_timestamp(),"
                "updated_at=clock_timestamp()"
            ),
            values=(provider_reference,),
            provider_reference=provider_reference,
        )

    async def mark_failed(
        self,
        session: Any,
        command: BillingCommand,
        *,
        failure_code: str,
    ) -> BillingCommand:
        return await self._settle_failure(
            session,
            command,
            target=BillingCommandState.FAILED,
            failure_code=failure_code,
        )

    async def mark_unknown(
        self,
        session: Any,
        command: BillingCommand,
        *,
        failure_code: str,
    ) -> BillingCommand:
        return await self._settle_failure(
            session,
            command,
            target=BillingCommandState.UNKNOWN,
            failure_code=failure_code,
        )

    async def _settle_failure(
        self,
        session: Any,
        command: BillingCommand,
        *,
        target: BillingCommandState,
        failure_code: str,
    ) -> BillingCommand:
        _required_text(failure_code, "failure_code")
        if len(failure_code) > 256:
            raise ValueError("billing command failure_code must be at most 256 characters")
        return await self._transition(
            session,
            command,
            source=BillingCommandState.SENDING,
            target=target,
            assignments=(
                f"state='{target.value}',failure_code=$5,lease_owner=NULL,"
                "lease_expires_at=NULL,completed_at=clock_timestamp(),"
                "updated_at=clock_timestamp()"
            ),
            values=(failure_code,),
            failure_code=failure_code,
        )

    async def _transition(
        self,
        session: Any,
        command: BillingCommand,
        *,
        source: BillingCommandState,
        target: BillingCommandState,
        assignments: str,
        values: tuple[Any, ...] = (),
        provider_reference: str | None = None,
        failure_code: str | None = None,
    ) -> BillingCommand:
        if not isinstance(command, BillingCommand):
            raise TypeError("billing command transition requires BillingCommand")
        if command.state is not source:
            raise ValueError(
                f"billing command transition to {target.value!r} requires state "
                f"{source.value!r}, not {command.state.value!r}"
            )
        identity = command.identity
        updated = await session.raw(
            f"UPDATE {self._commands} SET {assignments} "
            "WHERE provider=$1 AND operation=$2 AND idempotency_key=$3 "
            f"AND fencing_token=$4 AND state='{source.value}' RETURNING 1",
            identity.provider,
            identity.operation,
            identity.idempotency_key,
            command.fencing_token,
            *values,
        ).fetchval()
        if updated is None:
            raise RuntimeError("stale billing command fencing token")
        return replace(
            command,
            state=target,
            provider_reference=provider_reference,
            failure_code=failure_code,
        )

    async def apply_subscription(
        self,
        session: Any,
        snapshot: SubscriptionSnapshot,
        *,
        merchant_account: str | None = None,
    ) -> None:
        if not isinstance(snapshot, SubscriptionSnapshot):
            raise TypeError("billing subscription projection requires SubscriptionSnapshot")
        if (
            merchant_account is not None
            and snapshot.merchant_account is not None
            and merchant_account != snapshot.merchant_account
        ):
            raise ValueError("subscription merchant_account contradicts its projection")
        if snapshot.merchant_account is not None:
            merchant_account = snapshot.merchant_account
        _merchant_account(merchant_account)
        await self._ensure_subscription_owner(
            session,
            provider=snapshot.provider,
            subscription=snapshot.id,
            subject=snapshot.subject,
            merchant_account=merchant_account,
        )
        await session.raw(
            f"UPDATE {self._subscriptions} AS s SET plan=$4,state=$5,provider_state=$6,"
            "paid_through=GREATEST(s.paid_through,$7::timestamptz,(SELECT max(paid_through) "
            f"FROM {self._invoices} WHERE provider=$1 AND subscription_id=$2)),"
            "trial_ends_at=$8,updated_at=clock_timestamp() "
            "WHERE provider=$1 AND subscription_id=$2 AND subject=$3",
            snapshot.provider,
            snapshot.id,
            snapshot.subject,
            snapshot.plan,
            snapshot.state.value,
            snapshot.provider_state,
            snapshot.paid_through,
            snapshot.trial_ends_at,
        ).execute()

    async def apply_checkout(self, session: Any, payment: PaymentSnapshot) -> None:
        if not isinstance(payment, PaymentSnapshot):
            raise TypeError("billing checkout projection requires PaymentSnapshot")
        row = await session.raw(
            f"INSERT INTO {self._payments} AS existing "
            "(provider,payment_id,subject,reference,currency,amount_minor,state,customer,"
            "merchant_account) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
            "ON CONFLICT (provider,payment_id) DO UPDATE SET state=CASE "
            "WHEN existing.state='succeeded' OR EXCLUDED.state='succeeded' THEN 'succeeded' "
            "WHEN existing.state='failed' OR EXCLUDED.state='failed' THEN 'failed' "
            "ELSE 'pending' END,updated_at=clock_timestamp() WHERE "
            "existing.subject=EXCLUDED.subject AND existing.reference=EXCLUDED.reference AND "
            "existing.currency=EXCLUDED.currency AND "
            "existing.amount_minor=EXCLUDED.amount_minor AND "
            "existing.customer IS NOT DISTINCT FROM EXCLUDED.customer AND "
            "existing.merchant_account IS NOT DISTINCT FROM EXCLUDED.merchant_account "
            "RETURNING payment_id",
            payment.provider,
            payment.id,
            payment.subject,
            payment.reference,
            payment.amount.currency,
            payment.amount.minor,
            payment.state.value,
            payment.customer,
            payment.merchant_account,
        ).fetchrow()
        if row is None:
            raise ValueError(
                f"checkout payment {payment.id!r} contradicts its immutable ownership or amount"
            )

    async def apply_payment(
        self,
        session: Any,
        payment: SubscriptionPayment,
        *,
        merchant_account: str | None = None,
    ) -> None:
        if not isinstance(payment, SubscriptionPayment):
            raise TypeError("billing payment projection requires SubscriptionPayment")
        if (
            merchant_account is not None
            and payment.merchant_account is not None
            and merchant_account != payment.merchant_account
        ):
            raise ValueError("subscription payment merchant_account contradicts its projection")
        if payment.merchant_account is not None:
            merchant_account = payment.merchant_account
        _merchant_account(merchant_account)
        await self._ensure_subscription_owner(
            session,
            provider=payment.provider,
            subscription=payment.subscription,
            subject=payment.subject,
            merchant_account=merchant_account,
        )
        inserted = await session.raw(
            f"INSERT INTO {self._invoices} "
            "(provider,invoice_id,subscription_id,subject,merchant_account,paid_through) "
            "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (provider,invoice_id) DO NOTHING "
            "RETURNING subscription_id,subject,merchant_account,paid_through",
            payment.provider,
            payment.invoice,
            payment.subscription,
            payment.subject,
            merchant_account,
            payment.paid_through,
        ).fetchrow()
        if inserted is None:
            existing = await session.raw(
                f"SELECT subscription_id,subject,merchant_account,paid_through FROM "
                f"{self._invoices} WHERE provider=$1 AND invoice_id=$2",
                payment.provider,
                payment.invoice,
            ).fetchrow()
            expected = (
                payment.subscription,
                payment.subject,
                merchant_account,
                payment.paid_through,
            )
            if (
                existing is None
                or tuple(
                    _row_value(existing, field, index)
                    for index, field in enumerate(
                        ("subscription_id", "subject", "merchant_account", "paid_through")
                    )
                )
                != expected
            ):
                raise ValueError(
                    f"subscription payment invoice {payment.invoice!r} contradicts its first value"
                )
        await session.raw(
            f"UPDATE {self._subscriptions} SET "
            "paid_through=GREATEST(paid_through,$5::timestamptz),updated_at=clock_timestamp() "
            "WHERE provider=$1 AND subscription_id=$2 AND subject=$3 "
            "AND merchant_account IS NOT DISTINCT FROM $4",
            payment.provider,
            payment.subscription,
            payment.subject,
            merchant_account,
            payment.paid_through,
        ).execute()

    async def _ensure_subscription_owner(
        self,
        session: Any,
        *,
        provider: str,
        subscription: str,
        subject: str,
        merchant_account: str | None,
    ) -> None:
        row = await session.raw(
            f"INSERT INTO {self._subscriptions} "
            "(provider,subscription_id,subject,merchant_account) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (provider,subscription_id) DO NOTHING "
            "RETURNING subject,merchant_account",
            provider,
            subscription,
            subject,
            merchant_account,
        ).fetchrow()
        if row is None:
            row = await session.raw(
                f"SELECT subject,merchant_account FROM {self._subscriptions} "
                "WHERE provider=$1 AND subscription_id=$2",
                provider,
                subscription,
            ).fetchrow()
        if row is None:
            raise RuntimeError("billing subscription disappeared after its ownership conflict")
        owner = (
            _row_value(row, "subject", 0),
            _row_value(row, "merchant_account", 1),
        )
        if owner != (subject, merchant_account):
            raise ValueError(
                f"subscription {subscription!r} contradicts its immutable ownership: "
                f"expected {(subject, merchant_account)!r}, found {owner!r}"
            )


__all__ = [
    "BillingCommand",
    "BillingCommandIdentity",
    "BillingCommandState",
    "PostgresBillingLedger",
]
