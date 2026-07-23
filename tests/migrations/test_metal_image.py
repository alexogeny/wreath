"""Native packed schema images and deterministic merge diff."""

from __future__ import annotations

import hashlib
import importlib
import struct
from typing import Any

import pytest

from wreath.migrations import (
    _compile_registry_image,
    _decode_catalog_image,
    _diff_packed_images,
    _fingerprint_image,
    _registry_descriptor,
    detect_single,
)
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text

native: Any = importlib.import_module("wreath._native._postgres")

_IMAGE_HEADER = struct.Struct("<4sIII")
_IMAGE_RECORD = struct.Struct("<QQII")
_TAPE_HEADER = struct.Struct("<4sII")
_TAPE_RECORD = struct.Struct("<IIQII")

TABLE = 1
COLUMN = 2
CONSTRAINT = 3
ADD = 1
DROP = 2
ALTER = 3


def descriptor(*records: tuple[str, str, str, int, str]) -> bytes:
    payload = bytearray(b"WMD1" + struct.pack("<II", 1, len(records)))
    for schema, table, name, kind, signature in records:
        values = tuple(value.encode() for value in (schema, table, name, signature))
        payload += struct.pack("<HHHHI", *(len(value) for value in values), kind)
        payload += b"".join(values)
    return bytes(payload)


def named_operations(plan: bytes) -> list[tuple[int, int, str, str, str, str, str]]:
    assert plan[:4] == b"WMP1" and struct.unpack_from("<I", plan, 4)[0] == 1
    count = struct.unpack_from("<I", plan, 8)[0]
    offset = 12
    result = []
    for _ in range(count):
        operation, kind, *lengths = struct.unpack_from("<IIHHHHHH", plan, offset)
        offset += 20
        values = []
        for length in lengths[:5]:
            values.append(plan[offset : offset + length].decode())
            offset += length
        result.append((operation, kind, *values))
    assert offset == len(plan)
    return result


def image(*records: tuple[int, int, int, int]) -> bytes:
    payload = b"".join(_IMAGE_RECORD.pack(*record) for record in records)
    return _IMAGE_HEADER.pack(b"WMI1", 1, _IMAGE_RECORD.size, len(records)) + payload


def data_row(object_id: int, parent_id: int, kind: int, signature: int) -> memoryview:
    fields = (
        struct.pack("!q", object_id),
        struct.pack("!q", parent_id),
        struct.pack("!i", kind),
        struct.pack("!i", signature),
    )
    payload = bytearray(struct.pack("!H", len(fields)))
    for field in fields:
        payload += struct.pack("!I", len(field)) + field
    return memoryview(payload)


def named_data_row(
    schema: str, table: str, name: str, kind: int, signature: int
) -> memoryview:
    fields = (
        schema.encode(),
        table.encode(),
        name.encode(),
        struct.pack("!i", kind),
        struct.pack("!i", signature),
    )
    payload = bytearray(struct.pack("!H", len(fields)))
    for field in fields:
        payload += struct.pack("!I", len(field)) + field
    return memoryview(payload)


def text_signature(value: str) -> int:
    result = 2166136261
    for byte in value.encode():
        result = ((result ^ byte) * 16777619) & ((1 << 32) - 1)
    return result


def object_id(kind: int, schema: str, table: str, name: str) -> int:
    value = 14695981039346656037
    for part in (kind.to_bytes(4, "little"), schema.encode(), table.encode(), name.encode()):
        for byte in part:
            value = ((value ^ byte) * 1099511628211) & ((1 << 64) - 1)
        value = ((value ^ 0xFF) * 1099511628211) & ((1 << 64) - 1)
    return value


def operations(tape: bytes) -> list[tuple[int, int, int, int, int]]:
    magic, version, count = _TAPE_HEADER.unpack_from(tape)
    assert magic == b"WMO1" and version == 1
    return [
        _TAPE_RECORD.unpack_from(tape, _TAPE_HEADER.size + index * _TAPE_RECORD.size)
        for index in range(count)
    ]


