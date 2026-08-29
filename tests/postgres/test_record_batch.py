from __future__ import annotations

import struct
from typing import Any

import pytest

from wreath._native import extension
from wreath._pgdriver import Record as PureRecord
from wreath._pgdriver import RecordBatch as PureRecordBatch
from wreath._template_tape import (
    MAX_OUTPUT_BYTES,
    Markup,
    TemplateRenderError,
    compile_tape,
)
from wreath.postgres import RecordBatch, _implementation
from wreath.templates import Template

_postgres = extension("_postgres")


def _data_row(fields: tuple[bytes | None, ...]) -> memoryview:
    payload = bytearray(struct.pack("!H", len(fields)))
    for field in fields:
        if field is None:
            payload += struct.pack("!i", -1)
        else:
            payload += struct.pack("!I", len(field)) + field
    return memoryview(payload)


def _decoded_batch(rows: tuple[tuple[int, str], ...]) -> Any:
    tape = _postgres._FieldTape(2)
    for identifier, message in rows:
        tape.append(_data_row((struct.pack("!q", identifier), message.encode())), 2)
    plan = _postgres._compile_decoder_plan((20, 25), (1, 1), ("id", "message"))
    return _postgres._decode_field_tape(plan, tape, "fetch_batch", 256)


@pytest.mark.parametrize(
    "batch_type, record_type",
    [(_postgres.RecordBatch, _postgres.Record), (PureRecordBatch, PureRecord)],
)
def test_record_batch_is_a_mutable_sequence_with_stable_column_sort(
    batch_type: type, record_type: type
) -> None:
    first = record_type(("id", "message"), (1, "same"))
    second = {"id": 2, "message": "before"}
    third = record_type(("id", "message"), (3, "same"))
    batch = batch_type((first, second))
    batch.append(third)

    batch.sort_by("message")

    assert [row["id"] for row in batch] == [2, 1, 3]


def test_native_batch_sort_failure_does_not_reorder_rows() -> None:
    rows = ({"key": 2}, {}, {"key": 1})
    batch = _postgres.RecordBatch(rows)

    with pytest.raises(KeyError):
        batch.sort_by("key")

    assert list(batch) == list(rows)


def test_template_consumes_native_batch_without_changing_output() -> None:
    template = Template.from_string(
        "{% for row in rows %}{{ row.id }}={{ row.message }};{% endfor %}"
    )
    batch = _postgres.RecordBatch(
        (
            _postgres.Record(("id", "message"), (1, "a<b")),
            {"id": 2, "message": "c&d"},
        )
    )

    assert template.render_bytes({"rows": batch}) == b"1=a&lt;b;2=c&amp;d;"


def test_native_tagged_batch_sorts_and_renders_without_materializing_cells() -> None:
    native_core = extension("_core")
    native_core.template_configure(Markup, TemplateRenderError)
    native_core.template_record_configure(_postgres._RECORD_C_API)
    tape = compile_tape("{% for row in rows %}{{ row.id }}={{ row.message }};{% endfor %}")
    program = native_core.template_compile(tape)
    batch = _decoded_batch(((3, "same"), (2, "a<b"), (1, "same"), (4, "c&d")))
    batch.append(_postgres.Record(("id", "message"), (0, "prefix")))

    assert _postgres._batch_storage_counts(batch) == (1, 0, 4, 4, 2)
    batch.sort_by("message")
    assert _postgres._batch_storage_counts(batch) == (1, 0, 4, 4, 2)

    rendered = native_core.template_render_compiled(program, {"rows": batch}, MAX_OUTPUT_BYTES)

    assert rendered == b"2=a&lt;b;4=c&amp;d;0=prefix;3=same;1=same;"
    assert _postgres._batch_storage_counts(batch) == (1, 0, 4, 4, 2)


def test_native_tagged_text_sort_matches_python_unicode_order() -> None:
    source = (
        (0, "same"),
        (1, ""),
        (2, "a"),
        (3, "a\x00z"),
        (4, "aa"),
        (5, "é"),
        (6, "日本語"),
        (7, "same"),
        (8, "😀"),
    )
    batch = _decoded_batch(source)

    batch.sort_by("message")

    assert [(row["id"], row["message"]) for row in batch] == sorted(source, key=lambda row: row[1])


def test_native_backend_exports_record_batch() -> None:
    assert _postgres is not None
    expected = _postgres.RecordBatch if _implementation == "native" else PureRecordBatch
    assert RecordBatch is expected
