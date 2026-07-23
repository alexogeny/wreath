"""Column declarations and the descriptor that backs field access.

A ``column()`` in a class body is a *prototype*. ``ModelMeta`` clones one
descriptor per concrete model so a column declared on a shared mixin can hold
a different storage index in each model that inherits it.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from .constraints import Check, collect_checks, compile_column_validator
from .errors import DeclarationError, UnloadedAttributeError
from .expressions import ColumnExpr
from .types import PgType


class _Missing:
    """The absence of a default, distinct from a ``None`` default."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Any = _Missing()


class Mapped[T]:
    """Typing-only marker for a mapped column.

    ``Mapped[int]`` documents that class access yields a SQL expression and
    instance access yields an ``int``. It carries no runtime behavior: the
    registry compiles declarations from ``column()`` objects, never from
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
        "index",
        "nullable",
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
        checks: tuple[Check, ...] = (),
    ) -> None:
        global _ORDER
        _ORDER += 1
        self.pg_type = pg_type
        self.primary_key = primary_key
        self.nullable = nullable
        self.unique = unique
        self.indexed = indexed
        self.default = default
        self.server_default = server_default
        self.references = references
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
            checks=self.checks,
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


def column(
    pg_type: PgType,
    *,
    primary_key: bool = False,
    nullable: bool = False,
    unique: bool = False,
    index: bool = False,
    default: Any = MISSING,
    server_default: str | None = None,
    references: Any = None,
    check: Any = None,
) -> Any:
    """Declare a mapped column.

    ``default`` is a Python value or a zero-argument callable applied when a
    constructor omits the field. ``index=True`` declares one ordinary btree index
    on this column. ``server_default`` names a database-side
    default, which makes the column optional on insert and returned by
    ``RETURNING``. ``references`` takes another model's column expression
    (``references=User.id``) and records a foreign key.

    ``check`` takes one constraint from ``wreath.orm.constraints``, or a sequence
    of them, applied in order after the type has accepted the value::

        salary: Mapped[int] = column(Int64, check=Ge(0))
        name: Mapped[str] = column(Text, check=[Length(1, 200), Pattern(r"\\S")])

    Checks run on every write -- the constructor, assignment, and a request
    body -- and cost one comparison each, not one call each. A subclass adds to
    them with ``narrow()``; nothing removes them.
    """
    if not isinstance(pg_type, PgType):
        raise DeclarationError(
            f"column() requires a PgType from wreath.orm.types, got {pg_type!r}"
        )
    if primary_key and nullable:
        raise DeclarationError("a primary-key column cannot be nullable")
    if references is not None and not isinstance(references, ColumnExpr):
        raise DeclarationError(
            "references= requires a model column expression such as User.id"
        )
    if server_default is not None and not isinstance(server_default, str):
        raise DeclarationError("server_default= must be SQL text")
    return Column(
        pg_type,
        primary_key=primary_key,
        nullable=nullable,
        unique=unique,
        indexed=index,
        default=default,
        server_default=server_default,
        references=references,
        checks=collect_checks(check, f"column({pg_type.name})"),
    )


def resolve_default(spec: Any) -> Any:
    """Produce a fresh default value for ``spec``, calling factory defaults."""
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
