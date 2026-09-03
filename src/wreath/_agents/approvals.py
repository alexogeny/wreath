from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from math import isfinite
from time import time
from typing import Any, Literal, Protocol, runtime_checkable

from .._capability_map import CapabilityMap
from .._pgname import quote_identifier
from ..schema import Component, Step


class ApprovalExpired(RuntimeError):
    pass


class ApprovalDenied(RuntimeError):
    pass


class ApprovalMismatch(RuntimeError):
    pass


def _finite_time(value: float, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"approval {label} must be a finite number")
    return float(value)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    approval_id: str
    tenant: str
    principal_id: str
    action: str
    resource: str | None
    issued_at: float
    expires_at: float
    require_fresh_auth: bool = False
    state: Literal["pending", "denied", "used"] = "pending"


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    approval_id: str
    tenant: str
    principal_id: str
    action: str
    resource: str | None
    approved_at: float


@runtime_checkable
class ApprovalStore(Protocol):
    async def issue(
        self,
        *,
        approval_id: str,
        tenant: str,
        principal_id: str,
        action: str,
        resource: str | None = None,
        ttl: float,
        require_fresh_auth: bool = False,
        issued_at: float | None = None,
    ) -> ApprovalRequest: ...

    async def claim(
        self,
        approval_id: str,
        *,
        tenant: str,
        principal_id: str,
        authenticated_at: float | None = None,
    ) -> ApprovalGrant: ...

    async def deny(self, approval_id: str, *, tenant: str, principal_id: str) -> None: ...


class InMemoryApprovalStore:
    __slots__ = ("_clock", "_records")

    def __init__(
        self,
        *,
        max_entries: int = 4096,
        clock: Callable[[], float] = time,
    ) -> None:
        if max_entries < 1:
            raise ValueError("approval max_entries must be positive")
        self._clock = clock
        self._records = CapabilityMap(
            max_entries=max_entries,
            clock=clock,
            overflow="refuse",
        )

    @staticmethod
    def _key(approval_id: str, tenant: str) -> tuple[str, str]:
        return tenant, approval_id

    async def issue(
        self,
        *,
        approval_id: str,
        tenant: str,
        principal_id: str,
        action: str,
        resource: str | None = None,
        ttl: float,
        require_fresh_auth: bool = False,
        issued_at: float | None = None,
    ) -> ApprovalRequest:
        if not all((approval_id, tenant, principal_id, action)):
            raise ValueError("approvals require non-empty ID, tenant, principal, and action")
        ttl = _finite_time(ttl, label="ttl")
        if ttl <= 0:
            raise ValueError("approval ttl must be positive")
        now = _finite_time(self._clock(), label="clock")
        issued = now if issued_at is None else _finite_time(issued_at, label="issued_at")
        if issued > now:
            raise ValueError("approval issued_at cannot be in the future")
        expires_at = issued + ttl
        if expires_at <= now:
            raise ValueError("approval issued_at and ttl describe an already expired approval")
        request = ApprovalRequest(
            approval_id,
            tenant,
            principal_id,
            action,
            resource,
            issued,
            expires_at,
            require_fresh_auth,
        )
        key = self._key(approval_id, tenant)
        if not self._records.claim(key, request, ttl=expires_at - now, now=now):
            if self._records.peek(key, now=now) is not None:
                raise ValueError(f"duplicate approval ID {approval_id!r}")
            raise OverflowError("approval store is at capacity")
        return request

    def _bound(self, approval_id: str, *, tenant: str, principal_id: str) -> ApprovalRequest:
        now = _finite_time(self._clock(), label="clock")
        key = self._key(approval_id, tenant)
        request = self._records.peek(key, now=now)
        if request is None:
            raise ApprovalExpired(f"approval {approval_id!r} is unknown or expired")
        if now >= request.expires_at:
            self._records.discard(key)
            raise ApprovalExpired(f"approval {approval_id!r} expired")
        if request.tenant != tenant:
            raise ApprovalMismatch(f"approval {approval_id!r} tenant does not match {tenant!r}")
        if request.principal_id != principal_id:
            raise ApprovalMismatch(
                f"approval {approval_id!r} principal does not match {principal_id!r}"
            )
        return request

    async def claim(
        self,
        approval_id: str,
        *,
        tenant: str,
        principal_id: str,
        authenticated_at: float | None = None,
    ) -> ApprovalGrant:
        request = self._bound(approval_id, tenant=tenant, principal_id=principal_id)
        now = _finite_time(self._clock(), label="clock")
        if request.state == "denied":
            raise ApprovalDenied(f"approval {approval_id!r} was denied")
        if request.state == "used":
            raise ApprovalMismatch(f"approval {approval_id!r} was already used")
        if request.require_fresh_auth:
            if authenticated_at is None:
                raise ApprovalMismatch(f"approval {approval_id!r} requires fresh authentication")
            try:
                authenticated_at = _finite_time(
                    authenticated_at,
                    label="authentication time",
                )
            except ValueError as error:
                raise ApprovalMismatch(
                    f"approval {approval_id!r} requires a finite authentication time"
                ) from error
            if authenticated_at > now:
                raise ApprovalMismatch(
                    f"approval {approval_id!r} authentication time cannot be in the future"
                )
            if authenticated_at < request.issued_at:
                raise ApprovalMismatch(f"approval {approval_id!r} requires fresh authentication")
        key = self._key(approval_id, tenant)
        if not self._records.complete(key, replace(request, state="used"), now=now):
            raise ApprovalExpired(f"approval {approval_id!r} expired while being claimed")
        return ApprovalGrant(
            approval_id,
            tenant,
            principal_id,
            request.action,
            request.resource,
            _finite_time(self._clock(), label="clock"),
        )

    async def deny(self, approval_id: str, *, tenant: str, principal_id: str) -> None:
        request = self._bound(approval_id, tenant=tenant, principal_id=principal_id)
        if request.state == "used":
            raise ApprovalMismatch(f"approval {approval_id!r} was already used")
        if request.state == "denied":
            raise ApprovalDenied(f"approval {approval_id!r} was already denied")
        now = _finite_time(self._clock(), label="clock")
        key = self._key(approval_id, tenant)
        if not self._records.complete(key, replace(request, state="denied"), now=now):
            raise ApprovalExpired(f"approval {approval_id!r} expired while being denied")


