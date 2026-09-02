from __future__ import annotations

from time import time
from typing import Any, cast

import pytest

from wreath._agents.approvals import (
    ApprovalDenied,
    ApprovalExpired,
    ApprovalMismatch,
    InMemoryApprovalStore,
    PostgresApprovalStore,
)


def store(clock: list[float]) -> InMemoryApprovalStore:
    return InMemoryApprovalStore(max_entries=8, clock=lambda: clock[0])


def test_approval_configuration_refuses_unbounded_capacity() -> None:
    with pytest.raises(ValueError, match="max_entries"):
        InMemoryApprovalStore(max_entries=0)


@pytest.mark.asyncio
async def test_default_store_compares_fresh_auth_in_the_unix_time_domain() -> None:
    approvals = InMemoryApprovalStore(max_entries=8)
    request = await approvals.issue(
        approval_id="fresh-default",
        tenant="tenant-a",
        principal_id="user-7",
        action="wire",
        ttl=30.0,
        require_fresh_auth=True,
    )

    assert request.issued_at > 1_000_000_000
    with pytest.raises(ApprovalMismatch, match="fresh authentication"):
        await approvals.claim(
            "fresh-default",
            tenant="tenant-a",
            principal_id="user-7",
            authenticated_at=time() - 86_400,
        )


@pytest.mark.asyncio
async def test_issue_refuses_invalid_identity_ttl_and_duplicate_id() -> None:
    approvals = store([100.0])
    for field in ("approval_id", "tenant", "principal_id", "action"):
        values = {
            "approval_id": "approval-1",
            "tenant": "tenant-a",
            "principal_id": "user-7",
            "action": "release",
        }
        values[field] = ""
        with pytest.raises(ValueError, match="non-empty"):
            await approvals.issue(
                approval_id=values["approval_id"],
                tenant=values["tenant"],
                principal_id=values["principal_id"],
                action=values["action"],
                ttl=30.0,
            )
    with pytest.raises(ValueError, match="ttl"):
        await approvals.issue(
            approval_id="approval-1",
            tenant="tenant-a",
            principal_id="user-7",
            action="release",
            ttl=0,
        )

    issued = await approvals.issue(
        approval_id="approval-1",
        tenant="tenant-a",
        principal_id="user-7",
        action="release",
        ttl=30.0,
    )
    assert issued.issued_at == 100.0
    with pytest.raises(ValueError, match="duplicate"):
        await approvals.issue(
            approval_id="approval-1",
            tenant="tenant-a",
            principal_id="user-7",
            action="release",
            ttl=30.0,
        )


@pytest.mark.asyncio
async def test_approval_is_expiring_single_use_and_bound_to_tenant_and_principal() -> None:
    clock = [100.0]
    approvals = store(clock)
    approval = await approvals.issue(
        approval_id="approval-1",
        tenant="tenant-a",
        principal_id="user-7",
        action="release",
        resource="version:3",
        ttl=30.0,
    )

    with pytest.raises(ApprovalMismatch, match="tenant"):
        await approvals.claim("approval-1", tenant="tenant-b", principal_id="user-7")
    with pytest.raises(ApprovalMismatch, match="principal"):
        await approvals.claim("approval-1", tenant="tenant-a", principal_id="user-8")

    grant = await approvals.claim("approval-1", tenant="tenant-a", principal_id="user-7")
    assert grant.approval_id == approval.approval_id
    assert grant.action == "release"
    assert grant.resource == "version:3"
    with pytest.raises(ApprovalMismatch, match="already used"):
        await approvals.claim("approval-1", tenant="tenant-a", principal_id="user-7")


