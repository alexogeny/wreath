"""Fixed-size model storage: layout, bitmaps, protocol, and GC.

These tests assert the *shape* of the generated C types. Behavior is covered by
the shared suite, which runs against whichever storage is active; what is
specific here is that the layout is fixed, aligned, non-overlapping, and
collectable.
"""

from __future__ import annotations

import datetime
import gc
import sys
import uuid
from typing import Any

import pytest

from wreath.orm import Mapped, Model, column, relationship
from wreath.orm.model import _storage
from wreath.orm.types import (
    Bool,
    Bytea,
    Date,
    Float32,
    Float64,
    Int16,
    Int32,
    Int64,
    Json,
    Text,
    Timestamp,
    TimestampTz,
    Uuid,
)

storage: Any = _storage

# Inline (unboxed) cell kinds, mirroring WreathPgCellKind in model.h.
CELL_OBJECT = 0


class Wide(Model, table="wide"):
    """One column of every declarable type, in a deliberately awkward order."""

    id: Mapped[int] = column(Int64, primary_key=True)
    flag: Mapped[bool] = column(Bool)
    small: Mapped[int] = column(Int16)
    label: Mapped[str] = column(Text)
    medium: Mapped[int] = column(Int32)
    ratio: Mapped[float] = column(Float32)
    amount: Mapped[float] = column(Float64)
    blob: Mapped[bytes] = column(Bytea)
    day: Mapped[object] = column(Date)
    moment: Mapped[object] = column(Timestamp)
    zoned: Mapped[object] = column(TimestampTz)
    key: Mapped[object] = column(Uuid)
    doc: Mapped[object] = column(Json)


def test_the_storage_base_is_generated_and_prepended() -> None:
    base = Wide.__mro__[1]
    assert type(base) is storage._ModelType
    assert Wide.__mro__[2] is Model


def test_class_access_yields_expressions_through_storage_descriptors() -> None:
    from wreath.orm.expressions import ColumnExpr

    assert type(Wide.__dict__["id"]) is storage._ColumnDescriptor
    assert isinstance(Wide.id, ColumnExpr)
    assert Wide.id.column is Wide.__wreath_column_map__["id"]


def test_scalar_columns_are_stored_inline_and_only_payloads_are_boxed() -> None:
    layout = Wide.__layout__
    kinds = {
        column.python_name: field["kind"]
        for column, field in zip(Wide.__wreath_columns__, layout["fields"], strict=True)
    }
    boxed = {name for name, kind in kinds.items() if kind == CELL_OBJECT}
    # Exactly the variable-width payloads need a separate allocation.
    assert boxed == {"label", "blob", "doc"}


def test_cells_are_aligned_and_never_overlap() -> None:
    layout = Wide.__layout__
    spans = sorted(
        (field["offset"], field["offset"] + field["size"]) for field in layout["fields"]
    )
    for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
        assert end <= start, f"cells overlap: {spans}"
    assert spans[-1][1] <= layout["storage_basicsize"]
    for field in layout["fields"]:
        if field["kind"] == CELL_OBJECT:
            assert field["offset"] % 8 == 0
        elif field["size"] in (2, 4, 8):
            assert field["offset"] % field["size"] == 0, field


def test_bitmaps_sit_after_the_header_and_cover_every_column() -> None:
    layout = Wide.__layout__
    assert layout["bitmap_offset"] >= 32
    assert layout["bitmap_words"] * 64 >= layout["field_count"]
    # Three bitmaps (loaded, null, dirty) precede the first cell.
    first_cell = min(field["offset"] for field in layout["fields"])
    assert layout["bitmap_offset"] + layout["bitmap_words"] * 8 * 3 <= first_cell


def test_pointer_offsets_cover_object_cells_and_relationships() -> None:
    class Parent(Model, table="np_parents"):
        id: Mapped[int] = column(Int64, primary_key=True)

    class Child(Model, table="np_children"):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)
        parent_id: Mapped[int] = column(Int64, references=Parent.id)
        parent = relationship(Parent, foreign_key=parent_id, load="raise")

    layout = Child.__layout__
    object_cells = [f["offset"] for f in layout["fields"] if f["kind"] == CELL_OBJECT]
    assert layout["relation_count"] == 1
    # Every traversable pointer is registered: one text cell, one relationship.
    assert len(layout["pointer_offsets"]) == len(object_cells) + 1
    assert set(object_cells) <= set(layout["pointer_offsets"])


def test_instances_of_one_model_have_one_fixed_size() -> None:
    short = Wide._orm_new()
    long = Wide._orm_new()
    long._orm_set_loaded(3, "x" * 10_000)
    # Fixed size describes the model struct; the payload is allocated apart.
    assert sys.getsizeof(short) == sys.getsizeof(long)
    assert Wide.__basicsize__ == Wide.__layout__["basicsize"]


def test_two_models_get_independent_layouts() -> None:
    class Narrow(Model, table="narrow"):
        id: Mapped[int] = column(Int64, primary_key=True)

    assert Narrow.__basicsize__ < Wide.__basicsize__
    assert Narrow.__mro__[1] is not Wide.__mro__[1]


def test_a_model_has_no_instance_dict() -> None:
    instance = Wide._orm_new()
    with pytest.raises(AttributeError):
        instance.__dict__  # noqa: B018 - the absence is the subject
    with pytest.raises(AttributeError):
        instance.not_a_column = 1


