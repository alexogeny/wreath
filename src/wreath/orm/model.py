"""Model declaration, object state, and storage selection.

`ModelMeta` compiles a class body into an ordered column layout once, at
class creation. Registries later resolve relationships against that layout; no
part of the request path inspects annotations or class bodies again.

Every concrete model gets a *storage base* holding its values:

* the native backend generates one C type per model whose `tp_basicsize` is
  fixed, with unboxed inline cells for scalars and C descriptors for access;
* `PureModel` is the reference implementation, over a list and integer
  bitmaps.

Both implement the same storage protocol with the same observable behavior.
Assignment validates through the column's `PgType.coerce` in both, so the
type rules have one implementation rather than two that can drift.
"""

from __future__ import annotations

import importlib
import os
import re
from typing import Any, ClassVar

from .constraints import (
    CheckViolation,
    Narrow,
    Rule,
    check_rules,
    compile_rules,
)
from .errors import (
    DeclarationError,
    MappingError,
    UnloadedAttributeError,
    UnloadedRelationshipError,
)
from .fields import MISSING, Column, resolve_default
from .query import Select
from .relations import Relationship
from .schema import SchemaRef
from .table import Index, Unique


def _load_native() -> Any:
    if os.environ.get("WREATH_PURE"):
        return None
    try:
        _postgres = importlib.import_module("wreath._native._postgres")
    except ImportError:
        return None
    if not hasattr(_postgres, "_compile_model_layout"):
        # An older compiled extension without model storage; the reference
        # implementation still gives correct behavior.
        return None
    # The C module cannot import these itself: wreath.orm imports wreath.postgres,
    # which imports this extension, so the import would be circular.
    _postgres._configure_model_errors(
        UnloadedAttributeError, UnloadedRelationshipError, DeclarationError
    )
    if hasattr(_postgres, "_configure_hydrate_errors"):
        _postgres._configure_hydrate_errors(MappingError)
    return _postgres


_native = _load_native()

# Object states. A model is exactly one of these at any time.
TRANSIENT = 0  #: constructed, not present in an identity map
PERSISTENT = 1  #: loaded or inserted, owned by one open session
DELETED = 2  #: scheduled for deletion
DETACHED = 3  #: the owning session closed; loaded scalars stay readable

STATE_NAMES = {
    TRANSIENT: "transient",
    PERSISTENT: "persistent",
    DELETED: "deleted",
    DETACHED: "detached",
}

_UNLOADED: Any = object()

# Unquoted PostgreSQL identifiers fold to lower case, so an upper-case name in
# a declaration would silently disagree with the catalog.
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_$]*$")


