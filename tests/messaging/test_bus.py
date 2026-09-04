from __future__ import annotations

import json
import math
from dataclasses import fields
from types import SimpleNamespace
from typing import Any, cast

import pytest

from wreath import _jobcore
from wreath import messaging as messaging_module
from wreath._jobcore import PayloadTooLarge
from wreath._replay_adapters import DatabaseDouble
from wreath.messaging import Message, MessageBus, MessageEnvelope


def _bus(db):
    return MessageBus(db, name="events")


async def test_publish_ephemeral_notifies():
    db = DatabaseDouble()
    bus = _bus(db)
    await bus.publish("booking_created", {"id": 1})
    call = next(c for c in db.calls if "pg_notify" in c[0])
    wire, body = call[1]
    assert wire.startswith("wm_")
    envelope = json.loads(body.removeprefix(messaging_module._EPHEMERAL_PREFIX))
    assert envelope == {"tenant": "", "payload": {"id": 1}}


async def test_plain_payloads_are_wrapped_without_changing_their_value():
    db = DatabaseDouble()
    bus = _bus(db)
    payload = {"greeting": "olá", "not_a_number": float("nan"), 7: True}
    await bus.publish("booking_created", payload)
    call = next(c for c in db.calls if "pg_notify" in c[0])
    body = call[1][1]
    envelope = json.loads(body.removeprefix(messaging_module._EPHEMERAL_PREFIX))
    assert envelope["tenant"] == ""
    assert envelope["payload"]["greeting"] == "olá"
    assert envelope["payload"]["7"] is True
    assert math.isnan(envelope["payload"]["not_a_number"])


async def test_an_envelope_is_opt_in_and_plain_payloads_keep_their_wire_shape():
    db = DatabaseDouble()
    bus = _bus(db)
    envelope = MessageEnvelope(
        "booking.created",
        {"id": 1},
        correlation_id="checkout-7",
        causation_id="command-3",
    )
    await bus.publish("booking_created", envelope)
    call = next(c for c in db.calls if "pg_notify" in c[0])
    wire = json.loads(call[1][1].removeprefix(messaging_module._EPHEMERAL_PREFIX))["payload"]
    assert wire["__wreath_message__"] == 1
    assert wire["payload"] == {"id": 1}
    delivered = Message(payload=wire, channel="booking_created", group=None, tenant="")
    assert delivered.envelope() == envelope
    assert MessageEnvelope.decode({"id": 1}) is None


def test_a_boolean_marker_does_not_turn_a_plain_payload_into_an_envelope():
    value = {
        "__wreath_message__": True,
        "kind": "booking.created",
        "version": 1,
        "id": "event-1",
        "correlation_id": None,
        "causation_id": None,
        "trace_context": None,
        "payload": {"role": "admin"},
    }

    assert MessageEnvelope.decode(value) is None


@pytest.mark.parametrize("identifier", [None, False, 0, []])
def test_an_envelope_refuses_a_falsy_non_string_identifier(identifier: object):
    with pytest.raises(ValueError, match="id must be a non-empty string"):
        MessageEnvelope("booking.created", {}, id=cast(Any, identifier))


def test_an_envelope_does_not_let_a_non_string_impersonate_the_empty_default():
    class PretendsEmpty:
        def __eq__(self, other: object) -> bool:
            return other == ""

    with pytest.raises(ValueError, match="id must be a non-empty string"):
        MessageEnvelope("booking.created", {}, id=cast(Any, PretendsEmpty()))


