"""Folding the GraphQL schema into the shared typegen IR.

This is the reason to own GraphQL in-tree rather than mount a library beside
the app. A GraphQL type and a REST response body describing the same model are
the same type, so they become **one** entry in ``ApiModel.models`` and one
TypeScript interface. A consumer gets ``useGetUser()`` (REST) and
``userQuery()`` (GraphQL) returning the identical ``User`` -- no duplicate
generated types, no drift, no second codegen pipeline to keep in step.

Models are contributed under the *same* names the REST inspector uses, and
merging is by name, so whichever surface is generated first wins and the other
reuses it.
"""

from __future__ import annotations

from ..typegen.model import ApiModel, Field, Model, Operation, Parameter, TypeRef
from .schema import Schema

__all__ = ["graphql_models", "graphql_operations", "merge_into"]

_SCALAR_REFS = {
    "Boolean": TypeRef("boolean"),
    "Int": TypeRef("integer"),
    "Float": TypeRef("number"),
    "String": TypeRef("string"),
    "ID": TypeRef("string"),
    "JSON": TypeRef("unknown"),
}


def _type_ref(type_name: str, *, is_list: bool) -> TypeRef:
    inner = _SCALAR_REFS.get(type_name) or TypeRef("reference", name=type_name)
    return TypeRef("array", arguments=(inner,)) if is_list else inner


def graphql_models(schema: Schema) -> tuple[Model, ...]:
    """One IR model per GraphQL object type."""
    models: list[Model] = []
    for object_type in schema.types.values():
        fields = tuple(
            Field(
                wire_name=schema_field.name,
                type=_type_ref(schema_field.type_name, is_list=schema_field.is_list),
                required=schema_field.non_null or schema_field.is_list,
            )
            for schema_field in object_type.fields.values()
        )
        models.append(Model(name=object_type.name, fields=fields))
    return tuple(models)


def graphql_operations(schema: Schema, *, path: str = "/graphql") -> tuple[Operation, ...]:
    """One IR operation per root field.

    Every GraphQL request is a POST to one endpoint, so the operations differ by
    their *body*, not their path. Encoding them as distinct IR operations is what
    lets the TypeScript target emit a named, typed function per root field
    instead of one stringly-typed `graphql(query)` escape hatch.
    """
    operations: list[Operation] = []
    for root in schema.roots.values():
        parameters: tuple[Parameter, ...]
        if root.is_list:
            parameters = (
                Parameter("limit", "limit", "query", TypeRef("integer"), False),
                Parameter("offset", "offset", "query", TypeRef("integer"), False),
            )
        else:
            parameters = (
                Parameter("id", "id", "query", TypeRef("string"), True),
            )
        operations.append(
            Operation(
                id=f"graphql{root.name[0].upper()}{root.name[1:]}",
                method="POST",
                path=path,
                parameters=parameters,
                request_body=TypeRef("unknown"),
                request_body_media_type="application/json",
                response_body=_type_ref(root.type_name, is_list=root.is_list),
                tags=("graphql",),
                summary=f"GraphQL root field `{root.name}`",
            )
        )
    return tuple(operations)


def merge_into(api: ApiModel, schema: Schema, *, path: str = "/graphql") -> ApiModel:
    """Return ``api`` with the GraphQL surface folded in.

    Models are merged **by name**: a type the REST inspector already emitted is
    kept as-is rather than duplicated, which is what keeps one `User` interface
    serving both surfaces. Operation ids are prefixed `graphql*`, so they cannot
    collide with a derived REST id.
    """
    existing = {model.name for model in api.models}
    merged_models = (*api.models, *(m for m in graphql_models(schema) if m.name not in existing))
    existing_ops = {operation.id for operation in api.operations}
    merged_operations = (
        *api.operations,
        *(op for op in graphql_operations(schema, path=path) if op.id not in existing_ops),
    )
    return ApiModel(
        title=api.title,
        version=api.version,
        models=merged_models,
        operations=merged_operations,
    )