def test_registry_intent_compiles_to_the_same_native_name_and_signature_image() -> None:
    class Widget(Model, table="widgets", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)

    class Database:
        name = "main"

    registry = Registry(Database(), [Widget], validate_schema="off")
    desired = _compile_registry_image(registry)
    rows = [
        ("app", "widgets", "", TABLE, "table\x1fr\x1fp"),
        ("app", "widgets", "id", COLUMN, "column\x1f20\x1f1\x1f1\x1f\x1f\x1f"),
        ("app", "widgets", "name", COLUMN, "column\x1f25\x1f2\x1f1\x1f\x1f\x1f"),
        ("app", "widgets", "p:id:::", CONSTRAINT, "p:id:::"),
    ]
    tape = native._FieldTape(5)
    for schema, table, name, kind, signature in rows:
        fields = (
            schema.encode(), table.encode(), name.encode(), struct.pack("!i", kind),
            signature.encode(),
        )
        payload = bytearray(struct.pack("!H", len(fields)))
        for field in fields:
            payload += struct.pack("!I", len(field)) + field
        tape.append(memoryview(payload), 5)
    plan = native._compile_decoder_plan(
        (25, 25, 25, 23, 25), (1, 1, 1, 1, 1),
        ("schema", "table", "name", "kind", "signature"),
    )
    builder = native._migration_catalog_builder()
    native._migration_decode_catalog(plan, tape, builder, 256)
    actual_descriptor = builder.descriptor()
    actual = builder.finish()

    assert native._migration_compile_desired(actual_descriptor) == actual
    assert _diff_packed_images(desired, actual).operation_count == 0


@pytest.mark.asyncio
async def test_catalog_query_wrapper_returns_the_finished_native_image() -> None:
    expected = [(10, 0, TABLE, 1), (11, 10, COLUMN, 3)]

    class Connection:
        calls = 0

        async def _fetch_into(self, sql: str, args: tuple[object, ...], builder: Any) -> list[Any]:
            self.calls += 1
            assert sql == "catalog query" and args == ("app",)
            tape = native._FieldTape(4)
            for record in expected:
                tape.append(data_row(*record), 4)
            plan = native._compile_decoder_plan(
                (20, 20, 23, 23), (1, 1, 1, 1), ("id", "parent", "kind", "sig")
            )
            native._migration_decode_catalog(plan, tape, builder, 256)
            return []

    connection = Connection()
    result = await _decode_catalog_image(connection, "catalog query", ("app",))

    assert result == image(*expected)
    assert connection.calls == 1


def test_named_catalog_accepts_canonical_text_signatures() -> None:
    fields = (b"app", b"orders", b"status", struct.pack("!i", COLUMN), b"column:25:not-null")
    payload = bytearray(struct.pack("!H", len(fields)))
    for field in fields:
        payload += struct.pack("!I", len(field)) + field
    tape = native._FieldTape(5)
    tape.append(memoryview(payload), 5)
    plan = native._compile_decoder_plan(
        (25, 25, 25, 23, 25),
        (1, 1, 1, 1, 1),
        ("schema", "table", "name", "kind", "signature"),
    )
    builder = native._migration_catalog_builder()

    native._migration_decode_catalog(plan, tape, builder, 256)

    table = object_id(TABLE, "app", "orders", "")
    assert builder.finish() == image(
        (
            object_id(COLUMN, "app", "orders", "status"),
            table,
            COLUMN,
            text_signature("column:25:not-null"),
        )
    )


