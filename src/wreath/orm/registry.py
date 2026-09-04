"""The application-owned model registry.

A registry binds a set of models to one database. It is the only place that
resolves relationships, and the only owner of compiled query plans. Two
applications can register the same model classes against different databases
without sharing sessions, identity objects, or cached plans.
"""

from __future__ import annotations

import threading
from math import isfinite
from sys import getsizeof
from typing import Any, Literal

from ..kv import KV
from ._generated import render_generation
from ._index_predicate import render_predicate
from .errors import DeclarationError, RegistryError
from .expressions import ColumnExpr
from .fields import Column
from .model import Model, ModelMeta
from .relations import Relationship
from .schema import (
    ColumnRef,
    ColumnSpec,
    ModelSpec,
    RelationshipSpec,
    SchemaMode,
    SchemaRef,
    StorageSpec,
    fingerprint_model,
    fingerprint_registry,
    fingerprint_registry_template,
)
from .table import Index
from .types import GeneratedType

ValidateSchema = Literal["off", "warn", "error"]
_VALIDATE_MODES = frozenset({"off", "warn", "error"})


def _cache_entry_bytes(shape_key: bytes, plan: Any) -> int:
    """Price allocations directly retained by one cached plan.

    Registry metadata referenced by a plan is shared application state, so only
    the plan, its key, and its immediate slot values belong to this budget.
    """
    size = getsizeof(shape_key) + getsizeof(plan)
    for slot in getattr(type(plan), "__slots__", ()):
        if isinstance(slot, str) and hasattr(plan, slot):
            size += getsizeof(getattr(plan, slot))
    return size


