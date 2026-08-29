"""Column declarations and the descriptor that backs field access.

A `column()` in a class body is a *prototype*. `ModelMeta` clones one
descriptor per concrete model so a column declared on a shared mixin can hold
a different storage index in each model that inherits it.
"""

from __future__ import annotations

import datetime
import re
import uuid
from collections.abc import Mapping
from typing import Any

from .constraints import Check, collect_checks, compile_column_validator
from .errors import DeclarationError, UnloadedAttributeError
from .expressions import ColumnExpr
from .types import GeneratedType, PgType


class _Missing:
    """The absence of a default, distinct from a `None` default."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Any = _Missing()


class Mapped[T]:
    """Typing-only marker for a mapped column.

    `Mapped[int]` documents that class access yields a SQL expression and
    instance access yields an `int`. It carries no runtime behavior: the
    registry compiles declarations from `column()` objects, never from
    annotations.
    """

    __slots__ = ()


_ORDER = 0


class Column:
    """A declared column, and the descriptor that reads and writes it."""

    __slots__ = (
        "_expression",
        "checks",
        "default",
        "deferrable",
        "index",
        "index_method",
        "index_ops",
        "index_with",
        "nullable",
        "on_delete",
        "on_update",
        "order",
        "owner",
        "pg_type",
        "primary_key",
        "prototype",
        "python_name",
        "references",
        "server_default",
        "shape_projection",
        "shape_ref",
        "unique",
        "indexed",
        "validate",
    )

    def __init__(
        self,
        pg_type: PgType,
        *,
        primary_key: bool,
        nullable: bool,
        unique: bool,
        indexed: bool,
        default: Any,
        server_default: str | None,
        references: Any,
        on_delete: str | None = None,
        on_update: str | None = None,
        deferrable: bool = False,
        checks: tuple[Check, ...] = (),
        index_method: str | None = None,
        index_ops: str | None = None,
        index_with: tuple[tuple[str, str], ...] = (),
    ) -> None:
        global _ORDER
        _ORDER += 1
        self.pg_type = pg_type
        self.primary_key = primary_key
        self.nullable = nullable
        self.unique = unique
        self.indexed = indexed
        #: The access method for this column's index ("btree"/"gin"), or None
        #: when it has no index. Kept separate from `indexed` so the column
        #: fingerprint (which encodes only index *presence*) is unchanged.
        self.index_method = index_method
        #: The operator class this column's index is built with
        #: ("vector_cosine_ops"), or None for the access method's default. It
        #: is what makes an HNSW index answer a *cosine* search rather than an
        #: L2 one, so it belongs to the index rather than to the query.
        self.index_ops = index_ops
        #: Index-method options as ordered `(name, value)` pairs, rendered as
        #: `WITH (m = 16, ef_construction = 64)`. Ordered rather than a dict
        #: because the rendered DDL has to be byte-stable across runs.
        self.index_with = index_with
        self.default = default
        self.server_default = server_default
        self.references = references
        self.on_delete = on_delete
        self.on_update = on_update
        self.deferrable = deferrable
        self.checks = checks
        self.order = _ORDER
        self.python_name: str = ""
        # This column's contribution to a query's plan-cache key. `shape_of`
        # runs on every query and the name never changes after the model is
        # declared, so encoding it per request was pure repetition. Filled in
        # by `_clone`, once the column is attached and named.
        self.shape_ref: bytes = b""
        self.shape_projection: bytes = b""
        self.owner: type | None = None
        self.index: int = -1
        self.prototype: Column | None = None
        self._expression: ColumnExpr | None = None
        # Every write to this column goes through this one callable: the type's
        # coercion with the checks fused into it. Until ModelMeta has applied
        # any narrowing, the type alone is all there is to enforce.
        self.validate: Any = pg_type.coerce

    def _clone(self, owner: type | None, python_name: str, index: int) -> Column:
        copy = Column(
            self.pg_type,
            primary_key=self.primary_key,
            nullable=self.nullable,
            unique=self.unique,
            indexed=self.indexed,
            default=self.default,
            server_default=self.server_default,
            references=self.references,
            on_delete=self.on_delete,
            on_update=self.on_update,
            deferrable=self.deferrable,
            checks=self.checks,
            index_method=self.index_method,
            index_ops=self.index_ops,
            index_with=self.index_with,
        )
        copy.order = self.order
        copy.owner = owner
        copy.python_name = python_name
        encoded = python_name.encode("utf-8")
        copy.shape_ref = b"c" + encoded
        copy.shape_projection = b"p" + encoded
        copy.index = index
        copy.prototype = self.prototype or self
        copy._expression = ColumnExpr(copy)
        return copy

    def _narrow(self, checks: tuple[Check, ...]) -> None:
        """Append checks to this clone, tightening the rule for one model.

        Only ever appends. The inherited checks keep their place at the front of
        the chain and keep running, so a narrowed column accepts a subset of
        what the column it narrows accepts -- never a superset.
        """
        self.checks = (*self.checks, *checks)

    def _compile(self, owner: str) -> None:
        """Fuse the type and the checks. Called once the checks are final."""
        self.validate = compile_column_validator(self, owner)

    @property
    def database_name(self) -> str:
        return self.python_name

    @property
    def generated(self) -> bool:
        """Whether PostgreSQL computes this column rather than the application.

        A property rather than a stored flag: it is read on the *rare* branch of
        the constructor (a column with no supplied value and no default) and by
        the migration descriptor, never per assignment -- assignment is refused
        by the type's own `coerce`, which is already on that path.
        """
        return isinstance(self.pg_type, GeneratedType)

    @property
    def expression(self) -> ColumnExpr:
        if self._expression is None:
            raise DeclarationError(
                f"column {self.python_name or '<unnamed>'!r} is not attached to a model"
            )
        return self._expression

    def __get__(self, obj: Any, owner: type | None = None) -> Any:
        if obj is None:
            return self.expression
        return obj._orm_get(self.index)

    def __set__(self, obj: Any, value: Any) -> None:
        obj._orm_set(self.index, value)

    def __delete__(self, obj: Any) -> None:
        raise AttributeError(f"cannot delete mapped column {self.python_name!r}")

    def __repr__(self) -> str:
        owner = getattr(self.owner, "__name__", "?")
        return f"<Column {owner}.{self.python_name} {self.pg_type.name}>"


_FK_ACTIONS = frozenset({"no action", "restrict", "cascade", "set null", "set default"})

#: The access methods wreath renders `CREATE INDEX ... USING <method>` for.
#: `hnsw` and `ivfflat` are pgvector's; both are only meaningful on a vector
#: column, and both are expensive to build, which the migrations guide says out
#: loud rather than leaving to be discovered during a deploy.
#: `gist` is here for `point`, and unlike `hnsw`/`ivfflat` it needs no
#: extension: core PostgreSQL ships the `point_ops` operator class, which is
#: what makes a proximity search indexable on a stock server.
_INDEX_METHODS = frozenset({"btree", "gin", "gist", "hnsw", "ivfflat"})

#: The access methods whose index descriptor carries no operator-class field at
#: all. Everything else spells its operator class and its method options, which
#: is what makes a *default* operator class comparable: the catalog records a
#: default as the empty string, so the desired side has to know this database's
#: defaults to write the same thing. `wreath.migrations._registry_descriptor`
#: and `wreath.orm.introspection.declared_index_methods` both read this, and
#: `_SINGLE_CATALOG_SQL`'s `am.amname IN ('btree', 'gin')` is the third copy --
#: it is SQL and cannot import.
_IMPLICIT_OPCLASS_METHODS = frozenset({"btree", "gin"})

#: An operator class or an index option name, as PostgreSQL spells them.
#: Matched with `fullmatch`, never `match`: `$` also matches immediately before
#: a trailing newline, so `^...$` accepted `"vector_cosine_ops\n"` into DDL text.
_INDEX_TOKEN = re.compile(r"[a-z_][a-z0-9_]*")
#: An index option value: a bare identifier such as `on`, or a number, which may
#: be negative, fractional, or written with an exponent. Deliberately narrow: it
#: is rendered into DDL text rather than bound, so anything else is refused at
#: declaration rather than quoted at render.
#:
#: Spelled as an alternation rather than a character class because a class
#: containing `-` accepts it in *any* position, and `index_with={"m": "--"}`
#: rendered `WITH (m = --)` -- a comment introducer. Not an injection (nothing
#: can follow it on the line, and `--` closes nothing), but a value that opens a
#: comment is not a value, and the class already claimed to admit only numbers
#: and identifiers.
_INDEX_OPTION_VALUE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE]-?[0-9]+)?|[A-Za-z_][A-Za-z0-9_.]*")


def _resolve_index_ops(index_method: str | None, index_ops: str | None) -> str | None:
    if index_ops is None:
        return None
    if index_method is None:
        raise DeclarationError("index_ops= requires an index= on the same column")
    if not isinstance(index_ops, str) or not _INDEX_TOKEN.fullmatch(index_ops):
        raise DeclarationError(
            f"index_ops={index_ops!r} must be an operator class name such as 'vector_cosine_ops'"
        )
    return index_ops


def _resolve_index_with(index_method: str | None, index_with: Any) -> tuple[tuple[str, str], ...]:
    if index_with is None:
        return ()
    if index_method is None:
        raise DeclarationError("index_with= requires an index= on the same column")
    if not isinstance(index_with, Mapping):
        raise DeclarationError(
            f"index_with= must be a mapping of option names to values, got {index_with!r}"
        )
    resolved: list[tuple[str, str]] = []
    for name, value in index_with.items():
        if not isinstance(name, str) or not _INDEX_TOKEN.fullmatch(name):
            raise DeclarationError(
                f"index_with option name {name!r} is not a PostgreSQL identifier"
            )
        if isinstance(value, bool):
            text = "on" if value else "off"
        elif isinstance(value, (int, float)):
            text = repr(value)
        elif isinstance(value, str):
            text = value
        else:
            raise DeclarationError(
                f"index_with[{name!r}] must be a number, string, or bool, got {value!r}"
            )
        if not _INDEX_OPTION_VALUE.fullmatch(text):
            raise DeclarationError(
                f"index_with[{name!r}]={value!r} is not a plain index-option value; "
                "it would be rendered into DDL text rather than bound"
            )
        resolved.append((name, text))
    # Sorted, not dict-ordered. `pg_class.reloptions` echoes back the order the
    # index was created with, so the comparison only holds if the order wreath
    # emits is a function of the option *names* -- a reordered dict literal
    # would otherwise show up as drift on the next migration run.
    resolved.sort()
    return tuple(resolved)


def _resolve_index(index: bool | str) -> tuple[bool, str | None]:
    """Normalize `index=` into an (present, method) pair.

    `False`/`None` -> no index; `True` -> btree; a method name -> that
    access method. `indexed` stays a plain bool so the column fingerprint is
    unchanged for existing btree/none columns.
    """
    if index is False or index is None:
        return False, None
    if index is True:
        return True, "btree"
    if isinstance(index, str) and index in _INDEX_METHODS:
        return True, index
    raise DeclarationError(
        f"index= must be True, False, or one of {sorted(_INDEX_METHODS)}, got {index!r}"
    )


def column(
    pg_type: PgType,
    *,
    primary_key: bool = False,
    nullable: bool = False,
    unique: bool = False,
    index: bool | str = False,
    index_ops: str | None = None,
    index_with: Mapping[str, Any] | None = None,
    default: Any = MISSING,
    server_default: str | None = None,
    references: Any = None,
    on_delete: str | None = None,
    on_update: str | None = None,
    deferrable: bool = False,
    check: Any = None,
) -> Any:
    """Declare a mapped column.

    `default` is a Python value or a zero-argument callable applied when a
    constructor omits the field. `index=True` declares one ordinary btree index
    on this column; `index="gin"` declares a GIN index instead (the right
    choice for `Jsonb` and `Array` columns queried with the containment and
    key operators), and `index="hnsw"` or `index="ivfflat"` declares a pgvector
    index on a `Vector` column.

    `index_ops` names the operator class the index is built with, which is what
    decides *which* distance an approximate index can answer:

    ```python
    embedding: Mapped[list[float]] = column(
        Vector(1536),
        index="hnsw",
        index_ops="vector_cosine_ops",
        index_with={"m": 16, "ef_construction": 64},
    )
    ```
    `index_with` passes index-method options through to
    `WITH (m = 16, ef_construction = 64)`. Both require an `index=`, and both
    are rendered into DDL rather than bound, so their values are restricted to
    plain identifiers and numbers. `server_default` names a database-side
    default, which makes the column optional on insert and returned by
    `RETURNING`. `references` takes another model's column expression
    (`references=User.id`) and records a foreign key.

    `check` takes one constraint from `wreath.orm.constraints`, or a sequence
    of them, applied in order after the type has accepted the value:

    ```python
    salary: Mapped[int] = column(Int64, check=Ge(0))
    name: Mapped[str] = column(Text, check=[Length(1, 200), Pattern(r"\\S")])
    ```
    Checks run on every write -- the constructor, assignment, and a request
    body -- and cost one comparison each, not one call each. A subclass adds to
    them with `narrow()`; nothing removes them.
    """
    if not isinstance(pg_type, PgType):
        raise DeclarationError(f"column() requires a PgType from wreath.orm.types, got {pg_type!r}")
    if primary_key and nullable:
        raise DeclarationError("a primary-key column cannot be nullable")
    if references is not None and not isinstance(references, ColumnExpr):
        raise DeclarationError("references= requires a model column expression such as User.id")
    if server_default is not None and not isinstance(server_default, str):
        raise DeclarationError("server_default= must be SQL text")
    for label, action in (("on_delete", on_delete), ("on_update", on_update)):
        if action is not None and action not in _FK_ACTIONS:
            raise DeclarationError(f"{label}={action!r} must be one of {sorted(_FK_ACTIONS)}")
    if (on_delete or on_update or deferrable) and references is None:
        raise DeclarationError(
            "on_delete=/on_update=/deferrable= only apply to a references= column"
        )
    indexed, index_method = _resolve_index(index)
    return Column(
        pg_type,
        primary_key=primary_key,
        nullable=nullable,
        unique=unique,
        indexed=indexed,
        index_method=index_method,
        index_ops=_resolve_index_ops(index_method, index_ops),
        index_with=_resolve_index_with(index_method, index_with),
        default=default,
        server_default=server_default,
        references=references,
        on_delete=on_delete,
        on_update=on_update,
        deferrable=deferrable,
        checks=collect_checks(check, f"column({pg_type.name})"),
    )


def resolve_default(spec: Any) -> Any:
    """Produce a fresh default value for `spec`, calling factory defaults."""
    default = spec.default
    if default is MISSING:
        return MISSING
    if callable(default):
        return default()
    return default


# Values whose canonical byte encoding is stable across processes. Anything
# else must be rejected while compiling rather than silently fingerprinted by
# repr() or a randomized hash.
_ENCODABLE = (bool, int, float, str, bytes, uuid.UUID, datetime.date, datetime.datetime)


def encode_default(value: Any) -> bytes:
    """A deterministic tagged encoding of a column default."""
    if value is MISSING:
        return b"\x00"
    if value is None:
        return b"\x01"
    if callable(value):
        module = getattr(value, "__module__", None)
        qualname = getattr(value, "__qualname__", None)
        if not module or not qualname or "<" in qualname:
            raise DeclarationError(
                f"default callable {value!r} has no stable name; use a module-level "
                "function so the model fingerprint stays deterministic"
            )
        return b"\x02" + f"{module}.{qualname}".encode()
    if isinstance(value, bool):
        return b"\x03" + (b"1" if value else b"0")
    if isinstance(value, int):
        return b"\x04" + repr(value).encode("ascii")
    if isinstance(value, float):
        return b"\x05" + float.hex(value).encode("ascii")
    if isinstance(value, str):
        return b"\x06" + value.encode("utf-8")
    if isinstance(value, bytes):
        return b"\x07" + value
    if isinstance(value, uuid.UUID):
        return b"\x08" + value.bytes
    if isinstance(value, datetime.datetime):
        return b"\x09" + value.isoformat().encode("ascii")
    if isinstance(value, datetime.date):
        return b"\x0a" + value.isoformat().encode("ascii")
    raise DeclarationError(
        f"default {value!r} of type {type(value).__name__} has no deterministic "
        f"encoding; supported defaults are None, {', '.join(t.__name__ for t in _ENCODABLE)}, "
        "or a module-level callable"
    )


__all__ = [
    "MISSING",
    "Column",
    "Mapped",
    "UnloadedAttributeError",
    "column",
    "encode_default",
    "resolve_default",
]
