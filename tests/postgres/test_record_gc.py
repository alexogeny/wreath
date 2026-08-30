from __future__ import annotations

import gc
import importlib
import math
import struct
import weakref
from typing import Any

import pytest

native: Any = None
try:
    native = importlib.import_module("wreath._native._postgres")
except ImportError:
    pass

requires_native = pytest.mark.skipif(
    native is None, reason="native PostgreSQL extension not built"
)


def _data_row(fields: tuple[bytes | None, ...]) -> memoryview:
    payload = bytearray(struct.pack("!H", len(fields)))
    for field in fields:
        if field is None:
            payload += struct.pack("!i", -1)
        else:
            payload += struct.pack("!I", len(field)) + field
    return memoryview(payload)


@requires_native
def test_primitive_record_constructor_is_not_gc_tracked() -> None:
    row = native.Record(("id", "label", "payload"), (7, "wreath", b"pg"))

    assert not gc.is_tracked(row)


@requires_native
def test_primitive_decoder_records_are_not_gc_tracked() -> None:
    tape = native._FieldTape(5)
    tape.append(
        _data_row(
            (
                struct.pack("!i", 7),
                b"\x01",
                struct.pack("!d", 1.5),
                b"wreath",
                b"pg",
            )
        ),
        5,
    )
    plan = native._compile_decoder_plan(
        (23, 16, 701, 25, 17),
        (1, 1, 1, 1, 1),
        ("id", "enabled", "score", "label", "payload"),
    )

    row = native._decode_field_tape(plan, tape, "fetch", 256)[0]

    assert tuple(row) == (7, True, 1.5, "wreath", b"pg")
    assert not gc.is_tracked(row)


@requires_native
def test_record_with_cycle_capable_value_remains_gc_tracked() -> None:
    class Owner:
        pass

    owner = Owner()
    row = native.Record(("owner",), (owner,))
    owner.row = row
    owner_reference = weakref.ref(owner)

    assert gc.is_tracked(row)

    del owner
    del row
    gc.collect()

    assert owner_reference() is None


@requires_native
def test_decoder_names_subclass_keeps_record_cycle_visible_to_gc() -> None:
    class Names(tuple):
        pass

    class Owner:
        pass

    names = Names(("value",))
    owner = Owner()
    names.owner = owner
    tape = native._FieldTape(1)
    tape.append(_data_row((struct.pack("!i", 7),)), 1)
    plan = native._compile_decoder_plan((23,), (1,), names)
    rows = native._decode_field_tape(plan, tape, "fetch", 1)
    row = rows[0]
    owner.row = row
    owner_reference = weakref.ref(owner)

    assert gc.is_tracked(row)

    del names
    del owner
    del tape
    del plan
    del rows
    del row
    gc.collect()

    assert owner_reference() is None


@requires_native
@pytest.mark.parametrize(
    "bits",
    [
        0x0000000000000000,
        0x8000000000000000,
        0x3FF8000000000000,
        0x7FF0000000000000,
        0xFFF0000000000000,
        0x7FF8000000000001,
    ],
)
def test_binary_float8_decode_preserves_ieee_value(bits: int) -> None:
    payload = bits.to_bytes(8, "big")
    tape = native._FieldTape(1)
    tape.append(_data_row((payload,)), 1)
    plan = native._compile_decoder_plan((701,), (1,), ("value",))

    value = native._decode_field_tape(plan, tape, "fetchval", 1)

    if math.isnan(value):
        assert bits & 0x7FF0000000000000 == 0x7FF0000000000000
        assert bits & 0x000FFFFFFFFFFFFF
    else:
        assert struct.pack("!d", value) == payload


@requires_native
@pytest.mark.parametrize(
    "bits",
    [
        0x00000000,
        0x80000000,
        0x3FC00000,
        0x7F800000,
        0xFF800000,
        0x7FC00001,
    ],
)
def test_binary_float4_decode_preserves_ieee_value(bits: int) -> None:
    payload = bits.to_bytes(4, "big")
    tape = native._FieldTape(1)
    tape.append(_data_row((payload,)), 1)
    plan = native._compile_decoder_plan((700,), (1,), ("value",))

    value = native._decode_field_tape(plan, tape, "fetchval", 1)

    if math.isnan(value):
        assert bits & 0x7F800000 == 0x7F800000
        assert bits & 0x007FFFFF
    else:
        assert struct.pack("!f", value) == payload
