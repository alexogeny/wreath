from __future__ import annotations

import importlib
import struct
from collections.abc import AsyncIterator
from typing import Any

import pytest

from .test_connection import POSTGRES_BACKENDS, FakePostgres

native: Any = None
try:
    native = importlib.import_module("wreath._native._postgres")
except ImportError:
    pass

requires_native = pytest.mark.skipif(native is None, reason="native PostgreSQL extension not built")


@pytest.fixture(params=POSTGRES_BACKENDS, ids=lambda backend: backend._implementation)
def postgres(request: pytest.FixtureRequest) -> Any:
    return request.param


@pytest.fixture
async def database() -> AsyncIterator[tuple[FakePostgres, str]]:
    server = FakePostgres(fragment=True)
    dsn = await server.start_tcp()
    try:
        yield server, dsn
    finally:
        await server.close()


def _data_row(fields: tuple[bytes | None, ...]) -> memoryview:
    payload = bytearray(struct.pack("!H", len(fields)))
    for field in fields:
        if field is None:
            payload += struct.pack("!i", -1)
        else:
            payload += struct.pack("!I", len(field)) + field
    return memoryview(payload)


@requires_native
def test_column_batch_decode_matches_builtin_values_and_record_lookup() -> None:
    tape = native._FieldTape(3)
    for value in range(300):
        tape.append(
            _data_row(
                (
                    b"\x01" if value % 2 else b"\x00",
                    struct.pack("!i", value),
                    f"value-{value}".encode(),
                )
            ),
            3,
        )
    plan = native._compile_decoder_plan(
        (16, 23, 25), (1, 1, 1), ("enabled", "number", "label")
    )

    first = native._decode_field_tape(plan, tape, "fetch", 256)
    second = native._decode_field_tape(plan, tape, "fetch", 256)
    rows = first + second

    assert len(rows) == 300
    assert isinstance(rows[0], native.Record)
    assert rows[0][0] is False
    assert rows[1]["enabled"] is True
    assert rows[299]["number"] == 299
    assert rows[299][2] == "value-299"
    assert tape.row_count == 0
    assert tape.owner_count == 0


@requires_native
def test_decoder_functions_are_selected_once_per_plan_column() -> None:
    plan = native._compile_decoder_plan((16, 23, 25), (1, 1, 1), ("a", "b", "c"))
    assert plan.column_count == 3
    assert plan.decoder_selections == 3

    tape = native._FieldTape(3)
    for value in range(16):
        tape.append(_data_row((b"\x01", struct.pack("!i", value), b"x")), 3)
    native._decode_field_tape(plan, tape, "fetch", 256)
    assert plan.decoder_selections == 3


@requires_native
def test_fetchval_records_only_selected_field_and_allocates_no_record() -> None:
    tape = native._FieldTape(3)
    tape.append(_data_row((struct.pack("!i", 7), b"ignored", b"ignored")), 1)
    plan = native._compile_decoder_plan((23, 25, 25), (1, 1, 1), ("value", "b", "c"))
    before = native._record_allocation_count()

    value = native._decode_field_tape(plan, tape, "fetchval", 256)

    assert value == 7
    assert native._record_allocation_count() == before
    assert tape.stored_field_count == 0


@requires_native
def test_fetchrow_decodes_directly_without_result_list() -> None:
    tape = native._FieldTape(2)
    tape.append(_data_row((struct.pack("!q", 9), b"wreath")), 2)
    plan = native._compile_decoder_plan((20, 25), (1, 1), ("number", "name"))

    row = native._decode_field_tape(plan, tape, "fetchrow", 256)

    assert isinstance(row, native.Record)
    assert row["number"] == 9
    assert row["name"] == "wreath"
    assert not isinstance(row, list)


@requires_native
def test_unknown_type_uses_scalar_fallback_without_affecting_builtin_columns() -> None:
    tape = native._FieldTape(2)
    tape.append(_data_row((struct.pack("!i", 42), b"custom-wire-value")), 2)
    plan = native._compile_decoder_plan((23, 999_999), (1, 1), ("number", "custom"))

    rows = native._decode_field_tape(plan, tape, "fetch", 256)

    assert rows[0]["number"] == 42
    assert rows[0]["custom"] == b"custom-wire-value"
    assert plan.decoder_selections == 2


@requires_native
def test_nulls_and_256_row_batch_boundary() -> None:
    tape = native._FieldTape(2)
    for value in range(257):
        tape.append(
            _data_row((None if value % 2 else struct.pack("!i", value), b"x")),
            2,
        )
    plan = native._compile_decoder_plan((23, 25), (1, 1), ("number", "label"))

    first = native._decode_field_tape(plan, tape, "fetch", 256)
    assert len(first) == 256
    assert tape.row_count == 1
    assert tape.owner_count == 1
    second = native._decode_field_tape(plan, tape, "fetch", 256)
    assert second[0]["number"] == 256
    assert tape.owner_count == 0