def test_named_catalog_rows_hash_and_sort_inside_the_native_destination() -> None:
    rows = [
        ("app", "orders", "status", COLUMN, 22),
        ("app", "accounts", "", TABLE, 10),
        ("app", "orders", "", TABLE, 20),
        ("app", "accounts", "id", COLUMN, 11),
    ]
    tape = native._FieldTape(5)
    for row in rows:
        tape.append(named_data_row(*row), 5)
    plan = native._compile_decoder_plan(
        (25, 25, 25, 23, 23),
        (1, 1, 1, 1, 1),
        ("schema", "table", "name", "kind", "signature"),
    )
    builder = native._migration_catalog_builder()

    native._migration_decode_catalog(plan, tape, builder, 256)
    actual = builder.finish()

    accounts = object_id(TABLE, "app", "accounts", "")
    orders = object_id(TABLE, "app", "orders", "")
    expected = sorted(
        [
            (accounts, 0, TABLE, 10),
            (orders, 0, TABLE, 20),
            (object_id(COLUMN, "app", "accounts", "id"), accounts, COLUMN, 11),
            (object_id(COLUMN, "app", "orders", "status"), orders, COLUMN, 22),
        ],
        key=lambda record: (record[2], record[0]),
    )
    assert actual == image(*expected)


def test_catalog_rows_decode_directly_into_a_native_image_without_records() -> None:
    tape = native._FieldTape(4)
    expected = [(10, 0, TABLE, 1)]
    expected.extend((value, 10, COLUMN, value * 3) for value in range(11, 311))
    for record in expected:
        tape.append(data_row(*record), 4)
    plan = native._compile_decoder_plan(
        (20, 20, 23, 23),
        (1, 1, 1, 1),
        ("object_id", "parent_id", "kind", "signature"),
    )
    builder = native._migration_catalog_builder()
    allocations = native._record_allocation_count()

    native._migration_decode_catalog(plan, tape, builder, 256)
    native._migration_decode_catalog(plan, tape, builder, 256)
    actual = builder.finish()

    assert actual == image(*expected)
    assert tape.row_count == 0
    assert native._record_allocation_count() == allocations


def test_numeric_catalog_has_no_named_descriptor() -> None:
    builder = native._migration_catalog_builder()
    tape = native._FieldTape(4)
    tape.append(data_row(1, 0, TABLE, 10), 4)
    plan = native._compile_decoder_plan(
        (20, 20, 23, 23), (1, 1, 1, 1), ("id", "parent", "kind", "signature")
    )
    native._migration_decode_catalog(plan, tape, builder, 256)
    with pytest.raises(RuntimeError, match="no named descriptor"):
        builder.descriptor()


def test_catalog_destination_rejects_noncanonical_rows_without_publishing() -> None:
    tape = native._FieldTape(4)
    tape.append(data_row(12, 10, COLUMN, 1), 4)
    tape.append(data_row(11, 10, COLUMN, 1), 4)
    plan = native._compile_decoder_plan(
        (20, 20, 23, 23), (1, 1, 1, 1), ("id", "parent", "kind", "signature")
    )
    builder = native._migration_catalog_builder()

    with pytest.raises(ValueError, match="ordered canonically"):
        native._migration_decode_catalog(plan, tape, builder, 256)

    assert builder.finish() == image()


def test_metal_diff_emits_stable_add_drop_and_alter_operations() -> None:
    actual = image(
        (10, 0, TABLE, 1),
        (11, 10, COLUMN, 100),
        (13, 10, COLUMN, 300),
    )
    desired = image(
        (10, 0, TABLE, 1),
        (11, 10, COLUMN, 101),
        (12, 10, COLUMN, 200),
    )

    result = _diff_packed_images(desired, actual)

    assert result.operation_count == 3
    assert operations(result.tape) == [
        (ALTER, COLUMN, 11, 100, 101),
        (ADD, COLUMN, 12, 0, 200),
        (DROP, COLUMN, 13, 300, 0),
    ]
    assert _diff_packed_images(desired, actual).tape == result.tape


