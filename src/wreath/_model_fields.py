"""One declarative field image for Python model consumers.

Binding, OpenAPI/typegen, GraphQL, and form binding project different wire
types from a dataclass. They must nevertheless agree on the source facts:
resolved annotation, declaration order, default, and requiredness. This module
owns those facts while each consumer retains its domain-specific projection.
"""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_DECLARED = object()


@dataclass(frozen=True, slots=True)
class DeclaredField:
    python_name: str
    annotation: Any
    required: bool
    default: Any
    metadata: tuple[Any, ...]


def dataclass_field_image(
    model: type,
    hints: Mapping[str, Any],
    *,
    fallback: Any = _DECLARED,
) -> tuple[DeclaredField, ...]:
    """Compile declaration-order dataclass facts from already-resolved hints."""
    result: list[DeclaredField] = []
    for field in dataclasses.fields(model):
        annotation = hints.get(field.name, field.type if fallback is _DECLARED else fallback)
        metadata: tuple[Any, ...] = ()
        if typing.get_origin(annotation) is typing.Annotated:
            _base, *items = typing.get_args(annotation)
            metadata = tuple(items)
        required = (
            field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING
        )
        default = field.default if field.default is not dataclasses.MISSING else None
        result.append(DeclaredField(field.name, annotation, required, default, metadata))
    return tuple(result)


__all__: tuple[str, ...] = ()
