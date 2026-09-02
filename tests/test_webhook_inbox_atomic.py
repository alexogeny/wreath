from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from wreath.webhooks import PostgresWebhookInbox, WebhookEnvelope


def _envelope() -> WebhookEnvelope:
    return WebhookEnvelope(
        id="evt-1",
        type="chat.command",
        version="1",
        timestamp=datetime(2026, 9, 2, tzinfo=UTC),
        content_type="application/json",
        body=b'{"type":2}',
    )


class _Result:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def fetchrow(self) -> Any:
        return self._session.rows.pop(0)

    async def fetchval(self) -> Any:
        return self._session.values.pop(0)


class _Transaction(AbstractAsyncContextManager[None]):
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def __aenter__(self) -> None:
        self._events.append("begin")

    async def __aexit__(self, error_type: Any, _error: Any, _traceback: Any) -> None:
        self._events.append(("end", error_type))


@dataclass
class _Session:
    rows: list[Any]
    values: list[Any] = field(default_factory=list)
    events: list[Any] = field(default_factory=list)

    def begin(self) -> _Transaction:
        return _Transaction(self.events)

    def raw(self, sql: str, *parameters: Any) -> _Result:
        self.events.append(("sql", sql, parameters))
        return _Result(self)


class _SessionScope(AbstractAsyncContextManager[_Session]):
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def __aenter__(self) -> _Session:
        self._session.events.append("session-open")
        return self._session

    async def __aexit__(self, error_type: Any, _error: Any, _traceback: Any) -> None:
        self._session.events.append(("session-close", error_type))


def _inbox(session: _Session) -> PostgresWebhookInbox:
    return PostgresWebhookInbox(
        session_factory=lambda: _SessionScope(session),
        lease_owner="chat-worker-1",
        lease_seconds=30,
    )


def test_transactional_fact_reports_whether_the_inbox_owns_sessions() -> None:
    assert PostgresWebhookInbox().transactional is False
    assert _inbox(_Session(rows=[])).transactional is True


async def test_claim_and_enqueue_commits_claim_job_and_completion_in_one_transaction() -> None:
    session = _Session(rows=[{"fencing_token": 7}], values=[1])
    calls: list[tuple[Any, WebhookEnvelope]] = []
    envelope = _envelope()

    async def enqueue(*, transaction: Any) -> int:
        calls.append((transaction, envelope))
        session.events.append("enqueue")
        return 41

    accepted = await _inbox(session).claim_and_enqueue(
        source="discord",
        envelope=envelope,
        enqueue=enqueue,
        result_status=202,
    )

    assert accepted is True
    assert calls == [(session, envelope)]
    assert session.events[0:2] == ["session-open", "begin"]
    assert "INSERT INTO wreath_webhook_inbox" in session.events[2][1]
    assert session.events[3] == "enqueue"
    assert "SET state='completed'" in session.events[4][1]
    assert session.events[4][2] == ("discord", "evt-1", 7, 202)
    assert session.events[-2:] == [("end", None), ("session-close", None)]


@pytest.mark.parametrize("state", ["completed", "processing", "failed"])
async def test_claim_and_enqueue_does_not_enqueue_an_existing_delivery(state: str) -> None:
    session = _Session(rows=[None, {"state": state, "fencing_token": 3, "result_status": 202}])
    enqueued = False

    async def enqueue(*, transaction: Any) -> None:
        nonlocal enqueued
        enqueued = True

    accepted = await _inbox(session).claim_and_enqueue(
        source="discord",
        envelope=_envelope(),
        enqueue=enqueue,
    )

    assert accepted is False
    assert enqueued is False
    assert not any(
        event[0] == "sql" and "SET state='completed'" in event[1]
        for event in session.events
        if isinstance(event, tuple)
    )


