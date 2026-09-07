from __future__ import annotations

import dataclasses
from typing import get_type_hints

import pytest

from wreath.orm import Mapped, Model, column, model_dataclass
from wreath.orm.types import Int64, Jsonb, Text


def _wide_model(size: int) -> type[Model]:
    namespace = {"__annotations__": {f"field_{i}": Mapped[int] for i in range(size)}}
    namespace.update(
        {f"field_{i}": column(Int64, primary_key=i == 0, default=i) for i in range(size)}
    )
    return type("ProjectionFixture", (Model,), namespace, table="projection_fixture")


@pytest.mark.parametrize("size", [32, 64, 1024])
@pytest.mark.parametrize("warm", [False, True])
def test_narrow_projection_visits_no_unselected_columns(monkeypatch, size, warm):
    model = _wide_model(size)
    names = [f"field_{size - 1}", "field_1", f"field_{size - 1}"]
    expected = model_dataclass(model, include=names) if warm else None
    visits = []

    class Columns(tuple):
        def __iter__(self):
            for item in super().__iter__():
                visits.append(item.python_name)
                yield item

    class ColumnMap(dict):
        def __iter__(self):
            raise AssertionError("narrow selection enumerated the column map")

        def keys(self):
            raise AssertionError("narrow selection enumerated column-map keys")

        def values(self):
            raise AssertionError("narrow selection enumerated column-map values")

        def items(self):
            raise AssertionError("narrow selection enumerated column-map items")

    monkeypatch.setattr(model, "__wreath_columns__", Columns(model.__wreath_columns__))
    monkeypatch.setattr(model, "__wreath_column_map__", ColumnMap(model.__wreath_column_map__))
    result = model_dataclass(model, include=iter(names))
    assert tuple(item.name for item in dataclasses.fields(result)) == ("field_1", names[0])
    assert dataclasses.asdict(result()) == {"field_1": 1, names[0]: size - 1}
    assert get_type_hints(result) == {"field_1": int, names[0]: int}
    assert model_dataclass(model, include=reversed(names)) is result
    if warm:
        assert result is expected
    assert visits == []
    all_names = tuple(f"field_{i}" for i in range(size))
    dense = model_dataclass(model, include=all_names)
    assert tuple(item.name for item in dataclasses.fields(dense)) == all_names
    assert visits == list(all_names)


@pytest.mark.parametrize("size", [2, 32, 1024])
def test_dense_and_default_shapes_share_the_existing_cache(size):
    model = _wide_model(size)
    names = tuple(f"field_{i}" for i in range(size))
    result = model_dataclass(model)
    assert tuple(item.name for item in dataclasses.fields(result)) == names
    assert dataclasses.asdict(result()) == dict(zip(names, range(size), strict=True))
    assert model_dataclass(model, include=reversed(names)) is result
    assert model_dataclass(model, exclude={"unknown"}) is result
    partial = model_dataclass(model, exclude={names[0]})
    assert model_dataclass(model, include=reversed(names[1:])) is partial
    assert tuple(item.name for item in dataclasses.fields(partial)) == names[1:]


def test_inherited_projection_preserves_order_defaults_annotations_and_cache():
    class Base(Model):
        id: Mapped[int] = column(Int64, primary_key=True)
        title: Mapped[str] = column(Text, default="base")
        labels: Mapped[dict] = column(Jsonb, default=dict)

    class Parent(Base, table="dto_resource_parent"):
        pass

    class Child(Base, table="dto_resource_child"):
        optional: Mapped[str] = column(Text, nullable=True)
        database: Mapped[str] = column(Text, server_default="'db'")

    base = model_dataclass(Parent, include={"title", "labels"}, name="Shared")
    child = model_dataclass(Child, include=["labels", "title"], name="Shared")
    assert child is not base
    assert model_dataclass(Child, exclude={"id", "optional", "database"}, name="Shared") is child
    assert tuple(item.name for item in dataclasses.fields(child)) == ("title", "labels")
    assert child.__name__ == "Shared"
    assert child.__module__ == Child.__module__
    assert get_type_hints(child) == {"title": str, "labels": dict}
    assert dataclasses.asdict(child()) == {"title": "base", "labels": {}}
    assert child().labels is not child().labels
    assert child(title="value").title == "value"
    single = model_dataclass(Child, include={"title"}, name="Single")
    assert dataclasses.asdict(single()) == {"title": "base"}
    assert get_type_hints(single) == {"title": str}
    assert model_dataclass(Child, include=["title"], name="Single") is single
    assert model_dataclass(Parent, include=["title"], name="Single") is not single
    optional = model_dataclass(Child, include={"database", "optional", "id"})
    assert dataclasses.asdict(optional()) == {"id": None, "optional": None, "database": None}
    assert get_type_hints(optional) == {
        "id": int | None,
        "optional": str | None,
        "database": str | None,
    }


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        (
            {"include": [0], "exclude": None},
            TypeError,
            "include= must contain non-empty column names",
        ),
        (
            {"include": ["unknown"], "exclude": [0]},
            TypeError,
            "exclude= must contain non-empty column names",
        ),
        (
            {"include": ["unknown"], "exclude": None},
            TypeError,
            "exclude= must be an iterable of column names, not None",
        ),
        (
            {"include": [], "exclude": ["unknown"]},
            ValueError,
            "include= and exclude= are mutually exclusive",
        ),
        (
            {"include": ["zzz", "aaa"], "name": "bad name"},
            ValueError,
            "ProjectionFixture has no column(s) aaa, zzz",
        ),
        (
            {"include": [], "name": "bad name"},
            ValueError,
            "a model dataclass must contain at least one column",
        ),
        (
            {"include": ["field_1"], "name": "bad name"},
            ValueError,
            "name='bad name' is not a Python identifier",
        ),
    ],
)
def test_projection_validation_order(kwargs, error, message):
    with pytest.raises(error) as caught:
        model_dataclass(_wide_model(32), **kwargs)
    assert str(caught.value) == message
