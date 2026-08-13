"""Executing a parsed document against the ORM and its resolvers.

Four properties matter here, and all four come from wreath owning the layers
underneath rather than sitting on top of them:

**No N+1, without a DataLoader.** A relationship selection is not resolved per
parent. The whole level is collected and handed to
`Session._load_relationship` -- the same batched select-in loader the ORM uses
for eager loading, with the identity map deduplicating. Custom resolvers are
batched by the same rule: a resolver sees the level, not one object.

**Chained resolvers, ordered by declaration.** A resolver that `requires` a
sibling field has that field resolved -- in batch -- before it runs, even if the
client did not select it. Dependencies are a topological sort over the level,
not an `await` buried in a loop.

**One authorization language, asked once.** Field access goes through the
authorizer the endpoint was given, as a `PolicyRequirement` -- the same value a
route's `@authorize` builds, so the shipped `CedarAuthorizer` serves both
surfaces -- carrying the field's policy resource. Decisions are cached per
request, so a field selected under three aliases is authorized once.

**Per-field timing.** Each resolve is a `RESOLVER` Flight phase carrying the
level's object count, so a slow field is distinguishable from a wide one.
"""

from __future__ import annotations

import inspect
from time import monotonic_ns as _monotonic_ns
from typing import Any

from .._auth.requirements import PolicyRequirement
from .._flight_markers import COV_PYTHON as _COV_PYTHON
from .._flight_markers import PH_RESOLVER as _PH_RESOLVER
from .._flight_markers import phase_marker as _phase_marker
from .._native import _core
from .ast import Document, Field, FragmentSpread, InlineFragment, Operation, Variable
from .resolvers import ResolverInfo, order_fields
from .schema import ObjectType, Schema, policy_resource

__all__ = ["ExecutionError", "execute", "execute_json"]

#: Returned instead of a value when a field is denied under `on_denied="null"`.
_DENIED = object()


