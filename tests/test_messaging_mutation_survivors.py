from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from wreath import messaging as messaging_module
from wreath._jobcore import dedup_key
from wreath._replay_adapters import DatabaseDouble
from wreath.messaging import Message, MessageBus, MessageEnvelope


def _bus(database: Any | None = None) -> MessageBus:
    return MessageBus(database or DatabaseDouble(), name="events")


@pytest.mark.parametrize("channel", ["", "not-valid", "x" * 64])
async def test_publish_refuses_invalid_channel_names(channel: str) -> None:
    with pytest.raises(ValueError, match="channel"):
        await _bus().publish(channel, {"id": 1})


@pytest.mark.parametrize("kind", [1, "", "é" * 128])
def test_message_envelope_refuses_invalid_kinds(kind: Any) -> None:
    with pytest.raises(ValueError, match="kind"):
        MessageEnvelope(kind, {})


@pytest.mark.parametrize("version", [True, "1", 0])
def test_message_envelope_refuses_invalid_versions(version: Any) -> None:
    with pytest.raises(ValueError, match="version"):
        MessageEnvelope("event", {}, version=version)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", 7),
        ("id", "x" * 256),
        ("correlation_id", 7),
        ("correlation_id", "x" * 256),
        ("causation_id", 7),
        ("causation_id", "x" * 256),
        ("trace_context", 7),
        ("trace_context", "x" * 1025),
    ],
)
def test_message_envelope_refuses_invalid_text_fields(field: str, value: Any) -> None:
    with pytest.raises(ValueError, match=field):
        MessageEnvelope("event", {}, **{field: value})


def test_required_envelope_text_refuses_none() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        messaging_module._check_envelope_text("id", None, 255, required=True)


def test_required_envelope_text_refuses_an_empty_string() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        messaging_module._check_envelope_text("id", "", 255, required=True)


def test_optional_envelope_text_accepts_none() -> None:
    messaging_module._check_envelope_text("correlation_id", None, 255)


def test_optional_envelope_text_accepts_an_empty_string() -> None:
    messaging_module._check_envelope_text("correlation_id", "", 255)


def test_message_envelope_refuses_an_oversized_wire_value() -> None:
    with pytest.raises(ValueError, match="maximum"):
        MessageEnvelope("event", "x" * messaging_module.MAX_ENVELOPE_BYTES)


async def test_ephemeral_publish_uses_the_supplied_transaction() -> None:
    database = DatabaseDouble()
    transaction_database = DatabaseDouble()
    transaction = await transaction_database.acquire("write")

    await _bus(database).publish("orders", {"id": 1}, tx=transaction)

    assert not database.calls
    assert len(transaction_database.calls) == 1
    assert "pg_notify" in transaction_database.calls[0][0]


async def test_durable_publish_binds_a_namespaced_deduplication_key() -> None:
    database = DatabaseDouble()
    bus = _bus(database)

    @bus.subscribe("orders", group="billing", durable=True)
    async def handler(message: Message) -> None:
        message.ack()

    await bus.publish("orders", {"id": 1}, durable=True, key="invoice-1")

    insert = next(call for call in database.calls if "INSERT INTO" in call[0])
    assert insert[1][4] == dedup_key("orders:billing", "invoice-1")


def test_dispatch_ignores_an_unknown_wire_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[object] = []
    bus = _bus()
    monkeypatch.setattr(bus, "_wake_consumers", lambda: None)
    monkeypatch.setattr(bus, "_spawn_ephemeral", lambda sub, message: spawned.append(message))

    bus._dispatch(
        SimpleNamespace(channel="unknown", payload='{"id": 1}'),
        {},
        {None: [object()]},
    )

    assert spawned == []


def test_dispatch_does_not_decode_an_empty_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[object] = []
    bus = _bus()
    monkeypatch.setattr(bus, "_wake_consumers", lambda: None)
    monkeypatch.setattr(
        messaging_module._json,
        "loads",
        lambda payload: loads.append(payload),
    )

    bus._dispatch(
        SimpleNamespace(channel="wire", payload=""),
        {"wire": "orders"},
        {"orders": []},
    )

    assert loads == []


async def test_consumer_parks_when_a_claim_finds_no_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _bus()
    stopping = asyncio.Event()
    wake = asyncio.Event()
    parks = 0

    async def claim(sub: object) -> None:
        stopping.set()
        return None

    async def park(waiter: asyncio.Event) -> None:
        nonlocal parks
        parks += 1

    monkeypatch.setattr(bus, "_claim", claim)
    monkeypatch.setattr(bus, "_park", park)

    await bus._consume(object(), stopping, wake)

    assert parks == 1


async def test_consumer_does_not_deliver_a_claim_after_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _bus()
    stopping = asyncio.Event()
    wake = asyncio.Event()
    delivered: list[Message] = []
    message = Message(channel="orders", group="billing", tenant="", payload={})

    async def claim(sub: object) -> Message:
        stopping.set()
        return message

    async def deliver(sub: object, claimed: Message) -> None:
        delivered.append(claimed)

    monkeypatch.setattr(bus, "_claim", claim)
    monkeypatch.setattr(bus, "_deliver", deliver)

    await bus._consume(object(), stopping, wake)

    assert delivered == []


async def test_handler_error_is_recorded_on_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = _bus()
    errors: list[str] = []

    @bus.subscribe("orders", group="billing", durable=True)
    async def handler(message: Message) -> None:
        raise RuntimeError("handler broke")

    async def retry(sub: object, message: Message, error: str) -> None:
        errors.append(error)

    monkeypatch.setattr(bus, "_retry", retry)

    await bus._deliver_bound(
        bus._subs[0],
        Message(channel="orders", group="billing", tenant="", payload={}),
    )

    assert errors == ["RuntimeError('handler broke')"]


async def test_explicit_nack_records_its_fallback_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = _bus()
    errors: list[str] = []

    @bus.subscribe("orders", group="billing", durable=True)
    async def handler(message: Message) -> None:
        message.nack()

    async def retry(sub: object, message: Message, error: str) -> None:
        errors.append(error)

    monkeypatch.setattr(bus, "_retry", retry)

    await bus._deliver_bound(
        bus._subs[0],
        Message(channel="orders", group="billing", tenant="", payload={}),
    )

    assert errors == ["nacked"]