@pytest.mark.asyncio
async def test_expiry_and_deny_are_explicit_refusals() -> None:
    clock = [100.0]
    approvals = store(clock)
    await approvals.issue(
        approval_id="expired",
        tenant="tenant-a",
        principal_id="user-7",
        action="release",
        ttl=2.0,
    )
    await approvals.issue(
        approval_id="denied",
        tenant="tenant-a",
        principal_id="user-7",
        action="delete",
        ttl=30.0,
    )
    await approvals.deny("denied", tenant="tenant-a", principal_id="user-7")

    clock[0] = 103.0
    with pytest.raises(ApprovalExpired, match="expired"):
        await approvals.claim("expired", tenant="tenant-a", principal_id="user-7")
    with pytest.raises(ApprovalDenied, match="denied"):
        await approvals.claim("denied", tenant="tenant-a", principal_id="user-7")
    with pytest.raises(ApprovalDenied, match="already denied"):
        await approvals.deny("denied", tenant="tenant-a", principal_id="user-7")


@pytest.mark.asyncio
async def test_fresh_auth_requirement_uses_the_authentication_instant() -> None:
    clock = [100.0]
    approvals = store(clock)
    await approvals.issue(
        approval_id="fresh",
        tenant="tenant-a",
        principal_id="user-7",
        action="wire",
        ttl=30.0,
        require_fresh_auth=True,
        issued_at=100.0,
    )

    with pytest.raises(ApprovalMismatch, match="fresh authentication"):
        await approvals.claim("fresh", tenant="tenant-a", principal_id="user-7")
    with pytest.raises(ApprovalMismatch, match="fresh authentication"):
        await approvals.claim(
            "fresh",
            tenant="tenant-a",
            principal_id="user-7",
            authenticated_at=99.0,
        )

    assert (
        await approvals.claim(
            "fresh",
            tenant="tenant-a",
            principal_id="user-7",
            authenticated_at=100.0,
        )
    ).principal_id == "user-7"

    with pytest.raises(ApprovalMismatch, match="already used"):
        await approvals.deny("fresh", tenant="tenant-a", principal_id="user-7")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
async def test_approval_time_facts_must_be_finite(value: float) -> None:
    approvals = store([100.0])
    with pytest.raises(ValueError, match="finite"):
        await approvals.issue(
            approval_id="bad-ttl",
            tenant="tenant-a",
            principal_id="user-7",
            action="release",
            ttl=value,
        )
    with pytest.raises(ValueError, match="finite"):
        await approvals.issue(
            approval_id="bad-time",
            tenant="tenant-a",
            principal_id="user-7",
            action="release",
            ttl=30,
            issued_at=value,
        )

    await approvals.issue(
        approval_id="fresh",
        tenant="tenant-a",
        principal_id="user-7",
        action="release",
        ttl=30,
        require_fresh_auth=True,
    )
    with pytest.raises(ApprovalMismatch, match="finite authentication"):
        await approvals.claim(
            "fresh",
            tenant="tenant-a",
            principal_id="user-7",
            authenticated_at=value,
        )


@pytest.mark.asyncio
async def test_explicit_issue_time_is_not_replaced_by_store_clock() -> None:
    approvals = store([110.0])

    request = await approvals.issue(
        approval_id="historical",
        tenant="tenant-a",
        principal_id="user-7",
        action="release",
        ttl=30.0,
        issued_at=100.0,
    )

    assert request.issued_at == 100.0
    assert request.expires_at == 130.0


@pytest.mark.asyncio
async def test_explicit_issue_time_cannot_rewind_or_advance_store_time() -> None:
    clock = [100.0]
    approvals = store(clock)
    await approvals.issue(
        approval_id="safe",
        tenant="tenant-a",
        principal_id="user-7",
        action="release",
        ttl=50.0,
    )

    with pytest.raises(ValueError, match="future"):
        await approvals.issue(
            approval_id="future",
            tenant="tenant-a",
            principal_id="user-7",
            action="release",
            ttl=30.0,
            issued_at=10_000.0,
        )

    assert (
        await approvals.claim("safe", tenant="tenant-a", principal_id="user-7")
    ).approval_id == "safe"


