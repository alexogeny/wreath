from __future__ import annotations

import pytest

from wreath._native import _postgres
from wreath._pure.postgres import Record as PureRecord
from wreath._pure.postgres import RecordBatch as PureRecordBatch
from wreath.postgres import RecordBatch, _implementation
from wreath.templates import Template


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


def test_native_backend_exports_record_batch() -> None:
    assert _postgres is not None
    expected = _postgres.RecordBatch if _implementation == "native" else PureRecordBatch
    assert RecordBatch is expected
