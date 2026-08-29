"""A GraphQL schema derived from the ORM registry.

Types are not hand-written. They are read from the same `ModelSpec` the SQL
compiler, the OpenAPI generator, and typegen read, so the GraphQL surface cannot
drift from the REST surface or from the database -- there is one source of truth
and three renderings of it.

Each exposed model contributes:

- an object type with a field per column and a field per relationship,
- a singular root field (`user(id: 1)`),
- a plural root field (`users(limit: 20, offset: 0)`).

Relationship fields are the reason this is worth owning rather than bolting a
generic GraphQL library on: they resolve through the session's batched
select-in loader, so the N+1 problem every other stack solves with a DataLoader
layer is already solved one level down.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, is_dataclass
from typing import Any, get_args, get_origin, get_type_hints

from .._auth.cedar_engine import CedarParseError, EntityUid
from .._model_fields import dataclass_field_image

__all__ = ["ObjectType", "Schema", "SchemaField", "build_schema", "policy_resource"]

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


def policy_resource(policy: str) -> EntityUid:
    """The Cedar entity reference a GraphQL policy string names.

    A policy resource is written `Type.field` -- `User.email`, `Query.users`,
    `Mutation.createUser` -- and that is the string in the schema, in the error
    a denial produces, and in the documentation. It is **not** what an
    authorizer can be handed: `PolicyRequirement.resource` reaches
    `CedarPolicies` through `CedarAuthorizer`'s resource mapper, and the engine
    accepts an `EntityUid` or a `Type::"id"` string and raises on anything
    else. A bare `"User.email"` is neither, so the split happens here, once:
    the type is the entity type and the field is the id.

    That split is what makes the documented policies writable at all:

    ```cedar
    permit(principal, action == Action::"read", resource == User::"email");
    forbid(principal, action, resource is Mutation);   // every write, one clause
    ```

    A policy already written as a Cedar reference (`Billing::"read"`, or the
    bare `Billing::read`) is used verbatim, which is the seam for a resolver
    whose resource is not a field of anything.

    Raises:
        ValueError: `policy` is neither a Cedar entity reference nor a dotted
            `Type.field` name, so no engine could read it. Raised where the
            policy is *declared* rather than on the request that first selects
            the field -- the same reason `crud.Access.cedar` parses its resource
            at declaration.
    """
    try:
        return EntityUid.parse(policy)
    except CedarParseError:
        pass
    type_name, dot, field_name = policy.rpartition(".")
    if not dot or not type_name or not field_name:
        raise ValueError(
            f"{policy!r} is not a usable authorization resource; write it as "
            "`Type.field` (e.g. 'User.email') or as a Cedar entity reference "
            "(e.g. 'Billing::\"read\"'). A bare name cannot be evaluated by any "
            "Cedar engine."
        )
    return EntityUid(type_name, field_name)


@dataclass(frozen=True, slots=True)
class SchemaField:
    """One field on an object type.

    Exactly one of `column`, `attribute`, `relationship`, or `resolver` is set;
    that is what the executor dispatches on.
    """

    name: str
    type_name: str
    non_null: bool
    is_list: bool
    #: Set for a relationship field; None otherwise.
    relationship: Any = None
    #: Set for a column field; None otherwise.
    column: Any = None
    #: Set for a field projected directly from a registered dataclass.
    attribute: str | None = None
    #: Set for a custom/computed field; None otherwise.
    resolver: Any = None
    #: Authorization resource for this field. Defaults to "Type.field" at
    #: build time, so a policy can always be written without configuring one.
    policy: str = ""
    #: Complexity weight, charged against `max_complexity` by `cost.weigh`
    #: after the parse. A field that fans out or calls a service declares that
    #: it costs more than a column read, so the budget bounds *work* rather
    #: than merely counting selections. Additive: a list field does not
    #: multiply its children, because fan-out is what this number is for.
    cost: int = 1


@dataclass(frozen=True, slots=True)
class ObjectType:
    name: str
    spec: Any
    fields: dict[str, SchemaField] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RootField:
    """A queryable entry point.

    `spec` is the backing ModelSpec for a derived root; `resolver` is set
    instead for a custom root field, which need not correspond to a table.
    """

    name: str
    type_name: str
    is_list: bool
    spec: Any = None
    resolver: Any = None
    policy: str = ""
    #: Complexity weight, as on `SchemaField`. Derived roots declare more than
    #: 1 because a root is a query: `cost=10` for a list root and `cost=5` for
    #: a single-row one.
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


def _is_exposed(model_name: str, column_name: str, expose: frozenset[str]) -> bool:
    """Whether a column withheld by default was explicitly opted back in.

    Accepts `"Model.column"` and a bare `"column"`, the second so a name
    like `api_key` can be exposed once rather than per model.
    """
    return f"{model_name}.{column_name}" in expose or column_name in expose


def build_schema(
    registry: Any,
    models: list[Any] | None = None,
    *,
    expose: Iterable[str] = (),
    dataclasses: Iterable[type] = (),
) -> Schema:
    """Build a schema from `registry`, optionally narrowed to `models`.

    **Exposure is opt-in when `models` is given**, and that is the intended
    use: a registry holds every table the application has, including ones with
    no business being queryable from the internet. Passing None exposes them
    all, which is convenient in development and rarely right in production.

    **Columns whose names look sensitive are left out of the schema**, on the
    same rule and the same regex `wreath.crud.sensitive_fields` uses --
    `password`, `*_hash`, `token`, `secret`, `api_key`, and the rest.
    Both surfaces are generated from one `ModelSpec`, so it would be strange
    for the REST one to hide a password hash and the GraphQL one to answer
    `{ user { passwordHash } }`. Name a column in `expose` to put it back,
    which is the same deliberate, auditable act `crud_router(expose=...)`
    asks for.

    **Retrieval columns are left out too**, on the same argument and through the
    same function: `wreath.crud.retrieval_fields` reads the declared type rather
    than guessing from a name, so a `Vector` embedding and a `TsVector` are
    withheld here exactly as they are from a generated REST response. A
    `tsvector` is derived from columns already in the same selection, and a page
    of twenty `Vector(1536)` rows is thirty thousand floats a client did not want
    -- on a surface where the client, not the server, chooses the page. `expose`
    puts one back, and that is the whole of the difference between the two
    surfaces: `crud_router(expose=...)` also widens what may be *written*, and
    there is nothing here for it to widen, because no mutation is generated. A
    GraphQL write is a resolver somebody wrote, and it is that resolver's
    business what it accepts.
    """
    from ..crud import SENSITIVE_FIELD, retrieval_fields

    exposed_names = frozenset(expose)
    specs = []
    if models is None:
        specs = list(getattr(registry, "_specs", {}).values())
    else:
        specs = [registry.spec_for(model) for model in models]

    types: dict[str, ObjectType] = {}
    for spec in specs:
        name = spec.model_type.__name__
        object_type = ObjectType(name=name, spec=spec, fields={})
        # Asked once per model rather than re-derived per column, and asked of
        # `wreath.crud` rather than of the type system directly, so the two
        # generated surfaces cannot come to disagree about what a retrieval
        # column is.
        retrieval = retrieval_fields(spec.model_type)
        for column in spec.columns:
            withheld = (
                SENSITIVE_FIELD.search(column.python_name) is not None
                or column.python_name in retrieval
            )
            if withheld and not _is_exposed(name, column.python_name, exposed_names):
                continue
            object_type.fields[column.python_name] = SchemaField(
                name=column.python_name,
                type_name=_SCALARS.get(column.pg_type.name, "String"),
                non_null=not column.nullable,
                is_list=False,
                column=column,
                policy=f"{name}.{column.python_name}",
            )
        types[name] = object_type

    declared_dataclasses = tuple(dataclasses)
    for model in declared_dataclasses:
        if not isinstance(model, type) or not is_dataclass(model):
            raise TypeError(f"GraphQL dataclasses must contain dataclass types; got {model!r}")
        if model.__name__ in types:
            raise ValueError(f"GraphQL type {model.__name__!r} is already registered")
        hints = get_type_hints(model)
        object_type = ObjectType(name=model.__name__, spec=None, fields={})
        for item in dataclass_field_image(model, hints):
            annotation = item.annotation
            type_name, is_list, non_null = _dataclass_annotation(annotation)
            object_type.fields[item.python_name] = SchemaField(
                name=item.python_name,
                type_name=type_name,
                non_null=non_null,
                is_list=is_list,
                attribute=item.python_name,
                policy=f"{model.__name__}.{item.python_name}",
            )
        types[model.__name__] = object_type
    known_scalars = frozenset(
        {"String", "Int", "Float", "Boolean", "ID", "JSON", "Date", "DateTime"}
    )
    for model in declared_dataclasses:
        for schema_field in types[model.__name__].fields.values():
            if schema_field.type_name not in known_scalars and schema_field.type_name not in types:
                raise TypeError(
                    f"{model.__name__}.{schema_field.name} refers to GraphQL type "
                    f"{schema_field.type_name!r}, which is not registered"
                )

    exposed = frozenset(specs)
    for object_type in types.values():
        if object_type.spec is None:
            continue
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
        if object_type.spec is None:
            continue
        singular = object_type.name[0].lower() + object_type.name[1:]
        roots[singular] = RootField(
            singular,
            object_type.name,
            False,
            object_type.spec,
            policy=f"Query.{singular}",
        )
        plural = _plural(object_type.name)
        roots[plural] = RootField(
            plural,
            object_type.name,
            True,
            object_type.spec,
            policy=f"Query.{plural}",
            cost=10,
        )

    return Schema(registry=registry, types=types, roots=roots)


def _dataclass_annotation(annotation: Any) -> tuple[str, bool, bool]:
    """Map one native dataclass annotation to GraphQL shape metadata."""
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    non_null = True
    if type(None) in arguments:
        remaining = tuple(item for item in arguments if item is not type(None))
        non_null = False
        if len(remaining) != 1:
            raise TypeError(f"unsupported GraphQL union annotation {annotation!r}")
        annotation = remaining[0]
        origin = get_origin(annotation)
        arguments = get_args(annotation)
    is_list = origin in (list, tuple)
    if is_list:
        if len(arguments) != 1:
            raise TypeError(f"GraphQL list annotation needs one item type: {annotation!r}")
        annotation = arguments[0]
    scalars = {
        str: "String",
        int: "Int",
        float: "Float",
        bool: "Boolean",
        uuid.UUID: "ID",
        dt.date: "Date",
        dt.datetime: "DateTime",
        dict: "JSON",
        Any: "JSON",
    }
    type_name = scalars.get(annotation)
    if isinstance(annotation, type) and is_dataclass(annotation):
        type_name = annotation.__name__
    if type_name is None:
        raise TypeError(f"unsupported GraphQL dataclass annotation {annotation!r}")
    return type_name, is_list, non_null