def test_an_envelope_serializes_once_without_changing_its_dataclass_shape(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = 0
    native_dumps = messaging_module._json.dumps

    def counting_dumps(value):
        nonlocal calls
        calls += 1
        return native_dumps(value)

    monkeypatch.setattr(messaging_module._json, "dumps", counting_dumps)
    envelope = MessageEnvelope("booking.created", ("event", 1), id="event-1")
    first = envelope.encode()
    assert envelope.encode() is first
    assert calls == 1
    assert [item.name for item in fields(envelope)] == [
        "kind",
        "payload",
        "version",
        "id",
        "correlation_id",
        "causation_id",
        "trace_context",
    ]


def test_an_envelope_does_not_cache_a_mutable_payload() -> None:
    payload = {"booking": {"status": "pending"}}
    envelope = MessageEnvelope("booking.updated", payload, id="event-1")

    payload["booking"]["status"] = "confirmed"

    assert json.loads(envelope.encode())["payload"] == {"booking": {"status": "confirmed"}}


def test_dispatch_uses_the_native_compatible_json_decoder(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = 0
    native_loads = messaging_module._json.loads

    def counting_loads(value):
        nonlocal calls
        calls += 1
        return native_loads(value)

    monkeypatch.setattr(messaging_module._json, "loads", counting_loads)
    bus = _bus(DatabaseDouble())
    bus._dispatch(
        SimpleNamespace(channel="wire", payload='{"id": 1}'),
        {"wire": "booking_created"},
        {"booking_created": []},
    )
    assert calls == 1


async def test_publish_ephemeral_oversized_rejected():
    bus = _bus(DatabaseDouble())
    with pytest.raises(PayloadTooLarge):
        await bus.publish("booking_created", {"blob": "x" * 8000})


async def test_publish_durable_refuses_a_payload_above_the_queue_bound():
    db = DatabaseDouble()
    bus = _bus(db)

    @bus.subscribe("booking_created", group="billing", durable=True)
    async def handler(message):
        pass

    with pytest.raises(PayloadTooLarge, match="durable payload"):
        await bus.publish(
            "booking_created",
            "x" * (_jobcore.MAX_DURABLE_PAYLOAD + 1),
            durable=True,
        )

    assert db.calls == []


def test_durable_subscription_requires_group():
    bus = _bus(DatabaseDouble())
    with pytest.raises(ValueError):

        @bus.subscribe("booking_created", durable=True)
        async def handler(message):
            pass


def test_subscription_refuses_a_negative_retry_budget() -> None:
    bus = _bus(DatabaseDouble())

    with pytest.raises(ValueError, match="retries must be non-negative"):
        bus.subscribe("booking_created", retries=-1)


@pytest.mark.parametrize("retries", [True, 1.5, 2**31])
def test_subscription_retry_budget_must_fit_a_postgres_integer(retries: object) -> None:
    bus = _bus(DatabaseDouble())
    with pytest.raises(ValueError, match="retries must be a non-negative integer"):
        bus.subscribe("booking_created", retries=retries)


@pytest.mark.parametrize("concurrency", [True, 1.5])
def test_subscription_concurrency_must_be_an_integer(concurrency: object) -> None:
    bus = _bus(DatabaseDouble())
    with pytest.raises(ValueError, match="concurrency must be a positive integer"):
        bus.subscribe("booking_created", concurrency=concurrency)


@pytest.mark.parametrize("option", ["durable", "require_group"])
async def test_publish_refuses_a_truthy_non_boolean_delivery_flag(option: str) -> None:
    db = DatabaseDouble()
    bus = _bus(db)

    with pytest.raises(ValueError, match=f"{option} must be a boolean"):
        await bus.publish("booking_created", {}, **{option: "false"})

    assert db.calls == []


@pytest.mark.parametrize("option", ["durable"])
def test_subscribe_refuses_a_truthy_non_boolean_delivery_flag(option: str) -> None:
    bus = _bus(DatabaseDouble())
    with pytest.raises(ValueError, match=f"{option} must be a boolean"):
        bus.subscribe("booking_created", group="billing", **{option: "false"})


@pytest.mark.parametrize("method", ["purge", "prune_groups"])
@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), True])
async def test_retention_refuses_an_invalid_age_before_database_io(
    method: str, seconds: object
) -> None:
    db = DatabaseDouble()
    bus = _bus(db)

    with pytest.raises(ValueError, match="finite"):
        argument = {"older_than": seconds} if method == "purge" else {"unseen_for": seconds}
        await getattr(bus, method)(**argument)

    assert db.calls == []


async def test_publish_durable_fans_out_per_group():
    db = DatabaseDouble()
    bus = _bus(db)

    @bus.subscribe("booking_created", group="billing", durable=True)
    async def to_billing(message):
        pass

    @bus.subscribe("booking_created", group="fulfilment", durable=True)
    async def to_fulfilment(message):
        pass

    await bus.publish("booking_created", {"id": 9}, durable=True)
    inserts = [args for sql, args in db.calls if "INSERT INTO" in sql]
    # One statement, every group: (channel, payload, tenant, group, dedup, ...)
    assert len(inserts) == 1
    groups = sorted(inserts[0][3::2])
    assert groups == ["billing", "fulfilment"]


def test_schema_sql_has_messages_table():
    sql = _bus(DatabaseDouble()).schema_sql()
    assert "CREATE TABLE IF NOT EXISTS" in sql and ".messages" in sql
    assert "messages_claim_idx" in sql
    assert "messages_dedup_idx" in sql


def test_bus_refuses_a_schema_that_is_not_a_sql_safe_identifier():
    with pytest.raises(ValueError, match="schema"):
        MessageBus(DatabaseDouble(), name="events", schema='wreath"; DROP TABLE users; --')


async def test_a_failing_reclaim_is_counted_rather_than_swallowed():
    import asyncio
    import types

    bus = _bus(DatabaseDouble())
    bus._lease = 0.001
    bus._supervisor = types.SimpleNamespace(stopping=asyncio.Event())

    attempts = {"n": 0}
    enough = asyncio.Event()

    async def always_fails(sub):
        attempts["n"] += 1
        if attempts["n"] >= 3:
            enough.set()
        raise RuntimeError("reclaim unreachable")

    bus._reclaim_expired = always_fails
    task = asyncio.create_task(bus._sweeper(object()))
    await asyncio.wait_for(enough.wait(), timeout=1.0)
    bus._supervisor.stopping.set()
    await asyncio.wait_for(task, timeout=1.0)

    # The loop survived every failure, and each one is countable.
    assert bus.sweep_errors == attempts["n"]
    assert bus.stats()["sweep_errors"] == bus.sweep_errors


async def test_the_sweeper_does_not_swallow_cancellation():
    import asyncio
    import types

    bus = _bus(DatabaseDouble())
    bus._lease = 10.0
    bus._supervisor = types.SimpleNamespace(stopping=asyncio.Event())
    started = asyncio.Event()

    async def hangs(sub):
        started.set()
        await asyncio.sleep(3600)

    bus._reclaim_expired = hangs
    task = asyncio.create_task(bus._sweeper(object()))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
