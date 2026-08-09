from __future__ import annotations

from typing import Any, cast

from benchmarks import e2e_upstream


class _Transport:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)


def test_warm_postgres_double_collapses_complete_cached_flights() -> None:
    transport = _Transport()
    protocol = e2e_upstream._BenchPostgresProtocol()
    protocol.connection_made(cast(Any, transport))
    protocol.started = True
    protocol.statements[b"wreath_1"] = e2e_upstream._PG_HOT_SQL
    packet = b"".join(
        (
            e2e_upstream._message(b"B", b"\x00wreath_1\x00"),
            e2e_upstream._message(b"E", b"\x00\x00\x00\x00\x00"),
            e2e_upstream._PG_SYNC,
        )
    )

    # The first cached flight proves which statement this connection is using
    # and enables the benchmark-only arm.
    protocol.data_received(packet)
    assert protocol.hot
    assert transport.writes == [e2e_upstream._PG_HOT_RESPONSE]

    # Coalesced flights produce one response each without Python message
    # objects; a fragmented Sync is retained until its final bytes arrive.
    protocol.data_received(packet + packet[:-2])
    assert transport.writes[-1] == e2e_upstream._PG_HOT_RESPONSE
    protocol.data_received(packet[-2:])
    assert transport.writes[-1] == e2e_upstream._PG_HOT_RESPONSE
    assert protocol.buffer == bytearray()
