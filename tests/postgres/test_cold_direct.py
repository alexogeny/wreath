from __future__ import annotations

import asyncio
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


def _message(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


def _parameter_description(*oids: int) -> bytes:
    return struct.pack("!H", len(oids)) + b"".join(struct.pack("!I", oid) for oid in oids)


def _row_description(
    fields: tuple[tuple[str, int, int], ...],
) -> bytes:
    payload = bytearray(struct.pack("!H", len(fields)))
    for name, oid, format_code in fields:
        payload += name.encode() + b"\x00"
        payload += struct.pack("!IhIhih", 0, 0, oid, 4, -1, format_code)
    return bytes(payload)


def _data_row(*fields: bytes | None) -> bytes:
    payload = bytearray(struct.pack("!H", len(fields)))
    for field in fields:
        if field is None:
            payload += struct.pack("!i", -1)
        else:
            payload += struct.pack("!I", len(field)) + field
    return bytes(payload)


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


def _cold_operation(loop: asyncio.AbstractEventLoop, mode: str = "fetch") -> Any:
    operation = native.Operation(1, "select $1::int4", (7,), mode, loop.create_future(), None)
    operation.parameter_oids = ()
    operation.decoder_plan = None
    operation.field_tape = None
    operation.plan = None
    operation.cold = True
    return operation


@requires_native
@pytest.mark.asyncio
async def test_cold_row_description_installs_direct_decoder_before_same_slab_rows() -> None:
    protocol = native.BufferedProtocol()
    operation = _cold_operation(asyncio.get_running_loop())
    protocol.register_operations((operation,))
    response = b"".join(
        (
            _message(b"t", _parameter_description(23)),
            _message(b"T", _row_description((("value", 23, 0),))),
            _message(b"D", _data_row(b"7")),
        )
    )

    _feed(protocol, response, (1, 2, 3, 5, 8, 13, 21))

    assert operation.parameter_oids == (23,)
    assert operation.result_names == ("value",)
    assert operation.result_oids == (23,)
    assert operation.result_formats == (0,)
    assert operation.decoder_plan is not None
    assert operation.field_tape is not None
    assert operation.rows is not None
    assert operation.rows[0]["value"] == 7
    stats = protocol._receive_stats()
    assert stats["direct_data_rows"] == 1
    assert stats["queued_messages"] == 0


@requires_native
@pytest.mark.asyncio
async def test_cold_row_description_rejects_trailing_and_truncated_fields() -> None:
    loop = asyncio.get_running_loop()
    for payload, message in (
        (b"\x00", "truncated RowDescription"),
        (_row_description((("value", 23, 0),)) + b"x", "invalid RowDescription length"),
        (b"\x00\x01value", "truncated RowDescription field"),
    ):
        protocol = native.BufferedProtocol()
        protocol.register_operations((_cold_operation(loop),))
        with pytest.raises(native.ProtocolError, match=message):
            _feed(protocol, _message(b"T", payload))


@requires_native
@pytest.mark.asyncio
async def test_operation_subclass_retains_python_row_description_seam() -> None:
    class HookOperation(native.Operation):
        pass

    loop = asyncio.get_running_loop()
    operation = HookOperation(1, "select 7", (), "fetch", loop.create_future(), None)
    operation.dest = None
    operation.field_tape = None
    operation.decoder_plan = None
    operation.rows = []
    operation.cold = True
    operation.mode = "fetch"
    protocol = native.BufferedProtocol()
    protocol.register_operations((operation,))
    payload = _row_description((("value", 23, 0),))

    _feed(protocol, _message(b"T", payload))

    assert await protocol.read_message() == (b"T", payload)
    assert protocol._receive_stats()["queued_messages"] == 1


@requires_native
@pytest.mark.asyncio
async def test_overridden_decoder_compiler_retains_python_row_description_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def compile_override(*_args: object) -> None:
        return None

    connection = native.Connection.__new__(native.Connection)
    monkeypatch.setattr(
        native.Connection,
        "_compile_decoder_plan",
        staticmethod(compile_override),
    )
    protocol = native.BufferedProtocol()
    protocol.attach_connection(connection)
    operation = _cold_operation(asyncio.get_running_loop())
    protocol.register_operations((operation,))
    payload = _row_description((("value", 23, 0),))

    _feed(protocol, _message(b"T", payload))

    assert await protocol.read_message() == (b"T", payload)
    assert protocol._receive_stats()["queued_messages"] == 1