def test_every_inline_type_round_trips_through_its_cell() -> None:
    values = {
        "id": 2**40,
        "flag": True,
        "small": -32768,
        "label": "héllo",
        "medium": -(2**31),
        "ratio": 0.5,
        "amount": -2.25,
        "blob": b"\x00\xff",
        "day": datetime.date(2024, 2, 29),
        "moment": datetime.datetime(2024, 7, 15, 13, 45, 30, 123456),
        "zoned": datetime.datetime(2024, 7, 15, 13, 45, 30, tzinfo=datetime.UTC),
        "key": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "doc": {"a": [1, 2]},
    }
    instance = Wide(**values)
    for name, value in values.items():
        assert getattr(instance, name) == value, name


def test_loaded_null_and_dirty_bits_are_independent_across_columns() -> None:
    class Bits(Model, table="bits"):
        id: Mapped[int] = column(Int64, primary_key=True)
        a: Mapped[str] = column(Text, nullable=True)
        b: Mapped[int] = column(Int64, nullable=True)

    from wreath.orm.model import PERSISTENT

    instance = Bits._orm_new()
    instance._orm_set_loaded(0, 1)
    instance._orm_state = PERSISTENT
    assert instance._orm_is_loaded(0) and not instance._orm_is_loaded(1)
    instance.a = None
    assert instance._orm_is_loaded(1) and instance._orm_is_null(1)
    assert not instance._orm_is_null(0)
    assert instance._orm_is_dirty(1) and not instance._orm_is_dirty(0)
    instance.b = 5
    assert instance._orm_is_dirty(2) and not instance._orm_is_null(2)
    instance._orm_clear_dirty()
    assert not instance._orm_has_changes()


def test_a_model_with_more_than_64_columns_uses_multiple_bitmap_words() -> None:
    body: dict[str, Any] = {"id": column(Int64, primary_key=True)}
    for index in range(100):
        body[f"c{index}"] = column(Int64, nullable=True)
    Many = type("Many", (Model,), body, table="many")
    assert Many.__layout__["bitmap_words"] == 2
    instance = Many._orm_new()
    instance._orm_set_loaded(100, 7)
    assert instance._orm_is_loaded(100)
    assert not instance._orm_is_loaded(99)
    assert instance._orm_get(100) == 7


def test_loaded_values_materialize_only_the_public_boundary() -> None:
    instance = Wide._orm_new()
    instance._orm_set_loaded(0, 7)
    instance._orm_set_loaded(1, True)
    instance._orm_set_loaded(3, None)
    assert instance._orm_loaded_values() == {"id": 7, "flag": True, "label": None}


# -- garbage collection --------------------------------------------------------


def test_instances_are_collectable_through_reference_cycles() -> None:
    class Node(Model, table="gc_nodes"):
        id: Mapped[int] = column(Int64, primary_key=True)
        label: Mapped[str] = column(Text)
        peer = relationship("Node", foreign_key="id", load="raise")

    gc.collect()
    instance = Node(id=1, label="x")
    instance._orm_set_relation(0, instance)  # a cycle through a relation cell
    instance._orm_owner = instance
    reference = __import__("weakref").ref(instance)
    del instance
    gc.collect()
    assert reference() is None


def test_a_model_class_and_its_storage_are_collectable() -> None:
    import weakref

    def build() -> Any:
        class Doomed(Model, table="doomed"):
            id: Mapped[int] = column(Int64, primary_key=True)
            label: Mapped[str] = column(Text)

        return Doomed

    cls = build()
    storage_reference = weakref.ref(cls.__mro__[1])
    instances = [cls(id=i, label=str(i)) for i in range(100)]
    del instances
    del cls
    gc.collect()
    # The generated storage type owns the layout arrays and must not outlive it.
    assert storage_reference() is None


def test_many_instances_survive_repeated_collection() -> None:
    for _ in range(3):
        batch = [Wide(id=i, flag=True, small=1, label="x" * i, medium=1, ratio=1.0,
                      amount=1.0, blob=b"", day=datetime.date(2024, 1, 1),
                      moment=datetime.datetime(2024, 1, 1),
                      zoned=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
                      key=uuid.uuid4(), doc={})
                 for i in range(200)]
        gc.collect()
        assert len(batch) == 200
        del batch
        gc.collect()


def test_a_failed_constructor_leaves_no_half_built_object() -> None:
    gc.collect()
    with pytest.raises(TypeError):
        Wide(id=1, flag="not a bool")
    with pytest.raises(TypeError, match="no column"):
        Wide(id=1, nonexistent=1)
    gc.collect()


def test_replacing_an_object_cell_releases_the_previous_value() -> None:
    import weakref

    class Held(Model, table="held"):
        id: Mapped[int] = column(Int64, primary_key=True)
        label: Mapped[str] = column(Text)

    class Payload(str):
        __slots__ = ("__weakref__",)

    payload = Payload("first")
    instance = Held(id=1, label=payload)
    reference = weakref.ref(payload)
    del payload
    instance.label = "second"
    gc.collect()
    assert reference() is None


def test_generated_storage_exposes_the_declared_protocol() -> None:
    protocol = [
        name
        for name in dir(Model)
        if name.startswith("_orm_") and callable(getattr(Model, name, None))
    ]
    for name in protocol:
        assert hasattr(Wide, name), f"generated storage lacks {name}"
    for name in ("_orm_state", "_orm_owner"):
        instance = Wide._orm_new()
        assert hasattr(instance, name)
