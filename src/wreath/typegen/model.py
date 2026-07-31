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
    # `wreath.pagination.Page[T]`, carrying the element type as its one
    # argument. A distinct kind rather than a `reference`, because every target
    # must render it as *wreath's own* `Page` -- a generated near-copy
    # type-checks and behaves identically until someone passes one to a
    # function annotated with the real thing.
    "page",
    # `wreath.geospatial.Coordinate`. A distinct kind rather than a `reference`
    # for the same reason as `page`: the Python target must annotate the *real*
    # type, and the wire shape is an object with named `lat`/`lon` so a client
    # cannot transpose the pair -- which is the trap the constructor refuses
    # positionally, closed again at every other surface.
    "coordinate",
]


@dataclass(frozen=True, slots=True)
class TypeRef:
    """A structural type. `reference` names a model in `ApiModel.models`."""

    kind: TypeKind
    name: str | None = None
    arguments: tuple[TypeRef, ...] = ()
    #: For `literal` kinds, the exact permitted values.
    literals: tuple[str | int | float | bool | None, ...] = ()


@dataclass(frozen=True, slots=True)
class Field:
    wire_name: str
    type: TypeRef
    required: bool
    description: str | None = None
    examples: tuple[object, ...] = ()
    gt: int | float | object | None = None
    ge: int | float | object | None = None
    lt: int | float | object | None = None
    le: int | float | object | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    unique_items: bool = False


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
    #: Behaviours the tape declared for this operation, from the closed
    #: `wreath.middleware.base.BEHAVIOURS` vocabulary. A target may emit a
    #: runtime for each; a target that ignores them is still correct, just
    #: less capable.
    behaviours: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SeriesMeasure:
    """One named quantity on a calculated view.

    The name is load-bearing in three places at once: it keys the series in the
    envelope, it names the field in the generated TypeScript, and it is what a
    `fill=` refers to. That is why measures are named rather than positional --
    positional ones arrive in a component as `value_0`.

    `fills` says whether an absent bucket reads as a number or as `null`,
    which is the difference between `number[]` and `(number | null)[]` on the
    other side. A count fills with zero; an average of no rows is undefined and
    stays null, and a component that has to handle the gap should be made to.
    """

    name: str
    kind: str
    unit: str | None = None
    fills: bool = True


@dataclass(frozen=True, slots=True)
class SeriesShape:
    """A calculated view, typed for the client that will draw it.

    In the IR for the same reason `PermissionSet` is: the shape a chart
    endpoint returns is decided by a declaration on the server, and a component
    indexing `number[][]` by hand is a copy of that declaration which nothing
    keeps honest.
    """

    name: str
    #: `"series"` (a time axis) or `"aggregate"` (no time axis).
    form: str
    measures: tuple[SeriesMeasure, ...]
    #: The bucket unit for a series -- `"day"`, `"month"`. `None` for an
    #: aggregate, which has no time axis.
    bucket: str | None = None
    #: Whether `.by(...)` was declared: a grouped view returns several series
    #: per measure, an ungrouped one returns exactly one.
    grouped: bool = False
    #: Whether the declaration carries a prior period, and by which bucket.
    compares: str | None = None
    #: Whether an annotation layer was declared.
    events: bool = False


@dataclass(frozen=True, slots=True)
class PermissionSet:
    """The actions one resource type is authorized for, read off the routes.

    In the IR because the generated client should be typed on the *server's*
    vocabulary: a UI asking about an action the API does not enforce is a bug
    that ought to be a compile error, not a silent `false`.
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
    series: tuple[SeriesShape, ...] = ()


# Reusable singletons for the common scalar shapes.
UNKNOWN = TypeRef("unknown")
NULL = TypeRef("null")
BOOLEAN = TypeRef("boolean")
INTEGER = TypeRef("integer")
NUMBER = TypeRef("number")
STRING = TypeRef("string")

#: ISO-8601 strings, tagged with their OpenAPI `format` in `name`. A string
#: is never a reference, so `name` is free to carry the format -- which is
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
    "SeriesMeasure",
    "SeriesShape",
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