def validate_identifier(value: str, kind: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.match(value):
        raise DeclarationError(
            f"{kind} {value!r} is not a valid unquoted PostgreSQL identifier: "
            "use lower-case letters, digits, and underscores, starting with a "
            "letter or underscore"
        )
    if len(value.encode("utf-8")) > 63:
        raise DeclarationError(f"{kind} {value!r} exceeds PostgreSQL's 63-byte limit")
    return value


#: ModelMeta derives from the native metatype when it is available, so every
#: model class is allocated with room for its compiled layout. That is what lets
#: a class be created by Python while its storage base is generated in C without
#: a metaclass conflict.
_MetaBase: Any = type if _native is None else _native._ModelType


class ModelMeta(_MetaBase):
    """Collects ordered column and relationship declarations."""

    def __new__(
        mcls,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        *,
        table: str | None = None,
        schema: str | SchemaRef = "public",
        **kwargs: Any,
    ) -> ModelMeta:
        inherited_columns: list[Column] = []
        inherited_relations: list[Relationship] = []
        inherited_narrows: list[Narrow] = []
        inherited_rules: list[Rule] = []
        inherited_uniques: list[Unique] = []
        inherited_indexes: list[Index] = []
        for base in bases:
            if getattr(base, "__wreath_table__", None) is not None:
                raise DeclarationError(
                    f"{name} cannot inherit from the mapped model {base.__name__}; "
                    "share columns through a table-less base class instead"
                )
            inherited_columns.extend(getattr(base, "__wreath_proto_columns__", ()))
            inherited_relations.extend(getattr(base, "__wreath_proto_relations__", ()))
            inherited_narrows.extend(getattr(base, "__wreath_proto_narrows__", ()))
            inherited_rules.extend(getattr(base, "__wreath_proto_rules__", ()))
            inherited_uniques.extend(getattr(base, "__wreath_proto_uniques__", ()))
            inherited_indexes.extend(getattr(base, "__wreath_proto_indexes__", ()))

        own_columns = [
            (key, value)
            for key, value in namespace.items()
            if isinstance(value, Column)
        ]
        own_relations = [
            (key, value)
            for key, value in namespace.items()
            if isinstance(value, Relationship)
        ]
        for key, prototype in (*own_columns, *own_relations):
            prototype.python_name = key

        # Inherited declarations precede subclass declarations; within each
        # group, class-body order wins.
        columns = [*inherited_columns, *(value for _, value in own_columns)]
        relations = [*inherited_relations, *(value for _, value in own_relations)]
        _reject_duplicates(name, columns, "column")
        _reject_duplicates(name, relations, "relationship")

        # Constraint declarations are found by type, like columns are; their
        # attribute names are documentation. Inherited ones come first and are
        # never dropped, so a subclass can only ever tighten what it inherits.
        narrows = [
            *inherited_narrows,
            *(value for value in namespace.values() if isinstance(value, Narrow)),
        ]
        rules = [
            *inherited_rules,
            *(value for value in namespace.values() if isinstance(value, Rule)),
        ]
        uniques = [
            *inherited_uniques,
            *(value for value in namespace.values() if isinstance(value, Unique)),
        ]
        indexes = [
            *inherited_indexes,
            *(value for value in namespace.values() if isinstance(value, Index)),
        ]

        namespace["__wreath_proto_columns__"] = tuple(columns)
        namespace["__wreath_proto_relations__"] = tuple(relations)
        namespace["__wreath_proto_narrows__"] = tuple(narrows)
        namespace["__wreath_proto_rules__"] = tuple(rules)
        namespace["__wreath_proto_uniques__"] = tuple(uniques)
        namespace["__wreath_proto_indexes__"] = tuple(indexes)
        namespace["__wreath_table__"] = table
        namespace["__wreath_schema__"] = schema
        namespace.setdefault("__slots__", ())

        if table is None:
            # A table-less class is a base or mixin: it contributes columns but
            # has no storage, no identity, and cannot be queried. Its checks and
            # rules travel with the columns and compile in whatever maps them.
            namespace["__wreath_columns__"] = ()
            namespace["__wreath_relationships__"] = ()
            namespace["__wreath_by_prototype__"] = {}
            namespace["__wreath_column_map__"] = {}
            namespace["__wreath_primary_key__"] = ()
            namespace["__wreath_rules__"] = ()
            namespace["__wreath_compiled_rules__"] = ()
            return super().__new__(mcls, name, bases, namespace, **kwargs)

        validate_identifier(table, "table name")
        if isinstance(schema, str):
            validate_identifier(schema, "schema name")
        elif not isinstance(schema, SchemaRef):
            raise DeclarationError(
                f"schema {schema!r} must be a PostgreSQL identifier or logical schema role"
            )
        for item in columns:
            validate_identifier(item.python_name, "column name")
        if not any(item.primary_key for item in columns):
            raise DeclarationError(
                f"{name} declares no primary-key column; every mapped model needs one"
            )

        by_prototype: dict[Any, Any] = {}
        bound_columns: list[Column] = []
        for index, prototype in enumerate(columns):
            clone = prototype._clone(None, prototype.python_name, index)
            bound_columns.append(clone)
            by_prototype[prototype] = clone
        bound_relations: list[Relationship] = []
        for index, prototype in enumerate(relations):
            clone = prototype._clone(None, prototype.python_name, index)
            bound_relations.append(clone)
            by_prototype[prototype] = clone

        # Narrowing lands on the clones, so a base's column keeps its own rules
        # in every other model that inherits it. Once the chain is final, each
        # column fuses its type and its checks into the one callable every write
        # path goes through.
        column_map = {item.python_name: item for item in bound_columns}
        for item in narrows:
            target = column_map.get(item.field)
            if target is None:
                raise DeclarationError(
                    f"{name}: narrow({item.field!r}) names a column {name} does "
                    f"not declare; it has {', '.join(sorted(column_map)) or 'none'}"
                )
            target._narrow(item.checks)
        for item in bound_columns:
            item._compile(name)

        storage = _storage_base(name, bound_columns, bound_relations, namespace)
        bases = (storage, *bases)

        namespace["__wreath_columns__"] = tuple(bound_columns)
        namespace["__wreath_relationships__"] = tuple(bound_relations)
        namespace["__wreath_by_prototype__"] = by_prototype
        namespace["__wreath_column_map__"] = column_map
        namespace["__wreath_rules__"] = tuple(rules)
        # Identity is a property of the declaration, not of a registry, so two
        # registries mapping the same class agree on it without sharing state.
        namespace["__wreath_primary_key__"] = tuple(
            item for item in bound_columns if item.primary_key
        )
        namespace["__wreath_storage_kind__"] = "pure" if _native is None else "native"
        cls = super().__new__(mcls, name, bases, namespace, **kwargs)
        for item in (*bound_columns, *bound_relations):
            item.owner = cls
        # Descriptors are installed after the class exists because each needs
        # the ColumnExpr, which needs its column's owner.
        _install_descriptors(cls, storage, bound_columns, bound_relations)
        # Rules resolve their column names to storage indexes once, here, so a
        # rule over a column that does not exist is a declaration error rather
        # than a surprise on the first request that reaches it.
        cls.__wreath_compiled_rules__ = compile_rules(cls)
        return cls

    def __init__(
        cls,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        super().__init__(name, bases, namespace)

    def select(cls, *fields: Any) -> Any:
        """Begin a SELECT for this model; no arguments selects every column."""
        return Select.build(cls, fields)

    def __repr__(cls) -> str:
        table = getattr(cls, "__wreath_table__", None)
        if table is None:
            return f"<model base {cls.__name__}>"
        return f"<model {cls.__name__} {getattr(cls, '__wreath_schema__', '?')}.{table}>"


def _storage_base(
    name: str,
    columns: list[Column],
    relations: list[Relationship],
    namespace: dict[str, Any],
) -> type:
    """The class that will hold this model's values.

    Native storage is generated per model, so its `tp_basicsize` is fixed and
    scalar cells are unboxed. It is rooted at `object` rather than `Model`:
    rooting it at `Model` would make its metatype conflict with `ModelMeta`.
    """
    if _native is None:
        return PureModel
    return _native._compile_model_layout(
        tuple(
            (item.pg_type.oid, item.primary_key, item.nullable) for item in columns
        ),
        len(relations),
    )


def _install_descriptors(
    cls: type,
    storage: type,
    columns: list[Column],
    relations: list[Relationship],
) -> None:
    if _native is None:
        # The Column and Relationship objects are themselves the descriptors.
        for item in (*columns, *relations):
            setattr(cls, item.python_name, item)
        return
    for index, column in enumerate(columns):
        setattr(
            cls,
            column.python_name,
            _native._make_column_descriptor(storage, index, column),
        )
    for index, relation in enumerate(relations):
        setattr(
            cls,
            relation.python_name,
            _native._make_relation_descriptor(storage, index, relation),
        )


def enforce_rules(instance: Any) -> None:
    """Raise if any whole-object rule refuses `instance`.

    A `CheckViolation` is a `ValueError`, the same thing a refused
    assignment raises, so a caller that already handles a bad value handles a
    broken rule without a new except clause.
    """
    broken = check_rules(instance)
    if not broken:
        return
    message, kind, _ = broken[0]
    if len(broken) > 1:
        message = "; ".join(item[0] for item in broken)
    raise CheckViolation(f"{type(instance).__name__}: {message}", kind)


def _reject_duplicates(model: str, items: list[Any], kind: str) -> None:
    seen: dict[str, Any] = {}
    for item in items:
        existing = seen.get(item.python_name)
        if existing is not None and existing is not item:
            raise DeclarationError(
                f"{model} declares {kind} {item.python_name!r} twice"
            )
        seen[item.python_name] = item


class Model(metaclass=ModelMeta):
    """Base class for mapped models.

    Subclass with `table=` to map a class to a table:

    ```python
    class User(Model, table="users"):
        id: Mapped[int] = column(Int64, primary_key=True)
        email: Mapped[str] = column(Text, unique=True)
    ```
    Subclassing without `table=` declares a reusable mixin whose columns are
    inherited, in declaration order, ahead of the subclass's own.
    """

    # Declared for readers and type checkers; ModelMeta fills every one of
    # these in per class, so they are annotations only and never class values.
    __wreath_table__: ClassVar[str | None]
    __wreath_schema__: ClassVar[str]
    __wreath_columns__: ClassVar[tuple[Column, ...]]
    __wreath_relationships__: ClassVar[tuple[Relationship, ...]]
    __wreath_column_map__: ClassVar[dict[str, Column]]
    __wreath_primary_key__: ClassVar[tuple[Column, ...]]
    __wreath_by_prototype__: ClassVar[dict[Any, Any]]
    __wreath_proto_columns__: ClassVar[tuple[Column, ...]]
    __wreath_proto_relations__: ClassVar[tuple[Relationship, ...]]
    __wreath_proto_narrows__: ClassVar[tuple[Narrow, ...]]
    __wreath_proto_rules__: ClassVar[tuple[Rule, ...]]
    __wreath_proto_uniques__: ClassVar[tuple[Unique, ...]]
    __wreath_proto_indexes__: ClassVar[tuple[Index, ...]]
    __wreath_rules__: ClassVar[tuple[Rule, ...]]
    __wreath_compiled_rules__: ClassVar[tuple[Any, ...]]

    # Storage lives in the per-model base ModelMeta prepends, so this class adds
    # no instance layout of its own.
    __slots__ = ()

    def __init__(self, **values: Any) -> None:
        columns = type(self).__wreath_columns__
        if not columns:
            raise TypeError(f"{type(self).__name__} is not a mapped model")
        unknown = values.keys() - type(self).__wreath_column_map__.keys()
        if unknown:
            raise TypeError(
                f"{type(self).__name__} has no column(s) "
                f"{', '.join(sorted(unknown))}"
            )
        for spec in columns:
            if spec.python_name in values:
                self._orm_set(spec.index, values[spec.python_name])
                continue
            default = resolve_default(spec)
            if default is not MISSING:
                self._orm_set(spec.index, default)
            elif not (spec.nullable or spec.server_default or spec.primary_key):
                # Anything else would insert a NULL the column forbids.
                raise TypeError(
                    f"{type(self).__name__}.{spec.python_name} is not nullable and has "
                    "no default; pass it to the constructor"
                )
        # Per-field checks already ran, fused into each assignment above. A rule
        # spans fields, so it can only run now that they are all in. Hydration
        # goes through _orm_new() and never lands here, which is deliberate: a
        # row the database already holds is not the constructor's to reject.
        if type(self).__wreath_compiled_rules__:
            enforce_rules(self)

    # -- storage protocol ---------------------------------------------------
    #
    # The contract every storage base implements. ModelMeta prepends one to the
    # bases of each concrete model, so these stubs are always overridden; they
    # are declared here to state the protocol in one place. PureModel is the
    # reference implementation, and the native type must match its behavior --
    # not its representation.

    _orm_state: int
    _orm_owner: Any

    @classmethod
    def _orm_new(cls) -> Any:
        """Allocate an empty instance, bypassing `__init__`."""
        raise NotImplementedError

    def _orm_get(self, index: int) -> Any:
        """The value, None if null, raising if the column was never loaded."""
        raise NotImplementedError

    def _orm_set(self, index: int, value: Any) -> None:
        """Validate and assign, tracking loaded/null/dirty."""
        raise NotImplementedError

    def _orm_set_loaded(self, index: int, value: Any) -> None:
        """Record a value read from the database, without marking it dirty."""
        raise NotImplementedError

    def _orm_is_loaded(self, index: int) -> bool:
        raise NotImplementedError

    def _orm_is_null(self, index: int) -> bool:
        raise NotImplementedError

    def _orm_is_dirty(self, index: int) -> bool:
        raise NotImplementedError

    def _orm_has_changes(self) -> bool:
        raise NotImplementedError

    def _orm_clear_dirty(self) -> None:
        raise NotImplementedError

    def _orm_get_relation(self, index: int) -> Any:
        raise NotImplementedError

    def _orm_set_relation(self, index: int, value: Any) -> None:
        raise NotImplementedError

    def _orm_relation_loaded(self, index: int) -> bool:
        raise NotImplementedError

    def _orm_primary_key(self) -> tuple[Any, ...] | None:
        """The identity tuple, or None when any component is unloaded or null."""
        values: list[Any] = []
        for spec in type(self).__wreath_primary_key__:
            if not self._orm_is_loaded(spec.index) or self._orm_is_null(spec.index):
                return None
            values.append(self._orm_get(spec.index))
        return tuple(values)

    @property
    def __wreath_state__(self) -> str:
        return STATE_NAMES[self._orm_state]

    def __repr__(self) -> str:
        cls = type(self)
        parts = []
        for spec in cls.__wreath_columns__:
            if not self._orm_is_loaded(spec.index):
                continue
            parts.append(f"{spec.python_name}={self._orm_get(spec.index)!r}")
        body = " ".join(parts) if parts else "<unloaded>"
        return f"<{cls.__name__} {STATE_NAMES[self._orm_state]} {body}>"


class PureModel(Model):
    """Reference storage: a value list plus integer bitmaps.

    This is the behavior the native storage must match. It is used whenever the
    compiled extension is unavailable or `WREATH_PURE=1` is set.
    """

    __slots__ = (
        "__weakref__",
        "_orm_dirty",
        "_orm_loaded",
        "_orm_null",
        "_orm_owner",
        "_orm_relations",
        "_orm_state",
        "_orm_values",
    )

    def __init__(self, **values: Any) -> None:
        self._orm_values = [None] * len(type(self).__wreath_columns__)
        self._orm_relations = [_UNLOADED] * len(type(self).__wreath_relationships__)
        self._orm_loaded = 0
        self._orm_null = 0
        self._orm_dirty = 0
        self._orm_state = TRANSIENT
        self._orm_owner: Any = None
        super().__init__(**values)

    @classmethod
    def _orm_new(cls) -> Any:
        """Allocate an empty instance, bypassing `__init__`.

        The hydrator fills cells directly; running the constructor would apply
        defaults over values the database just returned.
        """
        instance = cls.__new__(cls)
        instance._orm_values = [None] * len(cls.__wreath_columns__)
        instance._orm_relations = [_UNLOADED] * len(cls.__wreath_relationships__)
        instance._orm_loaded = 0
        instance._orm_null = 0
        instance._orm_dirty = 0
        instance._orm_state = TRANSIENT
        instance._orm_owner = None
        return instance

    def _orm_get(self, index: int) -> Any:
        bit = 1 << index
        if not self._orm_loaded & bit:
            spec = type(self).__wreath_columns__[index]
            raise UnloadedAttributeError(
                f"{type(self).__name__}.{spec.python_name} was not loaded; "
                "select it or reload the object"
            )
        if self._orm_null & bit:
            return None
        return self._orm_values[index]

    def _orm_set(self, index: int, value: Any) -> None:
        spec = type(self).__wreath_columns__[index]
        if spec.primary_key and self._orm_state == PERSISTENT:
            raise DeclarationError(
                f"cannot change primary key {type(self).__name__}.{spec.python_name} "
                "on a persistent object"
            )
        bit = 1 << index
        if value is None:
            if not spec.nullable:
                raise ValueError(
                    f"{type(self).__name__}.{spec.python_name} is not nullable"
                )
            changed = not (self._orm_loaded & bit) or not self._orm_null & bit
            self._orm_values[index] = None
            self._orm_null |= bit
        else:
            # The column's fused validator: its type, then its business rules.
            # This is the same callable the body validator runs and the native
            # descriptor calls, so assignment cannot accept what a request body
            # would be refused for.
            value = spec.validate(value)
            changed = (
                not (self._orm_loaded & bit)
                or bool(self._orm_null & bit)
                or not _same_value(self._orm_values[index], value)
            )
            self._orm_values[index] = value
            self._orm_null &= ~bit
        self._orm_loaded |= bit
        if changed and self._orm_state in (PERSISTENT, DELETED):
            self._orm_dirty |= bit

    def _orm_set_loaded(self, index: int, value: Any) -> None:
        """Record a value read from the database without marking it dirty."""
        bit = 1 << index
        if value is None:
            self._orm_values[index] = None
            self._orm_null |= bit
        else:
            self._orm_values[index] = value
            self._orm_null &= ~bit
        self._orm_loaded |= bit

    def _orm_get_relation(self, index: int) -> Any:
        value = self._orm_relations[index]
        if value is _UNLOADED:
            spec = type(self).__wreath_relationships__[index]
            raise UnloadedRelationshipError(
                f"{type(self).__name__}.{spec.python_name} was not loaded; "
                f"include it in the query or call await session.load(obj, "
                f"{type(self).__name__}.{spec.python_name})"
            )
        return value

    def _orm_set_relation(self, index: int, value: Any) -> None:
        self._orm_relations[index] = value

    def _orm_relation_loaded(self, index: int) -> bool:
        return self._orm_relations[index] is not _UNLOADED

    def _orm_is_loaded(self, index: int) -> bool:
        return bool(self._orm_loaded & (1 << index))

    def _orm_is_dirty(self, index: int) -> bool:
        return bool(self._orm_dirty & (1 << index))

    def _orm_has_changes(self) -> bool:
        return self._orm_dirty != 0

    def _orm_clear_dirty(self) -> None:
        self._orm_dirty = 0

    def _orm_is_null(self, index: int) -> bool:
        return bool(self._orm_null & (1 << index))


def _same_value(current: Any, value: Any) -> bool:
    # Compare by value, but never let a custom __eq__ result masquerade as a
    # bool; identical objects short-circuit for the common case.
    if current is value:
        return True
    try:
        return bool(current == value)
    except Exception:  # noqa: BLE001 -- a user's __eq__/__bool__ may raise anything
        # Both operands are application values, so the failure set belongs to
        # the caller and cannot be enumerated: a custom `__eq__` raises whatever
        # it likes, and `__bool__` on the result does too (a numpy array raises
        # ValueError, a Decimal NaN raises InvalidOperation). Not counted -- this
        # runs per field per flush, and the answer is already the safe one:
        # "not the same" marks the field dirty, so an unanswerable comparison
        # costs a redundant write rather than a lost one.
        return False


__all__ = [
    "DELETED",
    "DETACHED",
    "PERSISTENT",
    "STATE_NAMES",
    "TRANSIENT",
    "Model",
    "ModelMeta",
    "PureModel",
    "enforce_rules",
    "validate_identifier",
]