@pytest.mark.asyncio
async def test_claim_refuses_if_approval_expires_during_atomic_transition() -> None:
    readings = iter((0.0, 0.0, 9.0, 11.0))
    approvals = InMemoryApprovalStore(max_entries=8, clock=lambda: next(readings))
    await approvals.issue(
        approval_id="edge",
        tenant="tenant-a",
        principal_id="user-7",
        action="release",
        ttl=10.0,
        issued_at=0.0,
    )

    with pytest.raises(ApprovalExpired, match="while being claimed"):
        await approvals.claim("edge", tenant="tenant-a", principal_id="user-7")


@pytest.mark.asyncio
async def test_deny_refuses_if_approval_expires_during_atomic_transition() -> None:
    readings = iter((0.0, 0.0, 9.0, 11.0))
    approvals = InMemoryApprovalStore(max_entries=8, clock=lambda: next(readings))
    await approvals.issue(
        approval_id="edge",
        tenant="tenant-a",
        principal_id="user-7",
        action="release",
        ttl=10.0,
        issued_at=0.0,
    )

    with pytest.raises(ApprovalExpired, match="while being denied"):
        await approvals.deny("edge", tenant="tenant-a", principal_id="user-7")


@pytest.mark.asyncio
async def test_capacity_refuses_instead_of_evicting_a_live_approval() -> None:
    approvals = InMemoryApprovalStore(max_entries=1)
    await approvals.issue(
        approval_id="first",
        tenant="tenant-a",
        principal_id="user-7",
        action="release",
        ttl=30.0,
    )

    with pytest.raises(OverflowError, match="capacity"):
        await approvals.issue(
            approval_id="second",
            tenant="tenant-a",
            principal_id="user-7",
            action="delete",
            ttl=30.0,
        )


class _PostgresResult:
    def __init__(self, session: _PostgresSession) -> None:
        self._session = session

    async def fetchrow(self) -> object:
        return self._session.rows.pop(0)


class _PostgresSession:
    def __init__(self, *rows: object) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> _PostgresSession:
        self.entered += 1
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exited += 1

    def raw(self, sql: str, *parameters: object) -> _PostgresResult:
        self.calls.append((sql, parameters))
        return _PostgresResult(self)


def _approval_row(*, state: str = "pending", require_fresh_auth: bool = False) -> dict[str, object]:
    return {
        "approval_id": "approval-1",
        "tenant": "tenant-a",
        "principal_id": "user-7",
        "action": "billing.refund",
        "resource": "payment:pi_1",
        "issued_at": 100.0,
        "expires_at": 130.0,
        "require_fresh_auth": require_fresh_auth,
        "state": state,
    }


def _postgres_store(session: _PostgresSession) -> PostgresApprovalStore:
    return PostgresApprovalStore(lambda: session, clock=lambda: 100.0)


def test_postgres_approval_store_declares_qualified_additive_schema() -> None:
    approvals = PostgresApprovalStore(lambda: _PostgresSession(), schema="private")

    component = approvals.component()

    assert component.name == "agent-approvals"
    assert component.schema == "private"
    assert component.relations == ("agent_approvals",)
    sql = approvals.schema_sql()
    assert 'CREATE TABLE IF NOT EXISTS "private"."agent_approvals"' in sql
    assert "PRIMARY KEY (approval_id)" in sql
    assert "CHECK (state IN ('pending','denied','used'))" in sql


@pytest.mark.asyncio
async def test_postgres_issue_is_one_atomic_expired_id_replacing_insert() -> None:
    session = _PostgresSession(_approval_row(require_fresh_auth=True))
    approvals = _postgres_store(session)

    request = await approvals.issue(
        approval_id="approval-1",
        tenant="tenant-a",
        principal_id="user-7",
        action="billing.refund",
        resource="payment:pi_1",
        ttl=30.0,
        require_fresh_auth=True,
    )

    assert request.approval_id == "approval-1"
    assert request.require_fresh_auth is True
    assert request.expires_at == 130.0
    assert session.entered == session.exited == 1
    assert len(session.calls) == 1
    sql, parameters = session.calls[0]
    assert "ON CONFLICT (approval_id) DO UPDATE" in sql
    assert "expires_at <= $9::float8" in sql
    assert parameters == (
        "approval-1",
        "tenant-a",
        "user-7",
        "billing.refund",
        "payment:pi_1",
        100.0,
        130.0,
        True,
        100.0,
    )


