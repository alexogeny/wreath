from __future__ import annotations

import pytest

from wreath._native import _edge


class _Transport:
    def __init__(self) -> None:
        self.closed = False
        self.writes: list[bytes] = []

    def get_extra_info(self, name: str):
        return ("127.0.0.1", 1234) if name == "peername" else None

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True


def _table():
    return _edge.UpstreamTable([b"origin.test"], b"1.1 wreath", b"http")


@pytest.mark.parametrize(
    "value",
    [
        b"?1",
        b"?1;mode=fast",
        b"?1;flag",
        b"?1; mode=fast",
        b"?1;started=@123",
        b"?1;digest=:aGk:",
        b'?1;label=%"caf%c3%a9"',
    ],
)
def test_incremental_true_is_permanently_refused(value: bytes) -> None:
    table = _table()
    transport = _Transport()
    protocol = _edge.EdgeProtocol(table)
    protocol.connection_made(transport)

    protocol.data_received(
        b"POST /upload HTTP/1.1\r\nHost: example.test\r\n"
        b"Content-Length: 999999\r\nIncremental: " + value + b"\r\n\r\n"
    )

    response = b"".join(transport.writes)
    assert response.startswith(b"HTTP/1.1 501 Not Implemented\r\n")
    assert b"proxy-status: wreath;error=incremental_refused\r\n" in response.lower()
    assert transport.closed


@pytest.mark.parametrize(
    "value",
    [
        b"?0",
        b"true",
        b"?1;",
        b"?1;mode=?2",
        b'?1;mode="unterminated',
        b"?1;digest=:a=:",
        b'?1;label=%"caf%C3%A9"',
        b'?1;label=%"%ff"',
    ],
)
def test_false_or_invalid_incremental_values_are_ignored(value: bytes) -> None:
    table = _table()
    transport = _Transport()
    protocol = _edge.EdgeProtocol(table)
    protocol.connection_made(transport)

    protocol.data_received(
        b"GET / HTTP/1.1\r\nHost: example.test\r\nIncremental: " + value + b"\r\n\r\n"
    )

    response = b"".join(transport.writes)
    assert not response.startswith(b"HTTP/1.1 501 Not Implemented\r\n")


def test_malformed_origin_response_reports_http_protocol_error() -> None:
    table = _table()
    client_transport = _Transport()
    client = _edge.EdgeProtocol(table)
    client.connection_made(client_transport)
    upstream_transport = _Transport()
    upstream = _edge.UpstreamConnection(table, 0)
    upstream.connection_made(upstream_transport)

    client.data_received(b"GET / HTTP/1.1\r\nHost: example.test\r\n\r\n")
    assert upstream_transport.writes
    upstream.data_received(b"not-http\r\n\r\n")

    response = b"".join(client_transport.writes)
    assert response.startswith(b"HTTP/1.1 502 Bad Gateway\r\n")
    assert b"proxy-status: wreath;error=http_protocol_error\r\n" in response.lower()


def test_no_live_origin_reports_destination_unavailable() -> None:
    table = _table()
    transport = _Transport()
    protocol = _edge.EdgeProtocol(table)
    protocol.connection_made(transport)

    protocol.data_received(b"GET / HTTP/1.1\r\nHost: example.test\r\n\r\n")

    response = b"".join(transport.writes)
    assert response.startswith(b"HTTP/1.1 502 Bad Gateway\r\n")
    assert b"proxy-status: wreath;error=destination_unavailable\r\n" in response.lower()


def test_origin_disconnect_before_response_reports_connection_terminated() -> None:
    table = _table()
    client_transport = _Transport()
    client = _edge.EdgeProtocol(table)
    client.connection_made(client_transport)
    upstream_transport = _Transport()
    upstream = _edge.UpstreamConnection(table, 0)
    upstream.connection_made(upstream_transport)

    client.data_received(b"GET / HTTP/1.1\r\nHost: example.test\r\n\r\n")
    upstream.connection_lost(None)

    response = b"".join(client_transport.writes)
    assert response.startswith(b"HTTP/1.1 502 Bad Gateway\r\n")
    assert b"proxy-status: wreath;error=connection_terminated\r\n" in response.lower()
