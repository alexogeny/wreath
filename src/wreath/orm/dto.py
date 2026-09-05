"""Dataclass projections over mapped models.

Request and response models often repeat a subset of an ORM model's columns.
`model_dataclass` makes that subset explicit while keeping the result an
ordinary dataclass understood by Wreath's binding, OpenAPI, and typegen layers:

```python
    LlamaCreate = model_dataclass(
        Llama,
        include={"name", "birth_date"},
        name="LlamaCreate",
    )
```

The projection is compiled when the declaration executes, never while binding a
request. Its cache belongs to the mapped model itself, so two declarations of
the same shape return the same type without a process-global registry.
"""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Iterable
from typing import Any

from .fields import MISSING, Mapped
from .model import Model

__all__ = ["model_dataclass"]

_CACHE_ATTRIBUTE = "__wreath_dataclass_projections__"


def _names(value: Iterable[str] | None, label: str) -> frozenset[str] | None:
    if value is None:
        return None
    names = frozenset(value)
    if any(not isinstance(item, str) or not item for item in names):
        raise TypeError(f"{label}= must contain non-empty column names")
    return names


def _column_annotations(model: type[Model], selected: tuple[str, ...]) -> dict[str, Any]:
    try:
        annotations = typing.get_type_hints(model, include_extras=True)
    except NameError, TypeError:
        annotations = {}
        for base in reversed(model.__mro__):
            annotations.update(getattr(base, "__annotations__", {}))
    resolved: dict[str, Any] = {}
    for column_name in selected:
        annotation = annotations.get(column_name, Any)
        if typing.get_origin(annotation) is Mapped:
            annotation = typing.get_args(annotation)[0]
        resolved[column_name] = annotation
    return resolved


def _field(column: Any, annotation: Any) -> tuple[Any, ...]:
    default = column.default
    if default is not MISSING:
        if callable(default):
            return (column.python_name, annotation, dataclasses.field(default_factory=default))
        return (column.python_name, annotation, dataclasses.field(default=default))
    may_be_omitted = (
        column.nullable
        or column.server_default is not None
        or column.primary_key
        or column.generated
    )
    if may_be_omitted:
        if annotation is not Any:
            annotation = annotation | None
        return (column.python_name, annotation, dataclasses.field(default=None))
    return (column.python_name, annotation)


def model_dataclass(
    model: type[Model],
    *,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] = (),
    name: str | None = None,
) -> type:
    """Compile an ordinary dataclass from selected mapped columns.

    `include` and `exclude` are mutually exclusive. Unknown names are
    refused rather than ignored, because a stale projection silently omitting a
    renamed field changes the wire schema. Fields retain model order and Python
    defaults; nullable, generated, primary-key, and server-default columns may
    be omitted and default to `None`. All fields are keyword-only, matching
    Wreath's request binder and avoiding declaration-order restrictions.
    """
    if not isinstance(model, type) or not issubclass(model, Model):
        raise TypeError("model_dataclass() needs a mapped wreath.orm.Model")
    if not model.__wreath_columns__:
        raise TypeError("model_dataclass() needs a concrete mapped model")
    included = _names(include, "include")
    excluded = _names(exclude, "exclude")
    if excluded is None:
        raise TypeError("exclude= must be an iterable of column names, not None")
    if included is not None and excluded:
        raise ValueError("include= and exclude= are mutually exclusive")
    column_map = model.__wreath_column_map__
    if included is not None:
        requested = included
        unknown = {column_name for column_name in included if column_name not in column_map}
        if unknown:
            raise ValueError(f"{model.__name__} has no column(s) {', '.join(sorted(unknown))}")
    else:
        requested = column_map.keys() - excluded
    if not requested:
        raise ValueError("a model dataclass must contain at least one column")
    type_name = name or f"{model.__name__}Data"
    if not isinstance(type_name, str) or not type_name.isidentifier():
        raise ValueError(f"name={type_name!r} is not a Python identifier")
    if included is not None and len(included) * 4 <= len(column_map):
        selected = tuple(sorted(included, key=lambda column_name: column_map[column_name].index))
    else:
        selected = tuple(
            column.python_name
            for column in model.__wreath_columns__
            if column.python_name in requested
        )
    cache = model.__dict__.get(_CACHE_ATTRIBUTE)
    if cache is None:
        cache = {}
        setattr(model, _CACHE_ATTRIBUTE, cache)
    key = (selected, type_name)
    cached = cache.get(key)
    if cached is not None:
        return cached
    annotations = _column_annotations(model, selected)
    fields = [
        _field(model.__wreath_column_map__[column_name], annotations[column_name])
        for column_name in selected
    ]
    projected = dataclasses.make_dataclass(
        type_name,
        fields,
        kw_only=True,
        slots=True,
        module=model.__module__,
    )
    projected.__doc__ = f"Dataclass projection of {model.__qualname__}."
    cache[key] = projected
    return projected
