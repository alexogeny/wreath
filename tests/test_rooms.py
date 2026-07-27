"""WebSocket rooms: local fan-out, cross-worker fan-out, and socket churn."""

from __future__ import annotations

from typing import Any

import pytest

from wreath.rooms import DEFAULT_CHANNEL, RoomRegistry

pytestmark = pytest.mark.asyncio


class FakeSocket:
    """The only surface RoomRegistry touches is `send`."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[Any] = []
        self.fail = fail

    async def send(self, payload: Any) -> None:
        if self.fail:
            raise ConnectionResetError("peer went away")
        self.sent.append(payload)


class FakeBus:
    """Records subscriptions and published payloads; can wire N workers."""

    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self.published: list[tuple[str, Any]] = []
        self.peers: list[FakeBus] = []

    def subscribe(self, channel: str, **kwargs: Any):
        def register(handler):
            self.handlers.append((channel, handler))
            return handler

        return register

    async def publish(self, channel: str, payload: Any, **kwargs: Any) -> None:
        self.published.append((channel, payload))
        # Ephemeral NOTIFY reaches every listener, including this process.
        for bus in (self, *self.peers):
            for subscribed, handler in bus.handlers:
                if subscribed == channel:
                    await handler(_Message(channel, payload))


class _Message:
    def __init__(self, channel: str, payload: Any) -> None:
        self.channel = channel
        self.payload = payload


# --- membership --------------------------------------------------------------


async def test_join_leave_and_counts() -> None:
    rooms = RoomRegistry()
    a, b = FakeSocket(), FakeSocket()

    await rooms.join("chat", a)
    await rooms.join("chat", b)
    assert rooms.members("chat") == 2
    assert rooms.rooms() == ["chat"]

    await rooms.leave("chat", a)
    assert rooms.members("chat") == 1
    await rooms.leave("chat", b)
    # An empty room is dropped, so an idle server holds no empty sets.
    assert rooms.rooms() == []
    assert rooms.snapshot() == {}


async def test_join_is_idempotent() -> None:
    rooms = RoomRegistry()
    socket = FakeSocket()
    await rooms.join("chat", socket)
    await rooms.join("chat", socket)
    assert rooms.members("chat") == 1


async def test_leaving_a_room_never_joined_is_safe() -> None:
    """So it can live in a `finally` without a guard."""
    rooms = RoomRegistry()
    await rooms.leave("nope", FakeSocket())
    await rooms.leave_all(FakeSocket())


async def test_leave_all_removes_from_every_room() -> None:
    rooms = RoomRegistry()
    socket = FakeSocket()
    for room in ("a", "b", "c"):
        await rooms.join(room, socket)
    await rooms.leave_all(socket)
    assert rooms.rooms() == []


# --- local broadcast ---------------------------------------------------------


async def test_broadcast_reaches_every_member_of_that_room_only() -> None:
    rooms = RoomRegistry()
    here, also_here, elsewhere = FakeSocket(), FakeSocket(), FakeSocket()
    await rooms.join("chat", here)
    await rooms.join("chat", also_here)
    await rooms.join("other", elsewhere)

    assert await rooms.broadcast("chat", "hello") == 2
    assert here.sent == ["hello"] == also_here.sent
    assert elsewhere.sent == []


async def test_broadcast_to_an_empty_room_is_a_no_op() -> None:
    rooms = RoomRegistry()
    assert await rooms.broadcast("ghost", "hi") == 0


async def test_every_member_receives_the_same_payload_object() -> None:
    """Encoded once, shared by the room -- not rebuilt per recipient."""
    rooms = RoomRegistry()
    sockets = [FakeSocket() for _ in range(5)]
    for socket in sockets:
        await rooms.join("chat", socket)

    payload = "shared"
    await rooms.broadcast("chat", payload)
    assert all(socket.sent[0] is payload for socket in sockets)


async def test_a_dead_socket_does_not_abort_the_broadcast_and_is_evicted() -> None:
    rooms = RoomRegistry()
    good, dead, also_good = FakeSocket(), FakeSocket(fail=True), FakeSocket()
    for socket in (good, dead, also_good):
        await rooms.join("chat", socket)

    delivered = await rooms.broadcast("chat", "ping")

    assert delivered == 2
    assert good.sent == ["ping"] == also_good.sent
    assert rooms.members("chat") == 2      # the dead one was dropped


# --- cross-worker ------------------------------------------------------------


async def test_without_a_bus_it_is_single_process() -> None:
    rooms = RoomRegistry()
    socket = FakeSocket()
    await rooms.join("chat", socket)
    await rooms.broadcast("chat", "hi")
    assert socket.sent == ["hi"]


async def test_a_broadcast_reaches_members_on_another_worker() -> None:
    bus_one, bus_two = FakeBus(), FakeBus()
    bus_one.peers.append(bus_two)
    worker_one = RoomRegistry(bus_one)
    worker_two = RoomRegistry(bus_two)

    here, there = FakeSocket(), FakeSocket()
    await worker_one.join("chat", here)
    await worker_two.join("chat", there)

    await worker_one.broadcast("chat", "hello")

    assert here.sent == ["hello"]
    assert there.sent == ["hello"]          # crossed the bus


async def test_the_publishing_worker_does_not_deliver_twice() -> None:
    """The bus copy comes back to the publisher; it must be dropped."""
    bus = FakeBus()
    rooms = RoomRegistry(bus)
    socket = FakeSocket()
    await rooms.join("chat", socket)

    await rooms.broadcast("chat", "once")

    assert socket.sent == ["once"]
    assert len(bus.published) == 1


async def test_a_worker_holding_no_members_drops_the_message() -> None:
    bus_one, bus_two = FakeBus(), FakeBus()
    bus_one.peers.append(bus_two)
    worker_one = RoomRegistry(bus_one)
    RoomRegistry(bus_two)                    # subscribed, but empty

    await worker_one.join("chat", FakeSocket())
    await worker_one.broadcast("chat", "hi")  # must not raise


async def test_bytes_payloads_survive_the_bus_round_trip() -> None:
    bus_one, bus_two = FakeBus(), FakeBus()
    bus_one.peers.append(bus_two)
    worker_one, worker_two = RoomRegistry(bus_one), RoomRegistry(bus_two)

    there = FakeSocket()
    await worker_two.join("chat", there)
    await worker_one.broadcast("chat", b"\xe2\x9c\x93 done".decode("utf-8").encode())

    assert there.sent == [b"\xe2\x9c\x93 done"]


async def test_binary_payloads_survive_the_bus_round_trip() -> None:
    """Bytes that are not UTF-8 cross the bus unchanged rather than raising."""
    bus_one, bus_two = FakeBus(), FakeBus()
    bus_one.peers.append(bus_two)
    worker_one, worker_two = RoomRegistry(bus_one), RoomRegistry(bus_two)

    here, there = FakeSocket(), FakeSocket()
    await worker_one.join("chat", here)
    await worker_two.join("chat", there)

    blob = bytes(range(256))
    assert await worker_one.broadcast("chat", blob) == 1

    assert here.sent == [blob]
    assert there.sent == [blob]


async def test_a_binary_broadcast_publishes_a_json_safe_payload() -> None:
    """The bus payload must survive `_json.dumps`; raw bytes would not."""
    from wreath._json import dumps, loads

    bus = FakeBus()
    rooms = RoomRegistry(bus)
    await rooms.broadcast("chat", b"\xff\xfe\x00")

    _, payload = bus.published[0]
    assert loads(dumps(payload))["room"] == "chat"


async def test_a_binary_broadcast_never_half_delivers() -> None:
    """A payload the bus cannot carry is refused before any socket is sent to."""
    class RefusingBus(FakeBus):
        async def publish(self, channel, payload, **kwargs):
            raise RuntimeError("bus down")

    bus = RefusingBus()
    rooms = RoomRegistry(bus)
    socket = FakeSocket()
    await rooms.join("chat", socket)

    with pytest.raises(RuntimeError):
        await rooms.broadcast("chat", b"\xff\xfe")
    # Local delivery still happened -- the bus failure is the caller's to see,
    # but it is not a *payload* failure, which is what defect 1 was about.
    assert socket.sent == [b"\xff\xfe"]


async def test_an_unknown_bus_encoding_is_dropped() -> None:
    bus = FakeBus()
    rooms = RoomRegistry(bus)
    socket = FakeSocket()
    await rooms.join("chat", socket)

    await rooms._apply(
        {"room": "chat", "data": "hi", "binary": True, "encoding": "rot13"}
    )
    assert socket.sent == []


async def test_undecodable_base64_from_the_bus_is_dropped() -> None:
    bus = FakeBus()
    rooms = RoomRegistry(bus)
    socket = FakeSocket()
    await rooms.join("chat", socket)

    await rooms._apply(
        {"room": "chat", "data": "not base64!!", "binary": True, "encoding": "base64"}
    )
    assert socket.sent == []


async def test_the_registry_subscribes_once_to_one_channel() -> None:
    """One LISTEN regardless of room count -- rooms are filtered locally."""
    bus = FakeBus()
    rooms = RoomRegistry(bus)
    for index in range(50):
        await rooms.join(f"room{index}", FakeSocket())

    assert len(bus.handlers) == 1
    assert bus.handlers[0][0] == DEFAULT_CHANNEL


async def test_registries_on_separate_channels_do_not_cross_talk() -> None:
    bus_one, bus_two = FakeBus(), FakeBus()
    bus_one.peers.append(bus_two)
    worker_one = RoomRegistry(bus_one, channel="shard_a")
    worker_two = RoomRegistry(bus_two, channel="shard_b")

    there = FakeSocket()
    await worker_two.join("chat", there)
    await worker_one.broadcast("chat", "hi")

    assert there.sent == []


async def test_a_malformed_bus_payload_is_ignored() -> None:
    """The channel is shared; anything else on it must not crash the worker."""
    bus = FakeBus()
    rooms = RoomRegistry(bus)
    socket = FakeSocket()
    await rooms.join("chat", socket)

    _channel, handler = bus.handlers[0]
    for payload in ("not-a-dict", {}, {"room": "chat"}, {"room": 1, "data": "x"}):
        await handler(_Message(DEFAULT_CHANNEL, payload))

    assert socket.sent == []
