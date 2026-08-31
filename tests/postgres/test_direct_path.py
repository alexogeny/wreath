from __future__ import annotations

import importlib
import struct
from typing import Any

import pytest

native: Any = None
try:
    native = importlib.import_module("wreath._native._postgres")
except ImportError:
    pass

requires_native = pytest.mark.skipif(native is None, reason="native PostgreSQL extension not built")


def _message(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


def _data_row(fields: tuple[bytes | None, ...]) -> bytes:
    payload = bytearray(struct.pack("!H", len(fields)))
    for field in fields:
        if field is None:
            payload += struct.pack("!i", -1)
        else:
            payload += struct.pack("!I", len(field)) + field
    return bytes(payload)


class _FakeTransport:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closing = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closing = True

    def is_closing(self) -> bool:
        return self.closing


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


class _FakeOperation:
    def __init__(
        self,
        mode: str,
        tape: Any,
        plan: Any,
        rows: list[Any] | None,
        dest: Any = None,
    ) -> None:
        self.mode = mode
        self.field_tape = tape
        self.decoder_plan = plan
        self.rows = rows
        #: None decodes to Records; anything else is handed to the backend's
        #: decode-destination hook instead.
        self.dest = dest
        self.discarded = False
        self.command = ""


def _register(
    protocol: Any, mode: str, oids: tuple[int, ...], names: tuple[str, ...]
) -> _FakeOperation:
    tape = native._FieldTape(len(oids))
    plan = native._compile_decoder_plan(oids, (1,) * len(oids), names)
    operation = _FakeOperation(mode, tape, plan, [] if mode == "fetch" else None)
    protocol.register_operations((operation,))
    return operation


@requires_native
@pytest.mark.asyncio
async def test_native_write_avoids_drain_until_transport_pauses() -> None:
    protocol = native.BufferedProtocol()
    transport = _FakeTransport()
    protocol.connection_made(transport)

    assert protocol.write_with_backpressure(b"first") is None
    protocol.pause_writing()
    pending = protocol.write_with_backpressure(b"second")
    assert pending is not None and not pending.done()
    protocol.pause_writing()
    assert protocol.write_with_backpressure(b"third") is pending

    protocol.resume_writing()
    await pending
    assert transport.writes == [b"first", b"second", b"third"]
    stats = protocol._receive_stats()
    assert stats["write_calls"] == 3
    assert stats["pause_writing_calls"] == 1
    assert stats["resume_writing_calls"] == 1
    assert stats["backpressure_waits"] == 2


@requires_native
@pytest.mark.asyncio
async def test_connection_loss_releases_native_backpressure_waiter() -> None:
    protocol = native.BufferedProtocol()
    protocol.connection_made(_FakeTransport())
    protocol.pause_writing()
    pending = protocol.write_with_backpressure(b"blocked")

    protocol.connection_lost(None)

    assert pending is not None
    await pending


@requires_native
@pytest.mark.asyncio
async def test_connection_loss_fails_native_read_waiters_and_future_io() -> None:
    protocol = native.BufferedProtocol()
    protocol.connection_made(_FakeTransport())
    pending = protocol.read_message()
    assert not pending.done()

    protocol.connection_lost(None)

    with pytest.raises(ConnectionError, match="transport closed"):
        await pending
    with pytest.raises(ConnectionError, match="transport closed"):
        await protocol.read_message()
    with pytest.raises(ConnectionError, match="transport closed"):
        protocol.write(b"query")


@requires_native
@pytest.mark.asyncio
async def test_execute_control_messages_stay_on_native_path() -> None:
    protocol = native.BufferedProtocol()
    operation = _register(protocol, "execute", (23,), ("value",))
    before = protocol._receive_stats()

    response = b"".join(
        (
            _message(b"1", b""),
            _message(b"t", b"\x00\x00"),
            _message(b"n", b""),
            _message(b"2", b""),
            _message(b"C", b"UPDATE 1\x00"),
            _message(b"Z", b"I"),
        )
    )
    _feed(protocol, response, (1, 2, 3, 5, 8, 13))

    assert operation.command == "UPDATE 1"
    assert await protocol.read_message() == (b"t", b"\x00\x00")
    assert await protocol.read_message() == (b"Z", b"I")
    after = protocol._receive_stats()
    assert after["queued_messages"] - before["queued_messages"] == 2


@requires_native
@pytest.mark.asyncio
async def test_execute_command_tag_preserves_utf8_replacement_behavior() -> None:
    protocol = native.BufferedProtocol()
    operation = _register(protocol, "execute", (23,), ("value",))

    _feed(protocol, _message(b"C", b"UPDATE \xff\x00") + _message(b"Z", b"I"))

    assert operation.command == "UPDATE \ufffd"
    assert await protocol.read_message() == (b"Z", b"I")


@requires_native
@pytest.mark.asyncio
async def test_direct_data_row_spanning_two_slabs_decodes_into_a_record() -> None:
    protocol = native.BufferedProtocol()
    operation = _register(protocol, "fetch", (17,), ("blob",))
    value = b"s" * (64 * 1024 + 51)
    _feed(protocol, _message(b"D", _data_row((value,))), (9, 4096, 33))

    assert operation.field_tape.row_count == 0
    assert protocol._receive_stats()["chained_messages"] == 1
    assert protocol._receive_stats()["direct_data_rows"] == 1
    assert operation.rows[0]["blob"] == value
    assert operation.field_tape.owner_count == 0


@requires_native
@pytest.mark.asyncio
async def test_direct_rows_do_not_retain_a_slab_owner() -> None:
    protocol = native.BufferedProtocol()
    operation = _register(protocol, "fetch", (23,), ("number",))
    batch = b"".join(_message(b"D", _data_row((struct.pack("!i", value),))) for value in range(200))
    _feed(protocol, batch)

    tape = operation.field_tape
    assert tape.row_count == 0
    assert tape.owner_count == 0
    assert [row["number"] for row in operation.rows] == list(range(200))
    assert protocol._receive_stats()["direct_record_rows"] == 200


@requires_native
@pytest.mark.asyncio
async def test_slabs_recycle_across_256_row_batch_boundaries() -> None:
    protocol = native.BufferedProtocol()
    operation = _register(protocol, "fetch", (25,), ("label",))
    total = 3000
    batch = b"".join(
        _message(b"D", _data_row((f"value-{value:028d}".encode(),))) for value in range(total)
    )
    _feed(protocol, batch)
    _feed(protocol, _message(b"Z", b"I"))
    assert await protocol.read_message() == (b"Z", b"I")

    assert operation.rows is not None and len(operation.rows) == total
    assert [row["label"] for row in operation.rows[:2]] == [
        "value-" + "0" * 22 + "000000",
        "value-" + "0" * 22 + "000001",
    ]
    assert operation.rows[-1]["label"] == f"value-{total - 1:028d}"
    assert operation.field_tape.row_count == 0
    stats = protocol._receive_stats()
    assert stats["direct_data_rows"] == total
    # The ~130 KiB of row data must flow through recycled fixed slabs, not
    # unbounded new allocations.
    assert stats["slab_allocations"] <= 6
    assert stats["active_slabs"] <= 1
    assert stats["idle_slabs"] <= 4


@requires_native
@pytest.mark.asyncio
async def test_operation_discarded_before_rows_drops_them_in_parser() -> None:
    protocol = native.BufferedProtocol()
    operation = _register(protocol, "fetch", (23,), ("number",))
    operation.discarded = True
    batch = b"".join(_message(b"D", _data_row((struct.pack("!i", value),))) for value in range(20))
    _feed(protocol, batch)

    assert operation.field_tape.row_count == 0
    assert operation.rows == []
    assert protocol._receive_stats()["direct_data_rows"] == 20


@requires_native
@pytest.mark.asyncio
async def test_cancellation_while_direct_rows_arrive_keeps_parser_consistent() -> None:
    protocol = native.BufferedProtocol()
    operation = _register(protocol, "fetch", (23,), ("number",))
    row = _message(b"D", _data_row((struct.pack("!i", 7),)))
    _feed(protocol, row * 10)
    # Cancellation lands mid-stream; the driver discards the operation's
    # results at publish time, the parser only has to stay consistent.
    operation.discarded = True
    _feed(protocol, row * 10)
    _feed(protocol, _message(b"Z", b"I"))
    assert await protocol.read_message() == (b"Z", b"I")

    assert operation.rows is not None and len(operation.rows) == 20
    assert operation.field_tape.row_count == 0
    assert operation.field_tape.owner_count == 0
    stats = protocol._receive_stats()
    assert stats["active_slabs"] == 0
    assert stats["idle_slabs"] <= 4


@requires_native
@pytest.mark.asyncio
async def test_malformed_field_length_in_direct_path_raises() -> None:
    protocol = native.BufferedProtocol()
    operation = _register(protocol, "fetch", (23,), ("number",))
    # Field length claims 64 bytes but only 4 are present.
    payload = struct.pack("!H", 1) + struct.pack("!I", 64) + struct.pack("!i", 1)
    with pytest.raises(ValueError):
        _feed(protocol, _message(b"D", payload))
    assert operation.field_tape.row_count == 0

    # Column-count mismatches are rejected the same way.
    protocol = native.BufferedProtocol()
    operation = _register(protocol, "fetch", (23,), ("number",))
    with pytest.raises(ValueError):
        _feed(protocol, _message(b"D", _data_row((b"\x00\x00\x00\x01", b"extra"))))
    assert operation.field_tape.row_count == 0