class ExecutionError(Exception):
    """A document that parsed but cannot run against this schema."""

    def __init__(self, message: str, *, path: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.path = path


def _resolve_value(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, Variable):
        if value.name not in variables:
            raise ExecutionError(f"variable ${value.name} was not provided")
        return variables[value.name]
    if isinstance(value, list):
        return [_resolve_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_value(item, variables) for key, item in value.items()}
    return value


def _arguments(field: Field, variables: dict[str, Any]) -> dict[str, Any]:
    return {
        argument.name: _resolve_value(argument.value, variables)
        for argument in field.arguments
    }


def _flatten(
    selections: tuple[Any, ...], document: Document, type_name: str,
    seen: frozenset[str] = frozenset(),
) -> list[Field]:
    """Expand fragments into a flat field list for one object type.

    Fragment *cycles* are already impossible -- the parser refuses them -- so
    `seen` guards only against a spread reappearing through two branches, which
    would duplicate work rather than loop.
    """
    fields: list[Field] = []
    for selection in selections:
        if isinstance(selection, Field):
            fields.append(selection)
        elif isinstance(selection, InlineFragment):
            if selection.type_condition in (None, type_name):
                fields.extend(
                    _flatten(selection.selection_set.selections, document, type_name, seen)
                )
        elif isinstance(selection, FragmentSpread):
            if selection.name in seen:
                continue
            definition = document.fragments.get(selection.name)
            if definition is None:
                raise ExecutionError(f"unknown fragment {selection.name!r}")
            if definition.type_condition != type_name:
                continue
            fields.extend(
                _flatten(
                    definition.selection_set.selections, document, type_name,
                    seen | {selection.name},
                )
            )
    return fields


class _Run:
    """One execution. Carries the per-request caches so they cannot leak."""

    __slots__ = (
        "_action", "_authorizer", "_document", "_max_page_size", "_on_denied",
        "_policy_schema", "_policy_state", "_request", "_schema", "_session",
        "_variables",
    )

    def __init__(
        self, schema: Schema, document: Document, session: Any, *,
        variables: dict[str, Any], authorizer: Any, request: Any,
        max_page_size: int, on_denied: str, action: str,
        policy_schema: Any,
    ) -> None:
        self._action = action
        self._schema = schema
        self._document = document
        self._session = session
        self._variables = variables
        self._authorizer = authorizer
        self._request = request
        self._max_page_size = max_page_size
        self._on_denied = on_denied
        if authorizer is not None and policy_schema is None:
            policies = tuple(
                schema_field.policy
                for object_type in schema.types.values()
                for schema_field in object_type.fields.values()
            ) + tuple(root.policy for root in schema.roots.values()) + tuple(
                root.policy for root in schema.mutations.values()
            )
            policy_schema = _core.graphql_policy_schema(policies, policy_resource)
        self._policy_schema = policy_schema
        # Authorization is asked once per resource per request. The tri-state
        # array is native-owned and belongs to this execution, so aliases and
        # repeated levels share decisions without a Python dict or global cache.
        self._policy_state = (
            _core.graphql_policy_state(policy_schema)
            if self._authorizer is not None
            else None
        )

    async def _allowed(self, resource: str, path: tuple[str, ...]) -> bool:
        if self._authorizer is None:
            return True
        cached = _core.graphql_policy_cached(
            self._policy_schema, self._policy_state, resource
        )
        if cached >= 0:
            return bool(cached)
        # An `AuthorizationProvider` is asked with a `PolicyRequirement`, never
        # with a bare resource: that is the protocol every route already uses,
        # and handing the shipped `CedarAuthorizer` a `str` raised
        # `AttributeError` on the first field it was asked about. The resource
        # becomes a Cedar entity reference here for the same reason -- the
        # engine reads `User::"email"`, not `"User.email"`.
        decision = await self._authorizer.authorize(
            self._request,
            PolicyRequirement(
                action=self._action,
                resource=_core.graphql_policy_resource(
                    self._policy_schema, resource
                ),
            ),
        )
        allowed = bool(getattr(decision, "allowed", False))
        _core.graphql_policy_store(
            self._policy_schema, self._policy_state, resource, allowed
        )
        if not allowed and self._on_denied == "error":
            raise ExecutionError(
                getattr(decision, "reason", None) or f"not authorized to read {resource}",
                path=path,
            )
        return allowed

    async def _projection_allowed(
        self,
        object_type: ObjectType,
        fields: list[Field],
        root_name: str,
        *,
        root_resource: str | None = None,
    ) -> tuple[bool, bool]:
        """Authorize one plain projection, batching shared Cedar query inputs."""
        if self._authorizer is None:
            return True, True
        try:
            plan = _core.graphql_policy_prepare(
                self._policy_schema,
                self._policy_state,
                object_type.fields,
                fields,
                root_resource,
                root_name,
            )
        except KeyError as error:
            missing = error.args[0]
            raise ExecutionError(
                f"{object_type.name} has no field {missing!r}", path=(root_name,)
            ) from None
        resources = _core.graphql_policy_resources(plan)
        decisions: Any = ()
        authorize_resources = getattr(self._authorizer, "_authorize_resources", None)
        authorize_many = getattr(self._authorizer, "_authorize_many", None)
        if resources and callable(authorize_resources):
            native = callable(getattr(
                getattr(self._authorizer, "_engine", None),
                "_is_authorized_many_native",
                None,
            ))
            decisions = await authorize_resources(
                self._request,
                self._action,
                resources,
                stop_on_denied=self._on_denied == "error",
                **({"native": True} if native else {}),
            )
        elif len(resources) > 1 and callable(authorize_many):
            decisions = await authorize_many(
                self._request,
                tuple(
                    PolicyRequirement(action=self._action, resource=resource)
                    for resource in resources
                ),
                stop_on_denied=self._on_denied == "error",
            )
        elif resources:
            boundary = _core.graphql_policy_items(
                plan, self._action, PolicyRequirement
            )
            scalar_decisions = []
            for requirement, _path in boundary:
                scalar_decisions.append(
                    await self._authorizer.authorize(self._request, requirement)
                )
                if (
                    self._on_denied == "error"
                    and not bool(getattr(scalar_decisions[-1], "allowed", False))
                ):
                    break
            decisions = scalar_decisions
        denial = _core.graphql_policy_apply(
            plan,
            self._policy_state,
            decisions,
            self._on_denied == "error",
        )
        if denial is not None:
            reason, path, resource = denial
            raise ExecutionError(
                reason or f"not authorized to read {resource}", path=path
            )
        result = _core.graphql_policy_result(plan, self._policy_state)
        return bool(result & 1), bool(result & 2)

    async def _call_resolver(
        self, spec: Any, parents: list[Any], field: Field, path: tuple[str, ...],
        parent_type: str,
    ) -> list[Any]:
        """Invoke a resolver over the level, returning one value per parent."""
        info = ResolverInfo(
            request=self._request,
            session=self._session,
            arguments=_arguments(field, self._variables),
            path=path,
            parent_type=parent_type,
        )
        if spec.batch:
            result = spec.fn(parents, info)
            if inspect.isawaitable(result):
                result = await result
            values = list(result)
            if len(values) != len(parents):
                raise ExecutionError(
                    f"batch resolver {parent_type}.{spec.field_name} returned "
                    f"{len(values)} values for {len(parents)} objects",
                    path=path,
                )
            return values
        values = []
        for parent in parents:
            result = spec.fn(parent, info)
            if inspect.isawaitable(result):
                result = await result
            values.append(result)
        return values

    async def _project(
        self, instances: list[Any], object_type: ObjectType, fields: list[Field],
        path: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        """Project one level, batching every field and honouring `requires`."""
        marker = _phase_marker.get(None)
        if self._authorizer is None and marker is None:
            projected = _core.graphql_project_plain(
                instances, object_type.fields, fields
            )
            if projected is not None:
                return projected
        results = _core.graphql_new_results(instances)
        # Values a `requires` produced but the client did not select: computed
        # once, readable by dependents, and never emitted.
        hidden: dict[str, list[Any]] = {}

        specs = {
            name: schema_field.resolver
            for name, schema_field in object_type.fields.items()
            if schema_field.resolver is not None
        }
        ordered = order_fields(fields, specs, type_name=object_type.name)
        selected_names = {item.name for item in fields}

        async def ensure(name: str) -> None:
            """Resolve a required-but-unselected field into `hidden`."""
            if name in hidden or name in selected_names:
                return
            schema_field = object_type.fields.get(name)
            if schema_field is None:
                return
            if schema_field.resolver is not None:
                for dependency in schema_field.resolver.requires:
                    await ensure(dependency)
                hidden[name] = await self._call_resolver(
                    schema_field.resolver, instances,
                    Field(name=name, key=name), (*path, name), object_type.name,
                )
            elif schema_field.relationship is not None and instances:
                await self._session._load_relationship(
                    schema_field.relationship, instances, ()
                )

        for field in ordered:
            schema_field = object_type.fields.get(field.name)
            if schema_field is None:
                raise ExecutionError(
                    f"{object_type.name} has no field {field.name!r}", path=path
                )
            field_path = (*path, field.name)
            if not await self._allowed(schema_field.policy, field_path):
                _core.graphql_project_constant(results, field.key, None)
                continue
            started = _monotonic_ns() if marker is not None else 0

            if schema_field.resolver is not None:
                for dependency in schema_field.resolver.requires:
                    await ensure(dependency)
                values = await self._call_resolver(
                    schema_field.resolver, instances, field, field_path,
                    object_type.name,
                )
                if schema_field.resolver.type_name_out in self._schema.types:
                    values = await self._project_children(
                        values, schema_field, field, field_path
                    )
                _core.graphql_project_values(results, field.key, values)
                if marker is not None:
                    marker(
                        _PH_RESOLVER, len(instances), _COV_PYTHON,
                        _monotonic_ns() - started,
                    )
                continue

            attribute: str | None = None
            if schema_field.column is not None:
                attribute = schema_field.column.python_name
            else:
                attribute = schema_field.attribute
            if attribute is not None:
                _core.graphql_project_attribute(
                    results, instances, field.key, attribute
                )
                if marker is not None:
                    marker(_PH_RESOLVER, 0, _COV_PYTHON, _monotonic_ns() - started)
                continue

            relationship = schema_field.relationship
            if field.selection_set is None:
                raise ExecutionError(
                    f"{field.name!r} is an object and needs a selection set",
                    path=field_path,
                )
            # The whole level at once: one statement per relationship, never one
            # per parent. This is the N+1 fix, and it is the ORM's, not ours.
            if instances:
                await self._session._load_relationship(relationship, instances, ())
            if marker is not None:
                marker(
                    _PH_RESOLVER, len(instances), _COV_PYTHON,
                    _monotonic_ns() - started,
                )

            target_type = self._schema.type_of(schema_field.type_name)
            if target_type is None:
                raise ExecutionError(
                    f"unknown type {schema_field.type_name!r}", path=field_path
                )
            child_fields = _flatten(
                field.selection_set.selections, self._document, target_type.name
            )
            children, layout = _core.graphql_flatten_relationship(
                instances, relationship.index, schema_field.is_list
            )
            projected = await self._project(children, target_type, child_fields, field_path)
            _core.graphql_restore_layout(
                results, field.key, projected, layout, schema_field.is_list
            )

        return _core.graphql_finish_results(results)

    async def _project_children(
        self, values: list[Any], schema_field: Any, field: Field,
        path: tuple[str, ...],
    ) -> list[Any]:
        """Project a resolver's object-typed return values."""
        target_type = self._schema.type_of(schema_field.resolver.type_name_out)
        if target_type is None or field.selection_set is None:
            return values
        child_fields = _flatten(
            field.selection_set.selections, self._document, target_type.name
        )
        flat, layout = _core.graphql_flatten_values(
            values, schema_field.resolver.is_list
        )
        projected = await self._project(flat, target_type, child_fields, path)
        return _core.graphql_restore_values(
            projected, layout, schema_field.resolver.is_list
        )

    async def _root_instances(self, root: Any, field: Field) -> list[Any]:
        """Fetch the objects one root field selects."""
        arguments = _arguments(field, self._variables)
        if root.resolver is not None:
            info = ResolverInfo(
                request=self._request, session=self._session, arguments=arguments,
                path=(field.name,), parent_type="Query",
            )
            result = root.resolver.fn(info)
            if inspect.isawaitable(result):
                result = await result
            if root.is_list:
                return list(result or ())
            return [result] if result is not None else []

        query = root.spec.model_type.select()
        if root.is_list:
            # Always bounded. An unpaginated root field is a table scan any
            # client can ask for, so the ceiling applies even with no `limit`.
            limit = arguments.get("limit", self._max_page_size)
            try:
                limit = min(int(limit), self._max_page_size)
            except (TypeError, ValueError):
                raise ExecutionError("limit must be an integer") from None
            query = query.limit(max(limit, 0))
            offset = arguments.get("offset")
            if offset is not None:
                try:
                    query = query.offset(max(int(offset), 0))
                except (TypeError, ValueError):
                    raise ExecutionError("offset must be an integer") from None
            return await self._session.fetch(query)

        identifier = arguments.get("id")
        if identifier is None:
            raise ExecutionError(f"{field.name!r} needs an `id` argument")
        primary = root.spec.primary_key
        if len(primary) != 1:
            raise ExecutionError(
                f"{root.type_name} has a composite primary key and cannot be "
                "fetched by a single id"
            )
        column = getattr(root.spec.model_type, primary[0].python_name)
        coerced = primary[0].pg_type.coerce(
            int(identifier) if primary[0].pg_type.name.startswith("int") else identifier
        )
        return await self._session.fetch(query.where(column == coerced))

    async def run(self, operation: Operation, *, json_output: bool = False) -> dict[str, Any]:
        is_mutation = operation.operation == "mutation"
        lookup = self._schema.mutation if is_mutation else self._schema.root
        kind = "mutation" if is_mutation else "root"
        data: dict[str, Any] = {}
        selections = _flatten(
            operation.selection_set.selections, self._document,
            "Mutation" if is_mutation else "Query",
        )
        for field in selections:
            root = lookup(field.name)
            if root is None:
                raise ExecutionError(f"unknown {kind} field {field.name!r}")
            object_type = self._schema.type_of(root.type_name)
            child_fields = None
            projection_allowed = None
            if (
                json_output
                and _phase_marker.get(None) is None
                and object_type is not None
                and field.selection_set is not None
                and callable(getattr(self._authorizer, "_authorize_many", None))
            ):
                child_fields = _flatten(
                    field.selection_set.selections, self._document, object_type.name
                )
                root_allowed, projection_allowed = await self._projection_allowed(
                    object_type,
                    child_fields,
                    field.name,
                    root_resource=root.policy,
                )
            else:
                root_allowed = await self._allowed(root.policy, (field.name,))
            if not root_allowed:
                data[field.key] = None
                continue
            instances = await self._root_instances(root, field)
            if object_type is None or field.selection_set is None:
                # A scalar-returning root (a count, an ack) needs no projection.
                value: Any = instances if root.is_list else (
                    instances[0] if instances else None
                )
                data[field.key] = value
                continue
            if child_fields is None:
                child_fields = _flatten(
                    field.selection_set.selections, self._document, object_type.name
                )
            if json_output and _phase_marker.get(None) is None:
                # Authorization decides whether a field may materialize; it
                # does not require materializing the allowed values.  Once all
                # selected fields are allowed, keep the plain-object projection
                # native-owned through JSON egress just as an unprotected query
                # does.  If the native projector declines a resolver or
                # relationship, the decisions stay in this run's cache and the
                # general executor below observes exactly the same policy result.
                if projection_allowed is None:
                    _root_allowed, projection_allowed = await self._projection_allowed(
                        object_type, child_fields, field.name
                    )
                if projection_allowed:
                    projection = _core.graphql_project_json(
                        instances, object_type.fields, child_fields, root.is_list
                    )
                    if projection is not None:
                        data[field.key] = projection
                        continue
            projected = await self._project(
                instances, object_type, child_fields, (field.name,)
            )
            data[field.key] = projected if root.is_list else (
                projected[0] if projected else None
            )
        return data


async def execute(
    schema: Schema,
    document: Document,
    session: Any,
    *,
    operation_name: str | None = None,
    variables: dict[str, Any] | None = None,
    authorizer: Any = None,
    request: Any = None,
    max_page_size: int = 100,
    on_denied: str = "error",
    action: str = "read",
    policy_schema: Any = None,
) -> dict[str, Any]:
    """Run `document` against `schema` on `session`, returning the data map."""
    operation: Operation = document.operation(operation_name)
    if operation.operation not in ("query", "mutation"):
        raise ExecutionError("only query and mutation operations are served")

    supplied = dict(variables or {})
    for definition in operation.variables:
        if definition.name not in supplied:
            if definition.has_default:
                supplied[definition.name] = definition.default
            elif definition.non_null:
                raise ExecutionError(f"variable ${definition.name} is required")

    return await _Run(
        schema, document, session,
        variables=supplied, authorizer=authorizer, request=request,
        max_page_size=max_page_size, on_denied=on_denied, action=action,
        policy_schema=policy_schema,
    ).run(operation)


async def execute_json(
    schema: Schema,
    document: Document,
    session: Any,
    *,
    operation_name: str | None = None,
    variables: dict[str, Any] | None = None,
    authorizer: Any = None,
    request: Any = None,
    max_page_size: int = 100,
    on_denied: str = "error",
    action: str = "read",
    policy_schema: Any = None,
) -> bytes:
    """Run one operation and encode its GraphQL data envelope."""
    from .._json import dumps

    operation: Operation = document.operation(operation_name)
    if operation.operation not in ("query", "mutation"):
        raise ExecutionError("only query and mutation operations are served")

    supplied = dict(variables or {})
    for definition in operation.variables:
        if definition.name not in supplied:
            if definition.has_default:
                supplied[definition.name] = definition.default
            elif definition.non_null:
                raise ExecutionError(f"variable ${definition.name} is required")

    data = await _Run(
        schema, document, session,
        variables=supplied, authorizer=authorizer, request=request,
        max_page_size=max_page_size, on_denied=on_denied, action=action,
        policy_schema=policy_schema,
    ).run(operation, json_output=True)
    return dumps({"data": data})
