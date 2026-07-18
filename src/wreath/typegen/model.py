"""The canonical typegen intermediate representation.

One semantic model feeds both OpenAPI and every consumer target (TypeScript
models, the fetch client, React Query hooks). Python owns all annotation
analysis; the records here are frozen, hashable, and contain no live Python
objects, so a renderer -- pure or native -- only ever sees normalized data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TypeKind = Literal[
    "unknown",
    "null",
    "boolean",
    "integer",
    "number",
    "string",
    "array",
    "tuple",
    "record",
    "union",
    "literal",
    "reference",
]


@dataclass(frozen=True, slots=True)
class TypeRef:
    """A structural type. ``reference`` names a model in ``ApiModel.models``."""

    kind: TypeKind
    name: str | None = None
    arguments: tuple[TypeRef, ...] = ()
    #: For ``literal`` kinds, the exact permitted values.
    literals: tuple[str | int | float | bool | None, ...] = ()


@dataclass(frozen=True, slots=True)
class Field:
    wire_name: str
    type: TypeRef
    required: bool


@dataclass(frozen=True, slots=True)
class Model:
    name: str
    fields: tuple[Field, ...]


@dataclass(frozen=True, slots=True)
class Parameter:
    python_name: str
    wire_name: str
    location: Literal["path", "query", "header", "cookie"]
    type: TypeRef
    required: bool


@dataclass(frozen=True, slots=True)
class Operation:
    id: str
    method: str
    path: str
    parameters: tuple[Parameter, ...]
    request_body: TypeRef | None
    request_body_media_type: str | None
    response_body: TypeRef
    tags: tuple[str, ...] = ()
    summary: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ApiModel:
    title: str
    version: str
    models: tuple[Model, ...] = ()
    operations: tuple[Operation, ...] = ()


# Reusable singletons for the common scalar shapes.
UNKNOWN = TypeRef("unknown")
NULL = TypeRef("null")
BOOLEAN = TypeRef("boolean")
INTEGER = TypeRef("integer")
NUMBER = TypeRef("number")
STRING = TypeRef("string")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A source-anchored problem, kept out of renderer input on purpose."""

    message: str
    operation_id: str | None = None
    method: str | None = None
    path: str | None = None
    location: str | None = None
    annotation: str | None = None

    def render(self) -> str:
        where = []
        if self.method and self.path:
            where.append(f"{self.method} {self.path}")
        if self.operation_id:
            where.append(f"operation {self.operation_id!r}")
        if self.location:
            where.append(self.location)
        if self.annotation:
            where.append(f"annotation {self.annotation}")
        prefix = " (" + ", ".join(where) + ")" if where else ""
        return f"{self.message}{prefix}"


class TypegenError(Exception):
    """Raised when a model cannot be built under the active strictness."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__(
            "typegen could not process the application:\n"
            + "\n".join(f"  - {diagnostic.render()}" for diagnostic in diagnostics)
        )


__all__ = [
    "ApiModel",
    "Diagnostic",
    "Field",
    "Model",
    "Operation",
    "Parameter",
    "TypeKind",
    "TypeRef",
    "TypegenError",
    "UNKNOWN",
    "NULL",
    "BOOLEAN",
    "INTEGER",
    "NUMBER",
    "STRING",
]
