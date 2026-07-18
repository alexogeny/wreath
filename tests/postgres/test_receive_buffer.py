from __future__ import annotations

import gc
import importlib
import struct
from typing import Any

import pytest

from .test_connection import FakePostgres

native: Any = None
try:
    native = importlib.import_module("wreath._native._postgres")
except ImportError:
    pass

requires_native = pytest.mark.skipif(native is None, reason="native PostgreSQL extension not built")


def _message(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


def _feed(protocol: Any, data: bytes, fragments: tuple[int, ...] = ()) -> None:
    offset = 0
    fragment_index = 0
    while offset < len(data):
        view = protocol.get_buffer(-1)
        requested = fragments[fragment_index] if fragment_index < len(fragments) else len(view)
        count = min(len(view), requested, len(data) - offset)
        view[:count] = data[offset : offset + count]
        del view
        protocol.buffer_updated(count)
        offset += count
        fragment_index += 1


@requires_native
@pytest.mark.asyncio
async def test_get_buffer_returns_writable_extension_owned_slab() -> None:
    protocol = native.BufferedProtocol()
    view = protocol.get_buffer(1)
    assert isinstance(view, memoryview)
    assert not view.readonly
    assert len(view) == 64 * 1024
    assert type(view.obj).__module__ == "wreath._native._postgres"
    del view

    encoded = _message(b"Z", b"I")
    _feed(protocol, encoded, (1, 1, 1, 1, 1, 1))
    assert await protocol.read_message() == (b"Z", b"I")
    stats = protocol._receive_stats()
    assert stats["idle_slabs"] >= 2
    assert stats["idle_slabs"] <= 4


@requires_native
@pytest.mark.asyncio
async def test_fragmented_message_spanning_multiple_slabs_parses() -> None:
    protocol = native.BufferedProtocol()
    payload = b"x" * (64 * 1024 + 137)
    _feed(protocol, _message(b"N", payload), (3, 11, 4096, 17))

    kind, decoded = await protocol.read_message()
    assert kind == b"N"
    assert decoded == payload
    stats = protocol._receive_stats()
    assert stats["chained_messages"] == 1
    assert stats["slab_allocations"] >= 4
    assert stats["idle_slabs"] <= 4


@requires_native
@pytest.mark.asyncio
async def test_data_row_spanning_slabs_retains_chained_slab_owners() -> None:
    protocol = native.BufferedProtocol()
    value = b"z" * (64 * 1024 + 31)
    row_payload = struct.pack("!Hi", 1, len(value)) + value
    _feed(protocol, _message(b"D", row_payload), (5, 8192, 7))

    kind, payload = await protocol.read_message()
    assert kind == b"D"
    assert isinstance(payload, memoryview)
    assert type(payload.obj).__name__ == "_ChainedPayload"
    assert native._decode_value(17, 1, payload[6:]) == value
    assert protocol._receive_stats()["chained_messages"] == 1


@requires_native
@pytest.mark.asyncio
async def test_oversized_message_uses_chained_fixed_slabs() -> None:
    protocol = native.BufferedProtocol()
    payload = b"y" * (3 * 64 * 1024 + 19)
    _feed(protocol, _message(b"N", payload))
    assert await protocol.read_message() == (b"N", payload)

    stats = protocol._receive_stats()
    assert stats["chained_messages"] == 1
    assert stats["slab_allocations"] >= 5
    assert stats["active_slabs"] == 0
    assert stats["idle_slabs"] <= 4


@requires_native
@pytest.mark.asyncio
async def test_control_message_queue_drains_in_order_across_compaction() -> None:
    """The control queue is a list plus a head index, compacted occasionally.

    Queue well past the compaction threshold (head >= 64 and head * 2 >= size),
    drain it fully, then queue and drain again: order must be exact, nothing
    lost, and the queue must stay reusable after it resets.
    """
    protocol = native.BufferedProtocol()
    payloads = [f"m{i:04d}".encode() for i in range(200)]
    _feed(protocol, b"".join(_message(b"N", p) for p in payloads))

    drained = [await protocol.read_message() for _ in range(len(payloads))]
    assert drained == [(b"N", p) for p in payloads]

    # The queue drained to empty and reset; it must still work afterwards.
    again = [f"z{i:04d}".encode() for i in range(150)]
    _feed(protocol, b"".join(_message(b"N", p) for p in again))
    assert [await protocol.read_message() for _ in range(len(again))] == [
        (b"N", p) for p in again
    ]


@requires_native
@pytest.mark.asyncio
async def test_data_row_payload_retains_slab_until_field_decoder_releases_it() -> None:
    protocol = native.BufferedProtocol()
    row_payload = struct.pack("!Hi", 1, 4) + struct.pack("!i", 42)
    _feed(protocol, _message(b"D", row_payload))

    kind, payload = await protocol.read_message()
    assert kind == b"D"
    assert isinstance(payload, memoryview)
    assert native._decode_value(23, 1, payload[6:10]) == 42
    slab = payload.obj
    assert type(slab).__module__ == "wreath._native._postgres"

    del payload
    gc.collect()
    for _ in range(8):
        _feed(protocol, _message(b"Z", b"I"))
        assert await protocol.read_message() == (b"Z", b"I")
    assert protocol._receive_stats()["idle_slabs"] <= 4


@requires_native
@pytest.mark.asyncio
async def test_native_connection_uses_buffered_protocol_without_stream_reader() -> None:
    server = FakePostgres(fragment=True)
    dsn = await server.start_tcp()
    connection = await native.connect(dsn)
    try:
        assert isinstance(connection._reader, native.BufferedProtocol)
        assert await connection.fetchval("select 42::int4") == 42
        stats = connection._reader._receive_stats()
        assert stats["slab_allocations"] >= 3
        assert stats["idle_slabs"] <= 4
    finally:
        await connection.close()
        await server.close()


@requires_native
@pytest.mark.asyncio
async def test_retired_slab_reclamation_is_budgeted_per_receive() -> None:
    """A pinned prefix must not be rescanned on every receive.

    Holding DataRow memoryviews pins their slabs in the retired list. Each
    get_buffer() inspects at most a small fixed budget and resumes from where
    it stopped, so cost per receive stays flat as the pinned count grows. The
    scan counter makes that assertable without any timing.
    """
    protocol = native.BufferedProtocol()
    pinned = 128
    held = []
    row = struct.pack("!Hi", 1, 60_000) + b"x" * 60_000
    for _ in range(pinned):
        _feed(protocol, _message(b"D", row))
        kind, payload = await protocol.read_message()
        assert kind == b"D"
        held.append(payload)
    assert protocol._receive_stats()["retired_slabs"] == pinned

    before = protocol._receive_stats()["retired_scan_steps"]
    cycles = 50
    for _ in range(cycles):
        _feed(protocol, _message(b"Z", b"I"))
        assert await protocol.read_message() == (b"Z", b"I")
    steps = protocol._receive_stats()["retired_scan_steps"] - before
    # A full rescan would be cycles * pinned == 6400 steps.
    assert steps <= cycles * 8, f"scan was not budgeted: {steps} steps over {cycles} cycles"
    assert protocol._receive_stats()["retired_slabs"] == pinned, "pinned slabs were freed"


@requires_native
@pytest.mark.asyncio
async def test_rotating_scan_eventually_reclaims_released_slabs() -> None:
    """The cursor must rotate, so every retained entry is examined eventually."""
    protocol = native.BufferedProtocol()
    held = []
    row = struct.pack("!Hi", 1, 60_000) + b"x" * 60_000
    for _ in range(128):
        _feed(protocol, _message(b"D", row))
        _kind, payload = await protocol.read_message()
        held.append(payload)
    assert protocol._receive_stats()["retired_slabs"] == 128

    # Release the ones the budgeted scan would reach last.
    for payload in held[64:]:
        payload.release()
    held = held[:64]
    gc.collect()

    # Ordinary receive cycles must eventually reclaim them without a full scan.
    for _ in range(400):
        _feed(protocol, _message(b"Z", b"I"))
        await protocol.read_message()
    remaining = protocol._receive_stats()["retired_slabs"]
    assert remaining <= 64, f"rotating scan did not reclaim released slabs: {remaining}"


@requires_native
@pytest.mark.asyncio
@pytest.mark.parametrize("released", [128, 256])
async def test_retired_slab_swap_delete_has_linear_move_count(released: int) -> None:
    protocol = native.BufferedProtocol()
    held = []
    row = struct.pack("!Hi", 1, 60_000) + b"x" * 60_000
    for _ in range(released):
        _feed(protocol, _message(b"D", row))
        _kind, payload = await protocol.read_message()
        held.append(payload)
    assert protocol._receive_stats()["retired_slabs"] == released

    for payload in held:
        payload.release()
    held.clear()
    gc.collect()

    before = protocol._receive_stats()
    cycles = 0
    while protocol._receive_stats()["retired_slabs"] and cycles < released:
        _feed(protocol, _message(b"Z", b"I"))
        assert await protocol.read_message() == (b"Z", b"I")
        cycles += 1

    stats = protocol._receive_stats()
    reclaims = stats["retired_reclaims"] - before["retired_reclaims"]
    moves = stats["retired_move_steps"] - before["retired_move_steps"]
    scans = stats["retired_scan_steps"] - before["retired_scan_steps"]
    assert stats["retired_slabs"] == 0
    assert reclaims == released
    assert moves <= reclaims
    assert scans <= cycles * 8
