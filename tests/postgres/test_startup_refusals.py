from __future__ import annotations

import asyncio
import struct

import pytest

from wreath._pure import postgres


class _Writer:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


@pytest.mark.asyncio
async def test_backend_key_data_must_carry_both_protocol_integers() -> None:
    reader = asyncio.StreamReader()
    payload = b"short"
    reader.feed_data(b"K" + struct.pack("!I", len(payload) + 4) + payload)
    writer = _Writer()
    info = postgres._parse_dsn("postgresql://wreath@127.0.0.1/wreath_test")

    with pytest.raises(postgres.ProtocolError, match="invalid BackendKeyData"):
        await postgres._authenticate(reader, writer, info)

    assert writer.writes, "the startup packet is sent before the backend can answer"
