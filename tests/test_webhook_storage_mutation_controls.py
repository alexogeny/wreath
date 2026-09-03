from __future__ import annotations

from datetime import UTC, datetime

import pytest

import wreath.webhooks as webhook_module
from wreath.passes import DutyCycle
from wreath.webhooks import (
    LocalReplayStore,
    OutboxDelivery,
    PostgresWebhookInbox,
    PostgresWebhookOutbox,
    WebhookEnvelope,
    _bounded_failure,
    _retention_purge_pass,
)


class _Raw:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def fetchrow(self) -> object:
        return self.session.rows.pop(0)

    async def fetchval(self) -> object:
        return self.session.values.pop(0)


class _Session:
    def __init__(
        self,
        *,
        rows: list[object] | None = None,
        values: list[object] | None = None,
    ) -> None:
        self.rows = list(rows or [])
        self.values = list(values or [])
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def raw(self, sql: str, *args: object) -> _Raw:
        self.calls.append((sql, args))
        return _Raw(self)


def _envelope() -> WebhookEnvelope:
    return WebhookEnvelope(
        "evt", "created", "1", datetime(2026, 8, 25, tzinfo=UTC), "application/json", b"{}"
    )


def _delivery() -> OutboxDelivery:
    return OutboxDelivery(
        "delivery",
        "evt",
        "receiver",
        "created",
        datetime(2026, 8, 25, tzinfo=UTC),
        "1",
        b"{}",
        "application/json",
        "key",
        1,
        2,
    )


def test_local_replay_store_refuses_nonpositive_capacity() -> None:
    with pytest.raises(ValueError, match="bounds must be positive"):
        LocalReplayStore(max_entries=0, ttl=1)


def test_local_replay_store_refuses_nonpositive_ttl() -> None:
    with pytest.raises(ValueError, match="bounds must be positive"):
        LocalReplayStore(max_entries=1, ttl=0)


@pytest.mark.asyncio
async def test_local_replay_store_uses_monotonic_clock_by_default(monkeypatch) -> None:
    monkeypatch.setattr(webhook_module.time, "monotonic", lambda: 42.0)
    store = LocalReplayStore(max_entries=2, ttl=5)

    assert await store.claim("source", "evt") is True
    assert store._last_now == 42.0


def test_retention_pass_builds_a_default_duty_cycle() -> None:
    declaration = _retention_purge_pass(table="events", key=("event_id",))

    assert isinstance(declaration.pace, DutyCycle)


def test_retention_pass_preserves_an_explicit_duty_cycle() -> None:
    pace = DutyCycle(0.25)

    declaration = _retention_purge_pass(table="events", key=("event_id",), pace=pace)

    assert declaration.pace is pace


@pytest.mark.asyncio
async def test_inbox_claim_refuses_nonpositive_lease_before_querying() -> None:
    session = _Session()

    with pytest.raises(ValueError, match="lease_seconds must be positive"):
        await PostgresWebhookInbox().claim(
            session,
            source="source",
            envelope=_envelope(),
            lease_owner="worker",
            lease_seconds=0,
        )

    assert session.calls == []


@pytest.mark.asyncio
async def test_inbox_claim_refuses_a_row_that_disappeared() -> None:
    session = _Session(rows=[None, None])

    with pytest.raises(RuntimeError, match="disappeared inside transaction"):
        await PostgresWebhookInbox().claim(
            session,
            source="source",
            envelope=_envelope(),
            lease_owner="worker",
            lease_seconds=1,
        )


@pytest.mark.asyncio
async def test_duplicate_inbox_claim_preserves_absent_result_status() -> None:
    session = _Session(
        rows=[
            None,
            {
                "state": "completed",
                "fencing_token": 2,
                "result_status": None,
                "identity_matches": True,
            },
        ]
    )

    claim = await PostgresWebhookInbox().claim(
        session,
        source="source",
        envelope=_envelope(),
        lease_owner="worker",
        lease_seconds=1,
    )

    assert claim.outcome == "duplicate"
    assert claim.result_status is None


@pytest.mark.asyncio
async def test_inbox_purge_normalizes_an_absent_count_to_zero() -> None:
    session = _Session(values=[None])

    assert await PostgresWebhookInbox().purge(session, limit=1) == 0


@pytest.mark.asyncio
async def test_outbox_purge_refuses_nonpositive_limit_before_querying() -> None:
    session = _Session()

    with pytest.raises(ValueError, match="purge limit must be positive"):
        await PostgresWebhookOutbox().purge(session, limit=0)

    assert session.calls == []


@pytest.mark.asyncio
async def test_outbox_purge_normalizes_an_absent_count_to_zero() -> None:
    session = _Session(values=[None])

    assert await PostgresWebhookOutbox().purge(session, limit=1) == 0


@pytest.mark.asyncio
async def test_outbox_claim_refuses_nonpositive_lease_before_querying() -> None:
    session = _Session()

    with pytest.raises(ValueError, match="lease_seconds must be positive"):
        await PostgresWebhookOutbox().claim_due(session, lease_owner="worker", lease_seconds=0)

    assert session.calls == []


@pytest.mark.asyncio
async def test_outbox_renew_refuses_nonpositive_lease_before_querying() -> None:
    session = _Session()

    with pytest.raises(ValueError, match="lease_seconds must be positive"):
        await PostgresWebhookOutbox().renew_lease(session, _delivery(), lease_seconds=0)

    assert session.calls == []


@pytest.mark.asyncio
async def test_outbox_renew_refuses_a_stale_fencing_token() -> None:
    with pytest.raises(RuntimeError, match="stale webhook outbox fencing token"):
        await PostgresWebhookOutbox().renew_lease(
            _Session(values=[None]), _delivery(), lease_seconds=1
        )


@pytest.mark.asyncio
async def test_outbox_retry_refuses_negative_delay_before_querying() -> None:
    session = _Session()

    with pytest.raises(ValueError, match="retry delay must be non-negative and finite"):
        await PostgresWebhookOutbox().mark_retry(
            session, _delivery(), delay=-1, status=None, failure=None
        )

    assert session.calls == []


def test_bounded_failure_preserves_none_and_truncates_text() -> None:
    assert _bounded_failure(None) is None
    assert _bounded_failure("x" * 300) == "x" * 256
