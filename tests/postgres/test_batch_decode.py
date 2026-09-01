from __future__ import annotations

import gc
import importlib
import struct
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from wreath._pgdriver import Connection as PureConnection

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
    plan = native._compile_decoder_plan((16, 23, 25), (1, 1, 1), ("enabled", "number", "label"))

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
def test_inline_owner_buffer_cleanup_survives_acquisition_failure() -> None:
    tape = native._FieldTape(1)
    row = _data_row((b"value",))
    tape.append(row, 1)
    row.release()
    plan = native._compile_decoder_plan((25,), (1,), ("value",))

    with pytest.raises(ValueError, match="released memoryview"):
        native._decode_field_tape(plan, tape, "fetch", 1)


def test_fetch_batch_owns_decoded_cells_until_python_observes_a_row() -> None:
    tape = native._FieldTape(2)
    for value in range(12):
        tape.append(_data_row((struct.pack("!i", value), f"value-{value}".encode())), 2)
    plan = native._compile_decoder_plan((23, 25), (1, 1), ("number", "label"))
    before = native._record_allocation_count()

    rows = native._decode_field_tape(plan, tape, "fetch_batch", 256)

    assert len(rows) == 12
    assert native._record_allocation_count() == before
    rows.sort_by("label")
    assert native._record_allocation_count() == before
    assert rows[0]["label"] == "value-0"
    assert native._record_allocation_count() == before + 1


@requires_native
@pytest.mark.parametrize(
    "oid, format_code, wire, expected",
    [
        (21, 1, struct.pack("!h", -(2**15)), -(2**15)),
        (21, 1, struct.pack("!h", 2**15 - 1), 2**15 - 1),
        (23, 1, struct.pack("!i", -(2**31)), -(2**31)),
        (23, 1, struct.pack("!i", 2**31 - 1), 2**31 - 1),
        (20, 1, struct.pack("!q", -(2**63)), -(2**63)),
        (20, 1, struct.pack("!q", 2**63 - 1), 2**63 - 1),
        (20, 0, b"-9223372036854775808", -(2**63)),
        (20, 0, b"+9223372036854775807", 2**63 - 1),
    ],
)
def test_fetch_batch_native_integer_cells_preserve_extremes(
    oid: int, format_code: int, wire: bytes, expected: int
) -> None:
    tape = native._FieldTape(1)
    tape.append(_data_row((wire,)), 1)
    plan = native._compile_decoder_plan((oid,), (format_code,), ("value",))

    rows = native._decode_field_tape(plan, tape, "fetch_batch", 256)

    assert native._batch_storage_counts(rows) == (0, 0, 1, 0, 0)
    assert rows[0]["value"] == expected
    assert native._batch_storage_counts(rows) == (1, 1, 0, 0, 0)


@requires_native
@pytest.mark.parametrize(
    "wire",
    [
        b"\x80",
        b"\xc0\x80",
        b"\xe2\x28\xa1",
        b"\xed\xa0\x80",
        b"\xf4\x90\x80\x80",
    ],
)
def test_fetch_batch_rejects_invalid_utf8_before_exposing_rows(wire: bytes) -> None:
    tape = native._FieldTape(1)
    tape.append(_data_row((wire,)), 1)
    plan = native._compile_decoder_plan((25,), (1,), ("value",))

    with pytest.raises(UnicodeDecodeError):
        native._decode_field_tape(plan, tape, "fetch_batch", 256)

    assert tape.row_count == 1


def test_fetch_batch_final_flush_extends_the_operation_owned_batch() -> None:
    class DecodeHarness(PureConnection):
        _decode_fetch_extend = staticmethod(native._decode_fetch_extend)

    tape = native._FieldTape(2)
    for value in range(300):
        tape.append(_data_row((struct.pack("!i", value), f"value-{value}".encode())), 2)
    operation = SimpleNamespace(
        decoder_plan=native._compile_decoder_plan((23, 25), (1, 1), ("number", "label")),
        dest=None,
        discarded=False,
        error=None,
        field_tape=tape,
        mode="fetch_batch",
        rows=native.RecordBatch(),
    )
    connection = object.__new__(DecodeHarness)

    connection._flush_decode_batch(operation)
    connection._flush_decode_batch(operation)

    assert len(operation.rows) == 300
    assert operation.rows[0]["number"] == 0
    assert operation.rows[299]["label"] == "value-299"


def test_python_receive_path_defers_orm_hydration_until_completion() -> None:
    class DecodeHarness(PureConnection):
        def _flush_decode_batch(self, operation: Any) -> None:
            raise AssertionError("ORM rows were published before ReadyForQuery")

    tape = native._FieldTape(1)
    operation = SimpleNamespace(
        dest=(object(), {}, object()),
        field_tape=tape,
        mode="fetch",
        result_oids=(23,),
    )
    connection = object.__new__(DecodeHarness)
    payload = bytes(_data_row((struct.pack("!i", 1),)))

    for _ in range(256):
        connection._tape_data_row(operation, payload)

    assert tape.row_count == 256


@requires_native
def test_repeated_record_width_reuses_empty_gc_storage() -> None:
    plan = native._compile_decoder_plan((23, 25), (1, 1), ("number", "label"))

    def decode(value: int) -> Any:
        tape = native._FieldTape(2)
        tape.append(_data_row((struct.pack("!i", value), b"value")), 2)
        return native._decode_field_tape(plan, tape, "fetchrow", 256)

    first = decode(1)
    assert first["number"] == 1
    del first
    gc.collect()
    warmed = native._record_storage_allocation_count()

    second = decode(2)
    assert second["number"] == 2
    assert second["label"] == "value"
    assert native._record_storage_allocation_count() == warmed


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
async def test_cached_data_rows_decode_to_records_without_python_queue(
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
    assert after["direct_record_rows"] - before["direct_record_rows"] == 300
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
        assert stats["idle_slabs"] <= 2
        assert stats["retired_slabs"] <= 2
    finally:
        await connection.close()


# Consuming rows advances logical cursors instead of shifting every surviving
# ref and rebasing every owner index. Physical storage is reclaimed by
# occasional compaction, so these drive enough rows to cross it repeatedly.


@requires_native
def test_consume_one_row_at_a_time_decodes_every_value() -> None:
    rows = 20_000
    tape = native._FieldTape(3)
    for value in range(rows):
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
    plan = native._compile_decoder_plan((16, 23, 25), (1, 1, 1), ("enabled", "number", "label"))
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
