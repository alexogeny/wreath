"""A GraphQL schema derived from the ORM registry.

Types are not hand-written. They are read from the same ``ModelSpec`` the SQL
compiler, the OpenAPI generator, and typegen read, so the GraphQL surface cannot
drift from the REST surface or from the database -- there is one source of truth
and three renderings of it.

Each exposed model contributes:

- an object type with a field per column and a field per relationship,
- a singular root field (``user(id: 1)``),
- a plural root field (``users(limit: 20, offset: 0)``).

Relationship fields are the reason this is worth owning rather than bolting a
generic GraphQL library on: they resolve through the session's batched
select-in loader, so the N+1 problem every other stack solves with a DataLoader
layer is already solved one level down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["ObjectType", "Schema", "SchemaField", "build_schema"]

#: PostgreSQL type name -> GraphQL scalar. Anything unlisted becomes `String`,
#: which is lossless for output because the JSON encoder stringifies it anyway.
#:
#: `DateTime`/`Date` are named rather than left as `String` because the SDL is
#: the contract a client generates from: `String` says nothing, while a named
#: scalar tells a code generator to parse it and tells a human what shape to
#: send. The wire form is unchanged -- an ISO-8601 string either way -- so this
#: costs an existing client nothing.
_SCALARS = {
    "bool": "Boolean",
    "int2": "Int",
    "int4": "Int",
    "int8": "Int",
    "float4": "Float",
    "float8": "Float",
    "text": "String",
    "varchar": "String",
    "uuid": "ID",
    "date": "Date",
    "timestamp": "DateTime",
    "timestamptz": "DateTime",
    "json": "JSON",
    "jsonb": "JSON",
    "bytea": "String",
}

#: The scalars above that GraphQL does not define itself, so the SDL has to
#: declare them. Everything else is a built-in.
_CUSTOM_SCALARS = frozenset({"JSON", "DateTime", "Date"})


@dataclass(frozen=True, slots=True)
class SchemaField:
    """One field on an object type.

    Exactly one of ``column``, ``relationship``, or ``resolver`` is set; that is
    what the executor dispatches on.
    """

    name: str
    type_name: str
    non_null: bool
    is_list: bool
    #: Set for a relationship field; None otherwise.
    relationship: Any = None
    #: Set for a column field; None otherwise.
    column: Any = None
    #: Set for a custom/computed field; None otherwise.
    resolver: Any = None
    #: Authorization resource for this field. Defaults to "Type.field" at
    #: build time, so a policy can always be written without configuring one.
    policy: str = ""
    #: Complexity weight. A field that fans out or calls a service can declare
    #: that it costs more than a column read, so `max_complexity` bounds work
    #: rather than merely counting selections.
    cost: int = 1


@dataclass(frozen=True, slots=True)
class ObjectType:
    name: str
    spec: Any
    fields: dict[str, SchemaField] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RootField:
    """A queryable entry point.

    ``spec`` is the backing ModelSpec for a derived root; ``resolver`` is set
    instead for a custom root field, which need not correspond to a table.
    """

    name: str
    type_name: str
    is_list: bool
    spec: Any = None
    resolver: Any = None
    policy: str = ""
    cost: int = 1


@dataclass(frozen=True, slots=True)
class Schema:
    registry: Any
    types: dict[str, ObjectType]
    roots: dict[str, RootField]
    mutations: dict[str, RootField] = field(default_factory=dict)

    def root(self, name: str) -> RootField | None:
        return self.roots.get(name)

    def mutation(self, name: str) -> RootField | None:
        return self.mutations.get(name)

    def type_of(self, name: str) -> ObjectType | None:
        return self.types.get(name)

    def sdl(self) -> str:
        """The schema in GraphQL SDL, for tooling and for a `/graphql` GET."""
        lines: list[str] = []
        # A custom scalar has to be declared or the document is not valid SDL,
        # and a client generator will reject it. Only the ones actually used are
        # emitted, so a schema with no timestamps carries no `scalar DateTime`.
        used = sorted(
            {
                schema_field.type_name
                for object_type in self.types.values()
                for schema_field in object_type.fields.values()
                if schema_field.type_name in _CUSTOM_SCALARS
            }
        )
        if used:
            lines.extend(f"scalar {name}" for name in used)
            lines.append("")
        for object_type in self.types.values():
            lines.append(f"type {object_type.name} {{")
            for schema_field in object_type.fields.values():
                lines.append(f"  {schema_field.name}: {_render(schema_field)}")
            lines.append("}")
            lines.append("")
        lines.append("type Query {")
        for root in self.roots.values():
            lines.append(f"  {_render_root(root)}")
        lines.append("}")
        if self.mutations:
            lines.append("")
            lines.append("type Mutation {")
            for root in self.mutations.values():
                lines.append(f"  {_render_root(root)}")
            lines.append("}")
        return "\n".join(lines)


def _render_root(root: RootField) -> str:
    """One root field's SDL line.

    A custom root's arguments are the resolver's business, not the schema's, so
    they are rendered open-ended rather than invented here.
    """
    if root.resolver is not None:
        rendered = f"[{root.type_name}!]!" if root.is_list else root.type_name
        return f"{root.name}: {rendered}"
    if root.is_list:
        return f"{root.name}(limit: Int, offset: Int): [{root.type_name}!]!"
    return f"{root.name}(id: ID!): {root.type_name}"


def _render(schema_field: SchemaField) -> str:
    inner = schema_field.type_name
    if schema_field.is_list:
        return f"[{inner}!]!"
    return f"{inner}!" if schema_field.non_null else inner


def _plural(name: str) -> str:
    lowered = name[0].lower() + name[1:]
    if lowered.endswith(("s", "x", "z", "ch", "sh")):
        return lowered + "es"
    if lowered.endswith("y") and lowered[-2:-1] not in "aeiou":
        return lowered[:-1] + "ies"
    return lowered + "s"


def build_schema(registry: Any, models: list[Any] | None = None) -> Schema:
    """Build a schema from ``registry``, optionally narrowed to ``models``.

    **Exposure is opt-in when ``models`` is given**, and that is the intended
    use: a registry holds every table the application has, including ones with
    no business being queryable from the internet. Passing None exposes them
    all, which is convenient in development and rarely right in production.
    """
    specs = []
    if models is None:
        specs = list(getattr(registry, "_specs", {}).values())
    else:
        specs = [registry.spec_for(model) for model in models]

    types: dict[str, ObjectType] = {}
    for spec in specs:
        name = spec.model_type.__name__
        object_type = ObjectType(name=name, spec=spec, fields={})
        for column in spec.columns:
            object_type.fields[column.python_name] = SchemaField(
                name=column.python_name,
                type_name=_SCALARS.get(column.pg_type.name, "String"),
                non_null=not column.nullable,
                is_list=False,
                column=column,
                policy=f"{name}.{column.python_name}",
            )
        types[name] = object_type

    exposed = {object_type.spec for object_type in types.values()}
    for object_type in types.values():
        for relationship in object_type.spec.relationships:
            target = relationship.target
            if target not in exposed:
                # A relationship to a model that was not exposed is left off the
                # schema rather than exposed transitively -- otherwise narrowing
                # `models` would not actually narrow what is reachable.
                continue
            object_type.fields[relationship.name] = SchemaField(
                name=relationship.name,
                type_name=target.model_type.__name__,
                non_null=False,
                is_list=relationship.cardinality == "many",
                relationship=relationship,
                policy=f"{object_type.name}.{relationship.name}",
                # A relationship fans out a whole level, so it is worth more
                # than a column read when `max_complexity` is doing its job.
                cost=5,
            )

    roots: dict[str, RootField] = {}
    for object_type in types.values():
        singular = object_type.name[0].lower() + object_type.name[1:]
        roots[singular] = RootField(
            singular, object_type.name, False, object_type.spec,
            policy=f"Query.{singular}",
        )
        plural = _plural(object_type.name)
        roots[plural] = RootField(
            plural, object_type.name, True, object_type.spec,
            policy=f"Query.{plural}", cost=10,
        )

    return Schema(registry=registry, types=types, roots=roots)
