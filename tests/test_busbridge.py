from __future__ import annotations

import asyncio
from typing import Any

import pytest

from wreath._busbridge import BusBridge

pytestmark = pytest.mark.asyncio


class FakeBus:
    """Records subscriptions and published payloads; can wire N workers.

    ``peers`` models the one property that matters: an ephemeral ``NOTIFY``
    reaches every listener on the channel, including the process that sent it.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.handlers: list[tuple[str, Any]] = []
        self.published: list[tuple[str, Any]] = []
        self.peers: list[FakeBus] = []
        self.fail = fail

    def subscribe(self, channel: str, **kwargs: Any):
        def register(handler):
            self.handlers.append((channel, handler))
            return handler

        return register

    async def publish(self, channel: str, payload: Any, **kwargs: Any) -> None:
        if self.fail:
            raise ConnectionError("the bus is down")
        self.published.append((channel, payload))
        for bus in (self, *self.peers):
            for subscribed, handler in bus.handlers:
                if subscribed == channel:
                    await handler(_Message(channel, payload))


class _Message:
    def __init__(self, channel: str, payload: Any) -> None:
        self.channel = channel
        self.payload = payload


def _collecting(bus: Any = None, *, channel: str = "test_channel"):
    """A bridge that records every payload it accepts as foreign."""
    accepted: list[dict] = []

    async def apply(payload: dict) -> None:
        accepted.append(payload)

    return BusBridge(bus, channel=channel, apply=apply), accepted


@pytest.mark.parametrize("max_inflight", [0, float("nan"), float("inf"), True, 1.5])
async def test_a_bridge_refuses_an_invalid_publish_ceiling(max_inflight: Any) -> None:
    async def apply(payload: dict) -> None:
        return None

    with pytest.raises(ValueError, match="max_inflight must be a positive integer"):
        BusBridge(FakeBus(), channel="events", apply=apply, max_inflight=max_inflight)


async def test_it_subscribes_once_at_construction() -> None:
    bus = FakeBus()
    _collecting(bus, channel="shard_a")

    assert len(bus.handlers) == 1
    assert bus.handlers[0][0] == "shard_a"


async def test_a_bridge_without_a_bus_subscribes_to_nothing() -> None:
    bridge, accepted = _collecting()
    assert bridge.attached is False
    assert accepted == []


async def test_publishing_without_a_bus_is_a_no_op() -> None:
    bridge, _accepted = _collecting()
    await bridge.publish({"hello": "world"})  # must not raise
    bridge.publish_soon({"hello": "world"})
    await asyncio.sleep(0)


async def test_every_publish_carries_the_origin() -> None:
    bus = FakeBus()
    bridge, _accepted = _collecting(bus)

    await bridge.publish({"kind": "awaited"})
    bridge.publish_soon({"kind": "deferred"})
    await asyncio.sleep(0)

    assert [payload["origin"] for _channel, payload in bus.published] == [
        bridge.origin,
        bridge.origin,
    ]


async def test_a_caller_cannot_shadow_the_origin() -> None:
    bus = FakeBus()
    bridge, _accepted = _collecting(bus)

    await bridge.publish({"origin": "spoofed"})

    ((_channel, payload),) = bus.published
    assert payload["origin"] == bridge.origin


async def test_two_bridges_have_different_origins() -> None:
    first, _a = _collecting(FakeBus())
    second, _b = _collecting(FakeBus())
    assert first.origin != second.origin


async def test_the_origin_is_not_the_object_address() -> None:
    bridge, _accepted = _collecting(FakeBus())

    address = f"{id(bridge):x}"
    assert bridge.origin != address
    assert address not in bridge.origin
    assert len(bridge.origin) == 16
    int(bridge.origin, 16)  # hex, so it survives any payload encoding


async def test_origins_do_not_repeat_across_short_lived_bridges() -> None:
    origins = {_collecting()[0].origin for _ in range(500)}
    assert len(origins) == 500


async def test_a_worker_ignores_the_echo_of_its_own_publish() -> None:
    bus = FakeBus()
    bridge, accepted = _collecting(bus)

    await bridge.publish({"n": 1})  # FakeBus delivers back to this listener

    assert bus.published  # it did go out
    assert accepted == []  # ... and did not come back in


async def test_another_workers_message_is_accepted() -> None:
    bus = FakeBus()
    bridge, accepted = _collecting(bus)

    _channel, handler = bus.handlers[0]
    await handler(_Message("test_channel", {"n": 1, "origin": "worker-a"}))

    assert accepted == [{"n": 1, "origin": "worker-a"}]
    assert bridge.untagged_applied == 0


async def test_a_payload_with_no_origin_is_treated_as_foreign() -> None:
    bus = FakeBus()
    _bridge, accepted = _collecting(bus)

    _channel, handler = bus.handlers[0]
    await handler(_Message("test_channel", {"n": 1}))

    assert accepted == [{"n": 1}]


@pytest.mark.parametrize("origin", ["", 0, False, 7])
async def test_an_invalid_origin_is_delivered_but_counted(origin: Any) -> None:
    bus = FakeBus()
    bridge, accepted = _collecting(bus)

    _channel, handler = bus.handlers[0]
    await handler(_Message("test_channel", {"n": 1, "origin": origin}))

    assert accepted == [{"n": 1, "origin": origin}]
    assert bridge.untagged_applied == 1


@pytest.mark.parametrize("payload", ["not-a-dict", None, 7, ["models"], b"bytes"])
async def test_a_payload_that_is_not_a_mapping_is_dropped(payload: Any) -> None:
    bus = FakeBus()
    _bridge, accepted = _collecting(bus)

    _channel, handler = bus.handlers[0]
    await handler(_Message("test_channel", payload))

    assert accepted == []


async def test_an_accepted_message_is_never_published_onward() -> None:
    bus = FakeBus()
    _bridge, accepted = _collecting(bus)

    _channel, handler = bus.handlers[0]
    await handler(_Message("test_channel", {"n": 1, "origin": "worker-a"}))
    for _ in range(5):
        await asyncio.sleep(0)

    assert accepted == [{"n": 1, "origin": "worker-a"}]
    assert bus.published == []


async def test_publish_soon_does_not_make_the_caller_wait() -> None:
    bus = FakeBus()
    bridge, _accepted = _collecting(bus)

    bridge.publish_soon({"n": 1})
    assert bus.published == []  # not yet: it is a task, not inline

    await asyncio.sleep(0)
    assert len(bus.published) == 1


async def test_publish_soon_survives_a_bus_that_is_down() -> None:
    bridge, _accepted = _collecting(FakeBus(fail=True))

    bridge.publish_soon({"n": 1})  # must not raise
    for _ in range(5):
        await asyncio.sleep(0)


async def test_publish_soon_outside_the_event_loop_is_not_carried() -> None:
    bus = FakeBus()
    bridge, _accepted = _collecting(bus)

    await asyncio.to_thread(bridge.publish_soon, {"n": 1})
    assert bus.published == []


async def test_publish_lets_a_bus_failure_reach_the_caller() -> None:
    bridge, _accepted = _collecting(FakeBus(fail=True))

    with pytest.raises(ConnectionError):
        await bridge.publish({"n": 1})


async def test_an_inflight_publish_keeps_its_own_reference() -> None:
    bus = FakeBus()
    bridge, _accepted = _collecting(bus)

    bridge.publish_soon({"n": 1})
    assert bridge.inflight == 1  # held from the moment it is scheduled

    # Several yields, not one: the task finishes in its first step, but the
    # done-callback that releases the reference is itself a `call_soon`.
    for _ in range(5):
        await asyncio.sleep(0)

    assert bridge.inflight == 0  # and released once it lands
    assert len(bus.published) == 1


async def test_bridges_on_separate_channels_do_not_cross_talk() -> None:
    bus_one, bus_two = FakeBus(), FakeBus()
    bus_one.peers.append(bus_two)
    bridge_one, _a = _collecting(bus_one, channel="shard_a")
    _bridge_two, accepted_two = _collecting(bus_two, channel="shard_b")

    await bridge_one.publish({"n": 1})

    assert accepted_two == []