@pytest.mark.asyncio
async def test_postgres_issue_refuses_a_live_duplicate_atomically() -> None:
    approvals = _postgres_store(_PostgresSession(None))

    with pytest.raises(ValueError, match="duplicate approval ID 'approval-1'"):
        await approvals.issue(
            approval_id="approval-1",
            tenant="tenant-a",
            principal_id="user-7",
            action="billing.refund",
            ttl=30.0,
        )


@pytest.mark.asyncio
async def test_postgres_claim_is_one_bound_conditional_transition() -> None:
    session = _PostgresSession(_approval_row())
    approvals = _postgres_store(session)

    grant = await approvals.claim(
        "approval-1",
        tenant="tenant-a",
        principal_id="user-7",
        authenticated_at=100.0,
    )

    assert grant.action == "billing.refund"
    assert grant.resource == "payment:pi_1"
    assert grant.approved_at == 100.0
    assert len(session.calls) == 1
    sql, parameters = session.calls[0]
    assert "UPDATE" in sql
    assert "state='pending'" in sql
    assert "expires_at > $4::float8" in sql
    assert "tenant=$2::text" in sql
    assert "principal_id=$3::text" in sql
    assert "authenticated_at" not in sql
    assert parameters == ("approval-1", "tenant-a", "user-7", 100.0, 100.0)


@pytest.mark.asyncio
async def test_postgres_claim_reports_the_stored_refusal_after_losing_the_transition() -> None:
    session = _PostgresSession(None, _approval_row(state="used"))
    approvals = _postgres_store(session)

    with pytest.raises(ApprovalMismatch, match="already used"):
        await approvals.claim(
            "approval-1",
            tenant="tenant-a",
            principal_id="user-7",
        )

    assert len(session.calls) == 2
    assert session.calls[1][1] == ("approval-1",)


@pytest.mark.asyncio
async def test_postgres_claim_fresh_auth_is_checked_by_the_atomic_update() -> None:
    session = _PostgresSession(None, _approval_row(require_fresh_auth=True))
    approvals = _postgres_store(session)

    with pytest.raises(ApprovalMismatch, match="fresh authentication"):
        await approvals.claim(
            "approval-1",
            tenant="tenant-a",
            principal_id="user-7",
            authenticated_at=99.0,
        )

    sql, parameters = session.calls[0]
    assert "NOT require_fresh_auth" in sql
    assert "$5::float8 >= issued_at" in sql
    assert parameters[-1] == 99.0


@pytest.mark.asyncio
async def test_postgres_deny_is_one_bound_conditional_transition() -> None:
    session = _PostgresSession({"approval_id": "approval-1"})
    approvals = _postgres_store(session)

    await approvals.deny("approval-1", tenant="tenant-a", principal_id="user-7")

    assert len(session.calls) == 1
    sql, parameters = session.calls[0]
    assert "SET state='denied'" in sql
    assert "state='pending'" in sql
    assert "expires_at > $4::float8" in sql
    assert parameters == ("approval-1", "tenant-a", "user-7", 100.0)


def test_postgres_configuration_refuses_invalid_factory_and_schema() -> None:
    with pytest.raises(TypeError, match="session_factory"):
        PostgresApprovalStore(cast(Any, object()))
    with pytest.raises(ValueError, match="SQL identifier"):
        PostgresApprovalStore(lambda: _PostgresSession(), schema='bad"schema')
