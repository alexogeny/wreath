from __future__ import annotations

import asyncio
import struct
from collections import deque

import pytest

from wreath._pgdriver import (
    Connection,
    Notification,
    _parse_notification,
    _validate_channel,
)


class _TinyRing(Connection):
    notify_ring_capacity = 2


def _new_conn(cls: type[Connection] = Connection) -> Connection:
    conn = cls.__new__(cls)
    conn._notifications = deque()
    conn._notify_event = asyncio.Event()
    conn._notifications_dropped = 0
    conn._closed = False
    return conn


def _frame(pid: int, channel: str, payload: str) -> bytes:
    return struct.pack(">i", pid) + channel.encode() + b"\x00" + payload.encode() + b"\x00"


def test_parse_notification_valid() -> None:
    assert _parse_notification(_frame(4321, "orders", '{"id": 1}')) == Notification(
        4321, "orders", '{"id": 1}'
    )


def test_parse_notification_empty_payload() -> None:
    # A NOTIFY with no payload still has the trailing terminators.
    assert _parse_notification(_frame(7, "chan", "")) == Notification(7, "chan", "")


@pytest.mark.parametrize("payload", [b"", b"\x00\x00\x00", struct.pack(">i", 1)])
def test_parse_notification_malformed_returns_none(payload: bytes) -> None:
    # Too short, or missing the channel terminator: dropped, never raised.
    assert _parse_notification(payload) is None


def test_validate_channel_accepts_identifier() -> None:
    assert _validate_channel("order_events$1") == "order_events$1"


@pytest.mark.parametrize(
    "channel",
    ["", "has space", 'has"quote', "semi;colon", "x" * 64, "drop\x00table"],
)
def test_validate_channel_rejects(channel: str) -> None:
    with pytest.raises(ValueError):
        _validate_channel(channel)


def test_enqueue_and_iterate_drains_without_awaiting() -> None:
    async def scenario() -> list[Notification]:
        conn = _new_conn()
        conn._enqueue_notification(_frame(1, "ch", "a"))
        conn._enqueue_notification(_frame(2, "ch", "b"))
        iterator = conn.notifications()
        first = await iterator.__anext__()
        second = await iterator.__anext__()
        return [first, second]

    got = asyncio.run(scenario())
    assert [n.payload for n in got] == ["a", "b"]
    assert [n.pid for n in got] == [1, 2]


def test_ring_overflow_drops_oldest_and_counts() -> None:
    async def scenario() -> tuple[list[str], int]:
        conn = _new_conn(cls=_TinyRing)
        for index in range(4):
            conn._enqueue_notification(_frame(1, "ch", str(index)))
        return [n.payload for n in conn._notifications], conn.dropped_notifications

    remaining, dropped = asyncio.run(scenario())
    assert remaining == ["2", "3"]  # capacity 2, oldest two evicted
    assert dropped == 2


def test_malformed_frame_is_not_enqueued() -> None:
    async def scenario() -> int:
        conn = _new_conn()
        conn._enqueue_notification(b"\x00\x00")  # too short -> parsed to None
        return len(conn._notifications)

    assert asyncio.run(scenario()) == 0


def test_close_ends_a_blocked_iterator() -> None:
    async def scenario() -> None:
        conn = _new_conn()
        iterator = conn.notifications()
        pending = asyncio.ensure_future(iterator.__anext__())
        await asyncio.sleep(0)  # let it park on the event
        assert not pending.done()
        conn._closed = True
        conn._notify_event.set()  # mimic close() waking waiters
        with pytest.raises(StopAsyncIteration):
            await pending

    asyncio.run(scenario())


def test_notify_wakes_a_blocked_iterator() -> None:
    async def scenario() -> Notification:
        conn = _new_conn()
        iterator = conn.notifications()
        pending = asyncio.ensure_future(iterator.__anext__())
        await asyncio.sleep(0)  # park on the event
        conn._enqueue_notification(_frame(9, "ch", "late"))
        return await pending

    got = asyncio.run(scenario())
    assert got == Notification(9, "ch", "late")
