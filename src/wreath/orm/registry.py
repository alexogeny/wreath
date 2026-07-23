"""The application-owned model registry.

A registry binds a set of models to one database. It is the only place that
resolves relationships, and the only owner of compiled query plans. Two
applications can register the same model classes against different databases
without sharing sessions, identity objects, or cached plans.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from sys import getsizeof
from typing import Any, Literal

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
        "_by_name",
        "_by_table",
        "_cache",
        "_cache_bytes",
        "_lock",
        "_model_order",
        "_specs",
        "database",
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
    ) -> None:
        if validate_schema not in _VALIDATE_MODES:
            raise RegistryError(
                f"unknown validate_schema {validate_schema!r}; expected one of "
                f"{', '.join(sorted(_VALIDATE_MODES))}"
            )
        if not isinstance(query_cache_size, int) or query_cache_size < 1:
            raise RegistryError("query_cache_size must be a positive integer")
        if not isinstance(query_cache_bytes, int) or query_cache_bytes < 1:
            raise RegistryError("query_cache_bytes must be a positive integer")
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
        self.fingerprint = b""
        self.template_fingerprint = b""
        self.deployment_fingerprint = b""
        # Plan cache insertion and eviction must stay correct without the GIL.
        self._lock = threading.Lock()
        self._cache: OrderedDict[bytes, tuple[Any, int]] = OrderedDict()
        self._cache_bytes = 0
        self.compile(tuple(models))

    # -- compilation --------------------------------------------------------

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
                self._build_relationship(spec, item)
                for item in model.__wreath_relationships__
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
                fingerprint_model(spec.schema, spec.table, spec.columns, spec.relationships),
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
        assert table is not None, "compile() rejects table-less models before this point"
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
                    oid=item.pg_type.oid,
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
                kind=getattr(model, "__wreath_storage_kind__", "pure"),
                field_count=len(columns),
                relation_count=len(model.__wreath_relationships__),
                basicsize=getattr(model, "__wreath_basicsize__", None),
            ),
            by_name={item.python_name: item for item in columns},
            by_database_name={item.database_name: item for item in columns},
        )
        return spec

    def _resolve_schema(
        self, schema: SchemaRef
    ) -> tuple[str, Literal["qualified", "tenant_search_path"]]:
        if schema.kind == "fixed":
            assert schema.name is not None
            return schema.name, "qualified"
        if self.schema_mode.kind == "single":
            assert self.schema_mode.schema is not None
            return self.schema_mode.schema, "qualified"
        if schema.kind == "central":
            assert self.schema_mode.central is not None
            return self.schema_mode.central, "qualified"
        return "", "tenant_search_path"

    def _build_relationship(
        self, owner: ModelSpec, item: Relationship
    ) -> RelationshipSpec:
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
                f"{owner.model_type.__name__}.{item.python_name} declares an empty "
                "foreign_key"
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
        """The columns ``keys`` point at, from references= or ``side``'s key."""
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

    # -- lookup -------------------------------------------------------------

    def spec_for(self, model: type[Model]) -> ModelSpec:
        spec = self._specs.get(model)
        if spec is None:
            raise RegistryError(
                f"{getattr(model, '__name__', model)!r} is not registered with the "
                f"{self.database.name!r} ORM registry"
            )
        return spec

    def order_of(self, model: type[Model]) -> int:
        """The model's position in ``specs``: its dependency order for writes."""
        index = self._model_order.get(model)
        if index is None:
            raise RegistryError(
                f"{getattr(model, '__name__', model)!r} is not registered with the "
                f"{self.database.name!r} ORM registry"
            )
        return index

    def __contains__(self, model: object) -> bool:
        return model in self._specs

    # -- bounded plan cache -------------------------------------------------

    def cached_plan(self, shape_key: bytes) -> Any:
        with self._lock:
            entry = self._cache.get(shape_key)
            if entry is not None:
                self._cache.move_to_end(shape_key)
                return entry[0]
            return None

    def store_plan(self, shape_key: bytes, plan: Any) -> Any:
        with self._lock:
            existing = self._cache.get(shape_key)
            if existing is not None:
                # Another thread compiled the same shape; keep one plan so
                # callers cannot observe two objects for one key.
                self._cache.move_to_end(shape_key)
                return existing[0]
            retained = _cache_entry_bytes(shape_key, plan)
            self._cache[shape_key] = (plan, retained)
            self._cache_bytes += retained
            while (
                len(self._cache) > self.query_cache_size
                or self._cache_bytes > self.query_cache_bytes
            ):
                _, (_, evicted_bytes) = self._cache.popitem(last=False)
                self._cache_bytes -= evicted_bytes
            return plan

    @property
    def cached_plan_count(self) -> int:
        with self._lock:
            return len(self._cache)

    def __repr__(self) -> str:
        return (
            f"<Registry database={self.database.name!r} "
            f"models={len(self._specs)} cache={self.cached_plan_count}>"
        )


def _column_ref(item: Column) -> ColumnRef | None:
    reference = item.references
    if reference is None:
        return None
    target = reference.column
    owner = target.owner
    if owner is None or owner.__wreath_table__ is None:
        raise DeclarationError(
            f"column {item.python_name!r} references {target.python_name!r} on an "
            "unmapped class"
        )
    return ColumnRef(
        schema=owner.__wreath_schema__,
        table=owner.__wreath_table__,
        column=target.database_name,
        position=target.index + 1,
        model_type=owner,
    )


__all__ = ["Registry", "ValidateSchema"]
