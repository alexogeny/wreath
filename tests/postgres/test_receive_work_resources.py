import struct
from typing import Any

import pytest

from wreath._native import _postgres


def feed(protocol: Any, data: bytes, fragment: int) -> None:
    offset = 0
    while offset < len(data):
        view = protocol.get_buffer(-1)
        count = min(fragment, len(view), len(data) - offset)
        view[:count] = data[offset : offset + count]
        view.release()
        protocol.buffer_updated(count)
        offset += count


@pytest.mark.parametrize("fragment", [1, 257, 65536])
async def test_unread_bytes_tracks_fragmented_copy_data(fragment: int) -> None:
    protocol = _postgres.BufferedProtocol()
    payload = bytes(range(256)) * 513
    wire = b"d" + struct.pack("!I", len(payload) + 4) + payload
    assert protocol._receive_stats()["unread_bytes"] == 0
    feed(protocol, wire[:-1], fragment)
    assert protocol._receive_stats()["unread_bytes"] == len(wire) - 1
    feed(protocol, wire[-1:], fragment)
    assert protocol._receive_stats()["unread_bytes"] == 0
    assert await protocol.read_message() == (b"d", payload)


async def test_unread_bytes_preserves_partial_next_frame_and_empty_offer() -> None:
    protocol = _postgres.BufferedProtocol()
    first = b"d" + struct.pack("!I", 7) + b"one"
    second = b"d" + struct.pack("!I", 7) + b"two"
    feed(protocol, first + second[:3], 65536)
    assert await protocol.read_message() == (b"d", b"one")
    assert protocol._receive_stats()["unread_bytes"] == 3
    protocol.get_buffer(-1).release()
    protocol.buffer_updated(0)
    assert protocol._receive_stats()["unread_bytes"] == 3
    feed(protocol, second[3:], 65536)
    assert protocol._receive_stats()["unread_bytes"] == 0
    assert await protocol.read_message() == (b"d", b"two")