class Registry:
    """Compiled models for one database."""

    __slots__ = (
        "identity_map_warn_at",
        "statement_timeout",
        "_by_name",
        "_by_table",
        "_cache",
        "_flight_model_ids",
        "_lock",
        "_model_order",
        "_prepared_shapes",
        "_specs",
        "database",
        "default_opclasses",
        "fingerprint",
        "query_cache_bytes",
        "query_cache_size",
        "schema_mode",
        "specs",
        "template_fingerprint",
        "deployment_fingerprint",
        "validate_schema",
    )

    def __init__(
        self,
        database: Any,
        models: Any,
        *,
        validate_schema: ValidateSchema = "error",
        query_cache_size: int = 512,
        query_cache_bytes: int = 8 * 1024 * 1024,
        schema_mode: SchemaMode | None = None,
        statement_timeout: float | None = None,
        identity_map_warn_at: int | None = None,
    ) -> None:
        #: Default seconds a statement may run, applied by every `Session` this
        #: registry opens. None leaves it to the server's own setting, which in
        #: a default PostgreSQL is "forever" -- one pathological query then holds
        #: a pooled connection until somebody notices.
        if statement_timeout is not None:
            if type(statement_timeout) not in (int, float):
                raise RegistryError("statement_timeout must be a finite positive number")
            if not isfinite(statement_timeout) or statement_timeout <= 0:
                raise RegistryError("statement_timeout must be a finite positive number")
        if identity_map_warn_at is not None:
            if type(identity_map_warn_at) is not int or identity_map_warn_at < 1:
                raise RegistryError("identity_map_warn_at must be a positive integer")
        if validate_schema not in _VALIDATE_MODES:
            raise RegistryError(
                f"unknown validate_schema {validate_schema!r}; expected one of "
                f"{', '.join(sorted(_VALIDATE_MODES))}"
            )
        if type(query_cache_size) is not int:
            raise RegistryError("query_cache_size must be a positive integer")
        if query_cache_size < 1:
            raise RegistryError("query_cache_size must be a positive integer")
        if type(query_cache_bytes) is not int:
            raise RegistryError("query_cache_bytes must be a positive integer")
        if query_cache_bytes < 1:
            raise RegistryError("query_cache_bytes must be a positive integer")
        self.statement_timeout = statement_timeout
        #: Default `identity_map_warn_at` for sessions this registry opens. Off
        #: unless set; see `wreath.orm.session.Session`.
        self.identity_map_warn_at = identity_map_warn_at
        self.database = database
        self.validate_schema: ValidateSchema = validate_schema
        self.schema_mode = schema_mode or SchemaMode.single("public")
        self.query_cache_size = query_cache_size
        self.query_cache_bytes = query_cache_bytes
        self._specs: dict[type[Model], ModelSpec] = {}
        self._by_table: dict[tuple[str, str], ModelSpec] = {}
        # Class name -> spec, or None when the name is ambiguous (two models
        # share it). Lets string-target relationships resolve in O(1) instead of
        # scanning every spec, so registry compilation is O(M + R) not O(M * R).
        self._by_name: dict[str, ModelSpec | None] = {}
        self.specs: tuple[ModelSpec, ...] = ()
        # Model class -> Flight Recorder metadata-image ID, stamped at startup
        # the way a Database's `_flight_dep_id` is, so an armed query attributes
        # its ORM_HYDRATE phase with one dict lookup and no name formatting.
        # Empty until an app builds the image; an unstamped model records no
        # phase rather than attributing to a made-up ID.
        self._flight_model_ids: dict[type[Model], int] = {}
        # `(access method, indexed type OID) -> default operator class`, read
        # from this registry's database by
        # `wreath.orm.introspection.resolve_default_opclasses`. `None` means not
        # resolved yet; an empty dict means resolved and there are none, which is
        # a different answer and must not trigger a second read. Nothing outside
        # the migration descriptor consults it.
        self.default_opclasses: dict[tuple[str, int], str] | None = None
        self.fingerprint = b""
        self.template_fingerprint = b""
        self.deployment_fingerprint = b""
        # Plan cache insertion and eviction must stay correct without the GIL.
        self._lock = threading.Lock()
        self._cache: Any = KV(max_entries=query_cache_size, max_bytes=query_cache_bytes)
        # Declared-query identity -> its registry-specific shape key. The plan
        # itself remains in the bounded LRU above; this small index lets a hot
        # declaration reach it without rebuilding a Select or hashing its tree.
        self._prepared_shapes: dict[Any, bytes] = {}
        self.compile(tuple(models))

    def compile(self, models: tuple[type[Model], ...]) -> None:
        """Resolve every model and relationship, then freeze the metadata."""
        for model in models:
            if not isinstance(model, ModelMeta) or not issubclass(model, Model):
                raise DeclarationError(f"{model!r} is not a wreath.orm Model subclass")
            if model.__wreath_table__ is None:
                raise DeclarationError(
                    f"{model.__name__} declares no table= and cannot be registered"
                )
            if model in self._specs:
                raise DeclarationError(f"{model.__name__} is registered twice")
            spec = self._build_columns(model)
            existing = self._by_table.get((spec.schema, spec.table))
            if existing is not None:
                raise DeclarationError(
                    f"{model.__name__} and {existing.model_type.__name__} both map "
                    f"{spec.qualified_name}"
                )
            self._specs[model] = spec
            self._by_table[(spec.schema, spec.table)] = spec
            name = model.__name__
            self._by_name[name] = None if name in self._by_name else spec

        for model, spec in self._specs.items():
            relationships = tuple(
                self._build_relationship(spec, item) for item in model.__wreath_relationships__
            )
            # Relationship graphs are cyclic (User.posts <-> Post.author), so
            # the specs are completed in place here and never mutated again.
            object.__setattr__(spec, "relationships", relationships)
            object.__setattr__(
                spec,
                "by_relationship_name",
                {item.name: item for item in reversed(relationships)},
            )
        for spec in self._specs.values():
            object.__setattr__(
                spec,
                "fingerprint",
                fingerprint_model(
                    spec.schema,
                    spec.table,
                    spec.columns,
                    spec.relationships,
                    spec.table_uniques,
                    spec.table_indexes,
                ),
            )
        for spec in self._specs.values():
            self._check_back_populates(spec)
        self.specs = tuple(self._specs.values())
        # Frozen alongside `specs` so dependency order is one dict lookup rather
        # than a scan per scheduled object.
        self._model_order: dict[type[Model], int] = {
            spec.model_type: index for index, spec in enumerate(self.specs)
        }
        self.template_fingerprint = fingerprint_registry_template(self.specs)
        self.deployment_fingerprint = fingerprint_registry(self.specs)
        self.fingerprint = self.deployment_fingerprint

    def _build_columns(self, model: type[Model]) -> ModelSpec:
        table = model.__wreath_table__
        if table is None:
            raise DeclarationError(
                f"{model.__name__} has no table; decorate it with @model(table=...)"
            )
        columns: list[ColumnSpec] = []
        seen_database_names: dict[str, str] = {}
        for position, item in enumerate(model.__wreath_columns__):
            existing = seen_database_names.get(item.database_name)
            if existing is not None:
                raise DeclarationError(
                    f"{model.__name__}.{item.python_name} and {existing} map the same "
                    f"database column {item.database_name!r}"
                )
            seen_database_names[item.database_name] = f"{model.__name__}.{item.python_name}"
            columns.append(
                ColumnSpec(
                    python_name=item.python_name,
                    database_name=item.database_name,
                    position=position,
                    pg_type=item.pg_type,
                    nullable=item.nullable,
                    primary_key=item.primary_key,
                    unique=item.unique,
                    indexed=item.indexed,
                    default=item.default,
                    server_default=item.server_default,
                    reference=_column_ref(item),
                    column=item,
                )
            )
        primary_key = tuple(item for item in columns if item.primary_key)
        if not primary_key:
            raise DeclarationError(f"{model.__name__} declares no primary-key column")
        db_names = {item.database_name for item in columns}
        table_uniques = tuple(getattr(model, "__wreath_proto_uniques__", ()))
        table_indexes = tuple(getattr(model, "__wreath_proto_indexes__", ()))
        for declaration in (*table_uniques, *table_indexes):
            for name in declaration.columns:
                if name not in db_names:
                    raise DeclarationError(
                        f"{model.__name__} {declaration!r} names unknown column "
                        f"{name!r}; declare it as a column first"
                    )
        # A partial index's predicate is rendered here, once column types are
        # known, into the exact text PostgreSQL's catalog will report back. Doing
        # it at declaration time would be too early (no types) and at migration
        # time too late (a bad predicate must fail startup, not a deploy).
        by_db_name = {item.database_name: item for item in columns}
        # A generated column's expression is rendered here for the same reason a
        # partial index's predicate is: it names other columns, so it needs their
        # database names and types, and it must fail startup rather than a deploy.
        for item in columns:
            if isinstance(item.pg_type, GeneratedType):
                object.__setattr__(
                    item,
                    "generated_sql",
                    render_generation(item.pg_type, by_db_name, model.__name__),
                )
        table_indexes = tuple(
            declaration
            if declaration.where is None
            else Index(
                declaration.columns,
                declaration.unique,
                declaration.where,
                render_predicate(declaration.where, by_db_name, model.__name__),
            )
            for declaration in table_indexes
        )
        declared_schema = model.__wreath_schema__
        schema_ref = (
            declared_schema
            if isinstance(declared_schema, SchemaRef)
            else SchemaRef("fixed", declared_schema)
        )
        resolved_schema, sql_namespace = self._resolve_schema(schema_ref)
        spec = ModelSpec(
            model_type=model,
            schema=resolved_schema,
            schema_ref=schema_ref,
            table=table,
            sql_namespace=sql_namespace,
            columns=tuple(columns),
            primary_key=primary_key,
            relationships=(),
            storage=StorageSpec(
                field_count=len(columns),
                relation_count=len(model.__wreath_relationships__),
                basicsize=model.__basicsize__,
            ),
            by_name={item.python_name: item for item in columns},
            by_database_name={item.database_name: item for item in columns},
            table_uniques=table_uniques,
            table_indexes=table_indexes,
        )
        return spec

    def _resolve_schema(
        self, schema: SchemaRef
    ) -> tuple[str, Literal["qualified", "tenant_search_path"]]:
        if schema.kind == "fixed":
            if schema.name is None:
                raise DeclarationError("a fixed schema reference requires a schema name")
            return schema.name, "qualified"
        if self.schema_mode.kind == "single":
            if self.schema_mode.schema is None:
                raise DeclarationError("single-schema mode requires a schema name")
            return self.schema_mode.schema, "qualified"
        if schema.kind == "central":
            if self.schema_mode.central is None:
                raise DeclarationError("a central model requires a central schema")
            return self.schema_mode.central, "qualified"
        return "", "tenant_search_path"

    def _build_relationship(self, owner: ModelSpec, item: Relationship) -> RelationshipSpec:
        target = self._resolve_target(owner, item)
        if owner.schema_ref.kind == "central" and target.schema_ref.kind == "tenant":
            raise DeclarationError(
                f"central model {owner.model_type.__name__} cannot declare a relationship "
                f"to tenant model {target.model_type.__name__}"
            )
        # Write the class back onto the declaration, so a forward-referenced
        # relationship can be traversed in a predicate once models are compiled.
        item.resolved_target = target.model_type
        if item.prototype is not None:
            item.prototype.resolved_target = target.model_type
        keys = self._resolve_foreign_key(owner, target, item)
        holder = keys[0].column.owner
        if any(key.column.owner is not holder for key in keys):
            raise DeclarationError(
                f"{owner.model_type.__name__}.{item.python_name} mixes foreign-key "
                "columns from different models"
            )
        if holder is owner.model_type:
            cardinality: Literal["one", "many"] = "one"
            local, remote = keys, self._referenced(owner, target, item, keys, target)
        elif holder is target.model_type:
            cardinality = "many"
            remote, local = keys, self._referenced(owner, target, item, keys, owner)
        else:
            raise DeclarationError(
                f"{owner.model_type.__name__}.{item.python_name} names a foreign key on "
                f"{holder.__name__}, which is neither {owner.model_type.__name__} nor "
                f"{target.model_type.__name__}"
            )
        if len(local) != len(remote):
            raise DeclarationError(
                f"{owner.model_type.__name__}.{item.python_name} joins {len(local)} "
                f"column(s) to {len(remote)}"
            )
        for left, right in zip(local, remote, strict=True):
            if left.oid != right.oid:
                raise DeclarationError(
                    f"{owner.model_type.__name__}.{item.python_name} joins "
                    f"{left.python_name} ({left.pg_type.name}) to {right.python_name} "
                    f"({right.pg_type.name}); foreign keys must match types"
                )
        return RelationshipSpec(
            name=item.python_name,
            target=target,
            local_columns=local,
            remote_columns=remote,
            cardinality=cardinality,
            default_load=item.load,
            relationship=item,
        )

    def _resolve_target(self, owner: ModelSpec, item: Relationship) -> ModelSpec:
        target = item.target
        if isinstance(target, str):
            if target not in self._by_name:
                raise DeclarationError(
                    f"{owner.model_type.__name__}.{item.python_name} targets "
                    f"{target!r}, which this registry does not contain"
                )
            match = self._by_name[target]
            if match is None:
                raise DeclarationError(
                    f"{owner.model_type.__name__}.{item.python_name} targets "
                    f"{target!r}, which is ambiguous; pass the class instead"
                )
            return match
        spec = self._specs.get(target)
        if spec is None:
            raise DeclarationError(
                f"{owner.model_type.__name__}.{item.python_name} targets "
                f"{getattr(target, '__name__', target)!r}, which this registry does "
                "not contain"
            )
        return spec

    def _resolve_foreign_key(
        self, owner: ModelSpec, target: ModelSpec, item: Relationship
    ) -> tuple[ColumnSpec, ...]:
        declared = item.foreign_key
        keys = declared if isinstance(declared, (tuple, list)) else (declared,)
        resolved: list[ColumnSpec] = []
        for key in keys:
            resolved.append(self._resolve_key_column(owner, target, item, key))
        if not resolved:
            raise DeclarationError(
                f"{owner.model_type.__name__}.{item.python_name} declares an empty foreign_key"
            )
        return tuple(resolved)

    def _resolve_key_column(
        self, owner: ModelSpec, target: ModelSpec, item: Relationship, key: Any
    ) -> ColumnSpec:
        if isinstance(key, ColumnExpr):
            key = key.column
        if isinstance(key, Column):
            for spec in (owner, target):
                bound = spec.model_type.__wreath_by_prototype__.get(key.prototype or key)
                if bound is not None:
                    return spec.by_name[bound.python_name]
            raise DeclarationError(
                f"{owner.model_type.__name__}.{item.python_name} names foreign-key "
                f"column {key.python_name!r}, which belongs to neither "
                f"{owner.model_type.__name__} nor {target.model_type.__name__}"
            )
        if not isinstance(key, str):
            raise DeclarationError(
                f"{owner.model_type.__name__}.{item.python_name} foreign_key must be a "
                f"column, a column name, or a tuple of them, got {key!r}"
            )
        on_owner = owner.by_name.get(key)
        on_target = target.by_name.get(key)
        if on_owner is not None and on_target is not None and owner is not target:
            raise DeclarationError(
                f"{owner.model_type.__name__}.{item.python_name} names foreign key "
                f"{key!r}, which exists on both {owner.model_type.__name__} and "
                f"{target.model_type.__name__}; pass the column itself to disambiguate"
            )
        found = on_owner if on_owner is not None else on_target
        if found is None:
            raise DeclarationError(
                f"{owner.model_type.__name__}.{item.python_name} names foreign key "
                f"{key!r}, which exists on neither {owner.model_type.__name__} nor "
                f"{target.model_type.__name__}"
            )
        return found

    def _referenced(
        self,
        owner: ModelSpec,
        target: ModelSpec,
        item: Relationship,
        keys: tuple[ColumnSpec, ...],
        side: ModelSpec,
    ) -> tuple[ColumnSpec, ...]:
        """The columns `keys` point at, from references= or `side`'s key."""
        referenced: list[ColumnSpec] = []
        for key in keys:
            reference = key.reference
            if reference is None:
                referenced.clear()
                break
            if reference.model_type is not side.model_type:
                raise DeclarationError(
                    f"{owner.model_type.__name__}.{item.python_name} uses "
                    f"{key.python_name}, which references "
                    f"{reference.schema}.{reference.table} rather than "
                    f"{side.qualified_name}"
                )
            referenced.append(side.by_database_name[reference.column])
        if referenced:
            return tuple(referenced)
        if len(keys) != len(side.primary_key):
            raise DeclarationError(
                f"{owner.model_type.__name__}.{item.python_name} joins {len(keys)} "
                f"column(s) to {side.model_type.__name__}'s "
                f"{len(side.primary_key)}-column primary key; declare references= on "
                "the foreign-key column"
            )
        return side.primary_key

    def _check_back_populates(self, spec: ModelSpec) -> None:
        for item in spec.relationships:
            name = item.relationship.back_populates
            if name is None:
                continue
            other = item.target.relationship(name)
            if other is None:
                raise DeclarationError(
                    f"{spec.model_type.__name__}.{item.name} back_populates "
                    f"{name!r}, which {item.target.model_type.__name__} does not declare"
                )
            if other.target is not spec:
                raise DeclarationError(
                    f"{spec.model_type.__name__}.{item.name} back_populates "
                    f"{item.target.model_type.__name__}.{name}, which targets "
                    f"{other.target.model_type.__name__} instead"
                )

    def spec_for(self, model: type[Model]) -> ModelSpec:
        spec = self._specs.get(model)
        if spec is None:
            raise RegistryError(
                f"{getattr(model, '__name__', model)!r} is not registered with the "
                f"{self.database.name!r} ORM registry"
            )
        return spec

    def order_of(self, model: type[Model]) -> int:
        """The model's position in `specs`: its dependency order for writes."""
        index = self._model_order.get(model)
        if index is None:
            raise RegistryError(
                f"{getattr(model, '__name__', model)!r} is not registered with the "
                f"{self.database.name!r} ORM registry"
            )
        return index

    def __contains__(self, model: object) -> bool:
        return model in self._specs

    def cached_plan(self, shape_key: bytes) -> Any:
        with self._lock:
            # `get` is the recency update: reading a plan is using it.
            return self._cache.get(shape_key)

    def store_plan(self, shape_key: bytes, plan: Any) -> Any:
        with self._lock:
            existing = self._cache.get(shape_key)
            if existing is not None:
                # Another thread compiled the same shape; keep one plan so
                # callers cannot observe two objects for one key.
                return existing
            self._cache.set(shape_key, plan, cost=_cache_entry_bytes(shape_key, plan))
            return plan

    def cached_prepared_plan(self, declaration: Any) -> tuple[bytes, Any] | None:
        """The live plan previously associated with `declaration`, if any."""
        with self._lock:
            shape_key = self._prepared_shapes.get(declaration)
            if shape_key is None:
                return None
            entry = self._cache.get(shape_key)
            if entry is None:
                return None
            return shape_key, entry

    def remember_prepared_shape(self, declaration: Any, shape_key: bytes) -> None:
        """Associate an explicit declaration with its registry-owned plan key."""
        with self._lock:
            self._prepared_shapes[declaration] = shape_key

    @property
    def cached_plan_count(self) -> int:
        with self._lock:
            return len(self._cache)

    def __repr__(self) -> str:
        return (
            f"<Registry database={self.database.name!r} "
            f"models={len(self._specs)} cache={self.cached_plan_count}>"
        )


_FK_ACTION_CODE = {
    None: "a",
    "no action": "a",
    "restrict": "r",
    "cascade": "c",
    "set null": "n",
    "set default": "d",
}


def _column_ref(item: Column) -> ColumnRef | None:
    reference = item.references
    if reference is None:
        return None
    target = reference.column
    owner = target.owner
    if owner is None or owner.__wreath_table__ is None:
        raise DeclarationError(
            f"column {item.python_name!r} references {target.python_name!r} on an unmapped class"
        )
    return ColumnRef(
        schema=owner.__wreath_schema__,
        table=owner.__wreath_table__,
        column=target.database_name,
        position=target.index + 1,
        model_type=owner,
        on_delete=_FK_ACTION_CODE[item.on_delete],
        on_update=_FK_ACTION_CODE[item.on_update],
        deferrable=item.deferrable,
    )


__all__ = ["Registry", "ValidateSchema"]
