from __future__ import annotations

import pytest

from wreath._native import _client, _core


@pytest.mark.skipif(_client is None, reason="native client unavailable")
def test_incremental_http_client_protocol_retains_partial_head() -> None:
    protocol = _client.Http1ClientProtocol(max_header_bytes=128)

    assert protocol.feed_data(b"HTTP/1.1 200 OK\r\nContent-Len") is None
    assert protocol.pending.endswith(b"Content-Len")

    parsed = protocol.feed_data(b"gth: 3\r\n\r\nabc")

    assert parsed[:3] == (1, 200, b"OK")
    assert parsed[3] == [(b"content-length", b"3")]
    assert protocol.pending == b"abc"

    protocol = _client.Http1ClientProtocol(max_header_bytes=64)
    parsed = protocol.feed_data(b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 200)
    assert parsed[1] == 200
    assert protocol.pending == b"x" * 200


@pytest.mark.skipif(_client is None, reason="native client unavailable")
def test_incremental_http_client_protocol_bounds_header_buffer() -> None:
    protocol = _client.Http1ClientProtocol(max_header_bytes=8)

    with pytest.raises(ValueError, match="exceed"):
        protocol.feed_data(b"HTTP/1.1 200 OK")


@pytest.mark.skipif(_core is None, reason="native core unavailable")
def test_native_scheduler_is_bounded_and_orders_deadlines() -> None:
    scheduler = _core.NativeScheduler(capacity=3)
    scheduler.schedule(3.0, "third")
    scheduler.schedule(1.0, "first")
    scheduler.schedule(2.0, "second")

    with pytest.raises(OverflowError, match="capacity"):
        scheduler.schedule(4.0, "overflow")

    assert scheduler.pop_due(2.5, 1) == ["first"]
    assert scheduler.pop_due(2.5) == ["second"]
    assert scheduler.size == 1
    assert scheduler.pop_due(3.0) == ["third"]