@pytest.mark.parametrize("error", [RuntimeError("queue unavailable"), asyncio.CancelledError()])
async def test_claim_and_enqueue_preserves_enqueue_failure_and_rolls_back(
    error: BaseException,
) -> None:
    session = _Session(rows=[{"fencing_token": 2}])

    async def enqueue(*, transaction: Any) -> None:
        raise error

    with pytest.raises(type(error), match=str(error) or None):
        await _inbox(session).claim_and_enqueue(
            source="discord",
            envelope=_envelope(),
            enqueue=enqueue,
        )

    assert session.events[-2:] == [
        ("end", type(error)),
        ("session-close", type(error)),
    ]
    assert not any(
        event[0] == "sql" and "SET state='completed'" in event[1]
        for event in session.events
        if isinstance(event, tuple)
    )


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(
            {"session_factory": object(), "lease_owner": "worker", "lease_seconds": 30},
            id="noncallable-factory",
        ),
        pytest.param(
            {"session_factory": lambda: None, "lease_owner": "", "lease_seconds": 30},
            id="empty-owner",
        ),
        pytest.param(
            {"session_factory": lambda: None, "lease_owner": 3, "lease_seconds": 30},
            id="nontext-owner",
        ),
        pytest.param(
            {"session_factory": lambda: None, "lease_owner": "worker", "lease_seconds": 0},
            id="zero-lease",
        ),
        pytest.param(
            {"session_factory": lambda: None, "lease_owner": "worker", "lease_seconds": -1},
            id="negative-lease",
        ),
        pytest.param(
            {"session_factory": lambda: None, "lease_owner": "worker", "lease_seconds": True},
            id="boolean-lease",
        ),
        pytest.param(
            {"session_factory": lambda: None, "lease_owner": "worker", "lease_seconds": "30"},
            id="nonnumber-lease",
        ),
        pytest.param({"lease_owner": "worker", "lease_seconds": 30}, id="missing-factory"),
        pytest.param({"lease_owner": "worker"}, id="owner-without-factory"),
        pytest.param({"lease_seconds": 30}, id="lease-without-factory"),
        pytest.param(
            {"session_factory": lambda: None, "lease_owner": "worker"}, id="missing-lease"
        ),
        pytest.param({"session_factory": lambda: None, "lease_seconds": 30}, id="missing-owner"),
    ],
)
def test_atomic_inbox_configuration_is_validated(options: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError), match="session_factory|lease"):
        PostgresWebhookInbox(**options)


async def test_claim_and_enqueue_requires_configured_transaction_ownership() -> None:
    async def enqueue(*, transaction: Any) -> None:
        raise AssertionError(f"an unconfigured inbox enqueued with {transaction!r}")

    with pytest.raises(RuntimeError, match="session_factory"):
        await PostgresWebhookInbox().claim_and_enqueue(
            source="discord",
            envelope=_envelope(),
            enqueue=enqueue,
        )


@pytest.mark.parametrize(
    ("changes", "error", "message"),
    [
        pytest.param({"source": ""}, ValueError, "source", id="empty-source"),
        pytest.param({"source": 3}, ValueError, "source", id="nontext-source"),
        pytest.param({"envelope": object()}, TypeError, "WebhookEnvelope", id="wrong-envelope"),
        pytest.param({"enqueue": object()}, TypeError, "enqueue", id="noncallable-enqueue"),
        pytest.param({"result_status": True}, TypeError, "result_status", id="boolean-status"),
        pytest.param({"result_status": "202"}, TypeError, "result_status", id="noninteger-status"),
    ],
)
async def test_claim_and_enqueue_validates_its_public_inputs(
    changes: dict[str, Any], error: type[Exception], message: str
) -> None:
    session = _Session(rows=[])

    async def enqueue(*, transaction: Any) -> None:
        raise AssertionError(f"invalid input enqueued with {transaction!r}")

    options: dict[str, Any] = {
        "source": "discord",
        "envelope": _envelope(),
        "enqueue": enqueue,
        "result_status": 202,
    }
    options.update(changes)
    with pytest.raises(error, match=message):
        await _inbox(session).claim_and_enqueue(**options)
    assert session.events == []
