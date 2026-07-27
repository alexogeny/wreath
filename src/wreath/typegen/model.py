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
class PermissionSet:
    """The actions one resource type is authorized for, read off the routes.

    In the IR because the generated client should be typed on the *server's*
    vocabulary: a UI asking about an action the API does not enforce is a bug
    that ought to be a compile error, not a silent ``false``.
    """

    resource_type: str
    actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApiModel:
    title: str
    version: str
    models: tuple[Model, ...] = ()
    operations: tuple[Operation, ...] = ()
    permissions: tuple[PermissionSet, ...] = ()


# Reusable singletons for the common scalar shapes.
UNKNOWN = TypeRef("unknown")
NULL = TypeRef("null")
BOOLEAN = TypeRef("boolean")
INTEGER = TypeRef("integer")
NUMBER = TypeRef("number")
STRING = TypeRef("string")

#: ISO-8601 strings, tagged with their OpenAPI `format` in ``name``. A string
#: is never a reference, so ``name`` is free to carry the format -- which is
#: what lets one declaration reach the schema, the TypeScript, and the GraphQL
#: scalar saying the same thing. Defined here so the REST and GraphQL sides of
#: typegen cannot drift to two different spellings.
DATE_TIME = TypeRef("string", "date-time")
DATE = TypeRef("string", "date")


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
    "DATE",
    "DATE_TIME",
    "Diagnostic",
    "Field",
    "Model",
    "Operation",
    "Parameter",
    "PermissionSet",
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
