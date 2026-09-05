import struct

import pytest

from wreath import _pgdriver


def test_cold_parse_does_not_pack_zero_once_per_parameter(monkeypatch):
    calls = []
    original = struct.pack

    def counted(fmt, *values):
        if fmt == "!I" and values == (0,):
            calls.append(values)
        return original(fmt, *values)

    monkeypatch.setattr(_pgdriver.struct, "pack", counted)
    packet = _pgdriver._build_cold_query_packet(
        b"statement", "SELECT $1", (None,) * 1024, (25,) * 1024, "fetch"
    )
    assert packet.startswith(b"P")
    assert calls == []


@pytest.mark.parametrize("count", [0, 1, 32, 1024])
@pytest.mark.parametrize("mode", ["execute", "fetch", "fetchrow", "fetchval"])
@pytest.mark.parametrize("binary_results", [False, True])
def test_cold_packet_parse_oid_vector_and_message_frames(count, mode, binary_results):
    packet = _pgdriver._build_cold_query_packet(
        b"statement",
        "SELECT $1",
        (None,) * count,
        (25,) * count,
        mode,
        binary_results=binary_results,
    )
    messages = []
    offset = 0
    while offset < len(packet):
        length = int.from_bytes(packet[offset + 1 : offset + 5], "big")
        messages.append((packet[offset : offset + 1], packet[offset + 5 : offset + 1 + length]))
        offset += length + 1
    assert offset == len(packet)
    assert b"".join(kind for kind, _ in messages) == (b"PDBES" if mode == "execute" else b"PDBDES")
    expected = b"statement\0SELECT $1\0" + count.to_bytes(2, "big") + bytes(4 * count)
    assert messages[0][1] == expected
    bind = messages[2][1]
    suffix = b"\0\1\0\1" if binary_results and mode != "execute" else b"\0\0"
    assert bind == b"\0statement\0\0\0" + count.to_bytes(2, "big") + b"\xff" * (4 * count) + suffix


def test_cold_packet_keeps_parameter_bounds_and_mode_errors():
    with pytest.raises(struct.error):
        _pgdriver._build_cold_query_packet(b"s", "SELECT 1", (None,) * 65536, (), "fetch")
    with pytest.raises(_pgdriver.InterfaceError, match="argument count"):
        _pgdriver._build_cold_query_packet(b"s", "SELECT 1", (None,), (), "fetch")
    with pytest.raises(ValueError, match="unknown PostgreSQL result mode"):
        _pgdriver._build_cold_query_packet(b"s", "SELECT 1", (), (), "unknown")