_APPROVAL_COLUMNS = (
    "approval_id,tenant,principal_id,action,resource,issued_at,expires_at,require_fresh_auth,state"
)


def _row_value(row: Any, key: str, index: int) -> Any:
    try:
        return row[key]
    except KeyError, TypeError:
        return row[index]


def _request(row: Any) -> ApprovalRequest:
    resource = _row_value(row, "resource", 4)
    return ApprovalRequest(
        approval_id=str(_row_value(row, "approval_id", 0)),
        tenant=str(_row_value(row, "tenant", 1)),
        principal_id=str(_row_value(row, "principal_id", 2)),
        action=str(_row_value(row, "action", 3)),
        resource=None if resource is None else str(resource),
        issued_at=float(_row_value(row, "issued_at", 5)),
        expires_at=float(_row_value(row, "expires_at", 6)),
        require_fresh_auth=bool(_row_value(row, "require_fresh_auth", 7)),
        state=_row_value(row, "state", 8),
    )


class PostgresApprovalStore:
    __slots__ = ("_clock", "_schema", "_session_factory", "_table")

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        schema: str = "wreath",
        clock: Callable[[], float] = time,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("approval session_factory must be callable")
        if not callable(clock):
            raise TypeError("approval clock must be callable")
        quoted = quote_identifier(schema, reject_quote=True)
        self._session_factory = session_factory
        self._clock = clock
        self._schema = schema
        self._table = f'{quoted}."agent_approvals"'

    def component(self) -> Component:
        table = self._table
        return Component(
            name="agent-approvals",
            schema=self._schema,
            relations=("agent_approvals",),
            steps=(
                Step(
                    version=1,
                    statements=(
                        f"CREATE TABLE IF NOT EXISTS {table} (\n"
                        "  approval_id text NOT NULL,\n"
                        "  tenant text NOT NULL,\n"
                        "  principal_id text NOT NULL,\n"
                        "  action text NOT NULL,\n"
                        "  resource text,\n"
                        "  issued_at double precision NOT NULL,\n"
                        "  expires_at double precision NOT NULL,\n"
                        "  require_fresh_auth boolean NOT NULL DEFAULT false,\n"
                        "  state text NOT NULL DEFAULT 'pending' "
                        "CHECK (state IN ('pending','denied','used')),\n"
                        "  decided_at double precision,\n"
                        "  PRIMARY KEY (approval_id),\n"
                        "  CHECK (expires_at > issued_at)\n"
                        ")",
                        f"CREATE INDEX IF NOT EXISTS agent_approvals_expiry_idx ON {table} "
                        "(expires_at) WHERE state='pending'",
                    ),
                ),
                Step(
                    version=2,
                    statements=(
                        "CREATE UNIQUE INDEX IF NOT EXISTS agent_approvals_identity_idx ON "
                        f"{table} (tenant, approval_id)",
                        f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS agent_approvals_pkey",
                    ),
                ),
            ),
        )

    def schema_sql(self) -> str:
        return self.component().sql()

    async def issue(
        self,
        *,
        approval_id: str,
        tenant: str,
        principal_id: str,
        action: str,
        resource: str | None = None,
        ttl: float,
        require_fresh_auth: bool = False,
        issued_at: float | None = None,
    ) -> ApprovalRequest:
        if not all((approval_id, tenant, principal_id, action)):
            raise ValueError("approvals require non-empty ID, tenant, principal, and action")
        ttl = _finite_time(ttl, label="ttl")
        if ttl <= 0:
            raise ValueError("approval ttl must be positive")
        now = _finite_time(self._clock(), label="clock")
        issued = now if issued_at is None else _finite_time(issued_at, label="issued_at")
        if issued > now:
            raise ValueError("approval issued_at cannot be in the future")
        expires_at = issued + ttl
        if expires_at <= now:
            raise ValueError("approval issued_at and ttl describe an already expired approval")
        row = None
        async with self._session_factory() as session:
            row = await session.raw(
                f"INSERT INTO {self._table} AS stored "
                "(approval_id,tenant,principal_id,action,resource,issued_at,expires_at,"
                "require_fresh_auth,state,decided_at) "
                "VALUES ($1::text,$2::text,$3::text,$4::text,$5::text,$6::float8,"
                "$7::float8,$8::boolean,'pending',NULL) "
                "ON CONFLICT (tenant,approval_id) DO UPDATE SET "
                "tenant=EXCLUDED.tenant,principal_id=EXCLUDED.principal_id,"
                "action=EXCLUDED.action,resource=EXCLUDED.resource,"
                "issued_at=EXCLUDED.issued_at,expires_at=EXCLUDED.expires_at,"
                "require_fresh_auth=EXCLUDED.require_fresh_auth,state='pending',decided_at=NULL "
                "WHERE stored.expires_at <= $9::float8 "
                f"RETURNING {_APPROVAL_COLUMNS}",
                approval_id,
                tenant,
                principal_id,
                action,
                resource,
                issued,
                expires_at,
                require_fresh_auth,
                now,
            ).fetchrow()
        if row is None:
            raise ValueError(f"duplicate approval ID {approval_id!r}")
        return _request(row)

    async def claim(
        self,
        approval_id: str,
        *,
        tenant: str,
        principal_id: str,
        authenticated_at: float | None = None,
    ) -> ApprovalGrant:
        now = _finite_time(self._clock(), label="clock")
        authenticated = authenticated_at
        if authenticated is not None:
            try:
                authenticated = _finite_time(authenticated, label="authentication time")
            except ValueError as error:
                raise ApprovalMismatch(
                    f"approval {approval_id!r} requires a finite authentication time"
                ) from error
            if authenticated > now:
                raise ApprovalMismatch(
                    f"approval {approval_id!r} authentication time cannot be in the future"
                )
        async with self._session_factory() as session:
            row = await session.raw(
                f"UPDATE {self._table} SET state='used',decided_at=$4::float8 "
                "WHERE approval_id=$1::text AND tenant=$2::text AND principal_id=$3::text "
                "AND state='pending' AND expires_at > $4::float8 "
                "AND (NOT require_fresh_auth OR "
                "($5::float8 IS NOT NULL AND $5::float8 >= issued_at)) "
                f"RETURNING {_APPROVAL_COLUMNS}",
                approval_id,
                tenant,
                principal_id,
                now,
                authenticated,
            ).fetchrow()
            if row is None:
                stored = await session.raw(
                    f"SELECT {_APPROVAL_COLUMNS} FROM {self._table} "
                    "WHERE approval_id=$1::text AND tenant=$2::text",
                    approval_id,
                    tenant,
                ).fetchrow()
                self._raise_refusal(
                    approval_id,
                    stored,
                    tenant=tenant,
                    principal_id=principal_id,
                    authenticated_at=authenticated,
                    now=now,
                    operation="claim",
                )
        request = _request(row)
        return ApprovalGrant(
            approval_id=request.approval_id,
            tenant=request.tenant,
            principal_id=request.principal_id,
            action=request.action,
            resource=request.resource,
            approved_at=now,
        )

    async def deny(self, approval_id: str, *, tenant: str, principal_id: str) -> None:
        now = _finite_time(self._clock(), label="clock")
        async with self._session_factory() as session:
            row = await session.raw(
                f"UPDATE {self._table} SET state='denied',decided_at=$4::float8 "
                "WHERE approval_id=$1::text AND tenant=$2::text AND principal_id=$3::text "
                "AND state='pending' AND expires_at > $4::float8 "
                "RETURNING approval_id",
                approval_id,
                tenant,
                principal_id,
                now,
            ).fetchrow()
            if row is None:
                stored = await session.raw(
                    f"SELECT {_APPROVAL_COLUMNS} FROM {self._table} "
                    "WHERE approval_id=$1::text AND tenant=$2::text",
                    approval_id,
                    tenant,
                ).fetchrow()
                self._raise_refusal(
                    approval_id,
                    stored,
                    tenant=tenant,
                    principal_id=principal_id,
                    authenticated_at=None,
                    now=now,
                    operation="deny",
                )

    @staticmethod
    def _raise_refusal(
        approval_id: str,
        row: Any,
        *,
        tenant: str,
        principal_id: str,
        authenticated_at: float | None,
        now: float,
        operation: Literal["claim", "deny"],
    ) -> None:
        if row is None:
            raise ApprovalExpired(f"approval {approval_id!r} is unknown or expired")
        request = _request(row)
        if now >= request.expires_at:
            raise ApprovalExpired(f"approval {approval_id!r} expired")
        if request.tenant != tenant:
            raise ApprovalMismatch(f"approval {approval_id!r} tenant does not match {tenant!r}")
        if request.principal_id != principal_id:
            raise ApprovalMismatch(
                f"approval {approval_id!r} principal does not match {principal_id!r}"
            )
        if request.state == "denied":
            suffix = "already denied" if operation == "deny" else "was denied"
            raise ApprovalDenied(f"approval {approval_id!r} {suffix}")
        if request.state == "used":
            raise ApprovalMismatch(f"approval {approval_id!r} was already used")
        if request.require_fresh_auth and (
            authenticated_at is None or authenticated_at < request.issued_at
        ):
            raise ApprovalMismatch(f"approval {approval_id!r} requires fresh authentication")
        raise ApprovalMismatch(f"approval {approval_id!r} could not be {operation}ed")


__all__ = [
    "ApprovalDenied",
    "ApprovalExpired",
    "ApprovalGrant",
    "ApprovalMismatch",
    "ApprovalRequest",
    "ApprovalStore",
    "InMemoryApprovalStore",
    "PostgresApprovalStore",
]