@pytest.mark.asyncio
async def test_pure_and_native_multirow_results_are_identical(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    _, dsn = database
    connection = await postgres.connect(dsn)
    try:
        rows = await connection.fetch("select generate_series(0, 299)")
    finally:
        await connection.close()
    assert [row["value"] for row in rows] == list(range(300))


@requires_native
@pytest.mark.asyncio
async def test_cached_data_rows_flow_parser_to_tape_without_python_queue(
    database: tuple[FakePostgres, str],
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)
    try:
        sql = "select generate_series(0, 299)"
        await connection.fetch(sql)
        before = connection._reader._receive_stats()
        rows = await connection.fetch(sql)
        after = connection._reader._receive_stats()
    finally:
        await connection.close()
    assert len(rows) == 300
    assert after["direct_data_rows"] - before["direct_data_rows"] == 300
    assert after["queued_messages"] - before["queued_messages"] <= 4


@requires_native
@pytest.mark.asyncio
async def test_large_native_result_releases_slabs_at_batch_boundaries(
    database: tuple[FakePostgres, str],
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)
    try:
        rows = await connection.fetch("select generate_series(0, 599)")
        assert len(rows) == 600
        stats = connection._reader._receive_stats()
        assert stats["active_slabs"] == 0
        assert stats["idle_slabs"] <= 1
        assert stats["retired_slabs"] <= 2
    finally:
        await connection.close()


# --- cursor-based tape consumption ------------------------------------------
#
# Consuming rows advances logical cursors instead of shifting every surviving
# ref and rebasing every owner index. Physical storage is reclaimed by
# occasional compaction, so these drive enough rows to cross it repeatedly.

@requires_native
def test_consume_one_row_at_a_time_decodes_every_value() -> None:
    rows = 20_000
    tape = native._FieldTape(3)
    for value in range(rows):
        tape.append(
            _data_row((
                b"\x01" if value % 2 else b"\x00",
                struct.pack("!i", value),
                f"value-{value}".encode(),
            )),
            3,
        )
    plan = native._compile_decoder_plan(
        (16, 23, 25), (1, 1, 1), ("enabled", "number", "label")
    )
    seen = []
    while tape.row_count:
        batch = native._decode_field_tape(plan, tape, "fetch", 1)  # crosses compaction
        assert len(batch) == 1
        seen.append(batch[0])
    assert len(seen) == rows
    for i, record in enumerate(seen):  # exact values, in order, after every shift
        assert record["enabled"] is bool(i % 2)
        assert record["number"] == i
        assert record["label"] == f"value-{i}"
    assert tape.row_count == 0
    assert tape.owner_count == 0


@requires_native
def test_tape_is_reusable_after_a_cursor_drain() -> None:
    """A drained tape resets both cursors and accepts a fresh batch."""
    plan = native._compile_decoder_plan((23,), (1,), ("n",))
    tape = native._FieldTape(1)
    for value in range(3000):
        tape.append(_data_row((struct.pack("!i", value),)), 1)
    while tape.row_count:
        native._decode_field_tape(plan, tape, "fetch", 1)
    assert tape.row_count == 0 and tape.owner_count == 0

    for value in range(500, 800):
        tape.append(_data_row((struct.pack("!i", value),)), 1)
    decoded = native._decode_field_tape(plan, tape, "fetch", 1000)
    assert [r["n"] for r in decoded] == list(range(500, 800))


@requires_native
def test_partial_consume_then_batch_consume_keeps_order() -> None:
    plan = native._compile_decoder_plan((23,), (1,), ("n",))
    tape = native._FieldTape(1)
    total = 5000
    for value in range(total):
        tape.append(_data_row((struct.pack("!i", value),)), 1)
    # Drain past the ref compaction threshold one row at a time...
    head = [native._decode_field_tape(plan, tape, "fetch", 1)[0]["n"] for _ in range(2000)]
    assert head == list(range(2000))
    # ...then take the rest in one batch from the middle of the tape.
    tail = [r["n"] for r in native._decode_field_tape(plan, tape, "fetch", total)]
    assert tail == list(range(2000, total))
    assert tape.row_count == 0


@requires_native
def test_owner_slabs_stay_alive_while_their_rows_survive() -> None:
    """A consumed prefix must not release a slab a surviving field still needs.

    Each append here owns its own payload, so owner lifetime is observable: the
    surviving rows must still decode to their exact values after the earlier
    ones are consumed and compaction has moved the owner base.
    """
    plan = native._compile_decoder_plan((25,), (1,), ("label",))
    tape = native._FieldTape(1)
    total = 400
    for value in range(total):
        tape.append(_data_row((f"row-{value:04d}".encode(),)), 1)
    assert tape.owner_count == total  # one payload owner per append

    for expected in range(200):  # crosses the owner compaction threshold
        batch = native._decode_field_tape(plan, tape, "fetch", 1)
        assert batch[0]["label"] == f"row-{expected:04d}"
    assert tape.owner_count == total - 200

    rest = native._decode_field_tape(plan, tape, "fetch", total)
    assert [r["label"] for r in rest] == [f"row-{i:04d}" for i in range(200, total)]