def test_registry_plan_uses_column_names_for_reviewable_constraints() -> None:
    class Account(Model, table="accounts", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        email: Mapped[str] = column(Text, unique=True)

    class Entry(Model, table="entries", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        account_id: Mapped[int] = column(Int64, references=Account.id)

    class Database:
        name = "main"

    registry = Registry(Database(), [Account, Entry], validate_schema="off")
    plan = native._migration_plan_descriptors(
        _registry_descriptor(registry), descriptor()
    )
    constraint_names = {
        operation[4]
        for operation in named_operations(plan)
        if operation[1] == CONSTRAINT
    }

    assert constraint_names == {
        "p:id:::",
        "u:email:::",
        "f:account_id:app:accounts:id",
    }


def test_native_named_plan_preserves_add_drop_and_alter_review_metadata() -> None:
    desired = descriptor(
        ("app", "new_table", "", TABLE, "table\x1fr\x1fp"),
        ("app", "widgets", "id", COLUMN, "column-new"),
    )
    actual = descriptor(
        ("app", "old_table", "", TABLE, "table\x1fr\x1fp"),
        ("app", "widgets", "id", COLUMN, "column-old"),
    )

    plan = native._migration_plan_descriptors(desired, actual)

    assert named_operations(plan) == [
        (DROP, TABLE, "app", "old_table", "", "table\x1fr\x1fp", ""),
        (ADD, TABLE, "app", "new_table", "", "", "table\x1fr\x1fp"),
        (ALTER, COLUMN, "app", "widgets", "id", "column-old", "column-new"),
    ]
    assert native._migration_plan_descriptors(desired, actual) == plan


def test_equal_images_produce_an_empty_tape_and_cryptographic_fingerprint() -> None:
    current = image((10, 0, TABLE, 1), (11, 10, COLUMN, 100))

    result = _diff_packed_images(current, current)

    assert result.operation_count == 0
    assert operations(result.tape) == []
    assert _fingerprint_image(current) == hashlib.sha256(current).digest()


@pytest.mark.asyncio
async def test_single_detection_returns_bounded_native_fingerprints_and_diff() -> None:
    class Widget(Model, table="widgets", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)

    class Database:
        name = "main"

    registry = Registry(Database(), [Widget], validate_schema="off")
    expected = _compile_registry_image(registry)

    class Connection:
        async def _fetch_into(self, sql: str, args: tuple[object, ...], builder: Any) -> list[Any]:
            assert "pg_catalog.pg_class" in sql and args == ("app",)
            rows = [
                ("app", "widgets", "", TABLE, "table\x1fr\x1fp"),
                ("app", "widgets", "id", COLUMN, "column\x1f20\x1f1\x1f1\x1f\x1f\x1f"),
                ("app", "widgets", "p:id:::", CONSTRAINT, "p:id:::"),
            ]
            tape = native._FieldTape(5)
            for schema, table, name, kind, signature in rows:
                fields = (
                    schema.encode(), table.encode(), name.encode(), struct.pack("!i", kind),
                    signature.encode(),
                )
                payload = bytearray(struct.pack("!H", 5))
                for field in fields:
                    payload += struct.pack("!I", len(field)) + field
                tape.append(memoryview(payload), 5)
            plan = native._compile_decoder_plan(
                (25, 25, 25, 23, 25), (1, 1, 1, 1, 1),
                ("schema", "table", "name", "kind", "signature"),
            )
            native._migration_decode_catalog(plan, tape, builder, 256)
            return []

    detection = await detect_single(registry, Connection())

    assert detection.current
    assert detection.diff.operation_count == 0
    assert detection.desired_fingerprint == hashlib.sha256(expected).digest()
    assert detection.actual_fingerprint == detection.desired_fingerprint


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        _IMAGE_HEADER.pack(b"BAD!", 1, _IMAGE_RECORD.size, 0),
        image((11, 10, COLUMN, 1), (10, 0, TABLE, 1)),
        image((10, 0, TABLE, 1), (10, 0, TABLE, 2)),
    ],
)
def test_malformed_or_noncanonical_images_are_rejected(bad: bytes) -> None:
    with pytest.raises(ValueError):
        _diff_packed_images(bad, image())


def test_mutable_image_buffers_are_rejected() -> None:
    with pytest.raises(TypeError, match="read-only"):
        _diff_packed_images(bytearray(image()), image())
