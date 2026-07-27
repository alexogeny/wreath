"""Immutable SQL expression nodes.

Operator overloads only build nodes. Nothing here inspects a database, holds a
connection, or renders SQL text; the compiler owns rendering and the session
owns execution.

Nodes compare by identity, because `==` is overloaded to *build* an equality
predicate rather than answer one.
"""

from __future__ import annotations

from typing import Any

from .errors import DeclarationError
from .types import Json, Jsonb, Text, TextArray, _ArrayType

# Operator tokens are matched against an allowlist in the compiler, so a node
# can never inject SQL even if one is constructed directly.
EQ = "="
NE = "<>"
LT = "<"
LE = "<="
GT = ">"
GE = ">="
LIKE = "LIKE"
ILIKE = "ILIKE"
AND = "AND"
OR = "OR"
NOT = "NOT"
IS_NULL = "IS NULL"
IS_NOT_NULL = "IS NOT NULL"
IN = "IN"
NOT_IN = "NOT IN"

# JSONB and array operators. The two-word "= ANY"/"= ALL" tokens are rendered
# specially by the compiler (`value = ANY(column)`); the rest render as an
# ordinary `left <op> right` and only widen the operator allowlist.
CONTAINS = "@>"
CONTAINED_BY = "<@"
HAS_KEY = "?"
HAS_ANY = "?|"
HAS_ALL = "?&"
PATH_TEXT = "#>>"
PATH_JSON = "#>"
OVERLAPS = "&&"
ANY_EQ = "= ANY"
ALL_EQ = "= ALL"

ASC = "ASC"
DESC = "DESC"


class Expression:
    """Base class for everything that renders into a SQL fragment."""

    __slots__ = ()

    __hash__ = object.__hash__


class Predicate(Expression):
    """An expression that renders where a boolean is expected."""

    __slots__ = ()

    def __and__(self, other: Predicate) -> BooleanExpr:
        return BooleanExpr(AND, (self, _as_predicate(other)))

    def __or__(self, other: Predicate) -> BooleanExpr:
        return BooleanExpr(OR, (self, _as_predicate(other)))

    def __invert__(self) -> UnaryExpr:
        return UnaryExpr(NOT, self)

    def __bool__(self) -> bool:
        raise TypeError(
            "SQL predicates cannot be used in Python boolean context; "
            "combine them with & and | rather than and/or"
        )


def _as_predicate(value: Any) -> Predicate:
    if not isinstance(value, Predicate):
        raise TypeError(f"expected a SQL predicate, got {type(value).__name__}")
    return value


class ColumnExpr(Expression):
    """A reference to one model column."""

    __slots__ = ("column",)

    def __init__(self, column: Any) -> None:
        self.column = column

    @property
    def model(self) -> type:
        return self.column.owner

    def _compare(self, operator: str, other: Any) -> BinaryExpr:
        return BinaryExpr(operator, self, _bind(self.column, other))

    def __eq__(self, other: Any) -> BinaryExpr:  # ty: ignore[invalid-method-override]
        if other is None:
            raise TypeError(
                "comparing a column to None is ambiguous in SQL; use .is_null()"
            )
        return self._compare(EQ, other)

    def __ne__(self, other: Any) -> BinaryExpr:  # ty: ignore[invalid-method-override]
        if other is None:
            raise TypeError(
                "comparing a column to None is ambiguous in SQL; use .is_not_null()"
            )
        return self._compare(NE, other)

    def __lt__(self, other: Any) -> BinaryExpr:
        return self._compare(LT, other)

    def __le__(self, other: Any) -> BinaryExpr:
        return self._compare(LE, other)

    def __gt__(self, other: Any) -> BinaryExpr:
        return self._compare(GT, other)

    def __ge__(self, other: Any) -> BinaryExpr:
        return self._compare(GE, other)

    __hash__ = object.__hash__

    def like(self, pattern: str) -> BinaryExpr:
        return self._compare(LIKE, pattern)

    def ilike(self, pattern: str) -> BinaryExpr:
        return self._compare(ILIKE, pattern)

    def is_null(self) -> UnaryExpr:
        return UnaryExpr(IS_NULL, self)

    def is_not_null(self) -> UnaryExpr:
        return UnaryExpr(IS_NOT_NULL, self)

    def in_(self, values: Any) -> InExpr:
        return InExpr(IN, self, _bind_many(self.column, values))

    def not_in(self, values: Any) -> InExpr:
        return InExpr(NOT_IN, self, _bind_many(self.column, values))

    # -- JSONB and array operators ----------------------------------------
    #
    # `contains`/`contained_by` work on both jsonb and array columns (the
    # operand takes the column's own type); the jsonb key operators require a
    # `Jsonb` column and the array operators require an `Array` column, both
    # checked at build time so a mistyped query fails at the call site.

    def contains(self, other: Any) -> BinaryExpr:
        """`self @> other` -- jsonb/array containment."""
        return BinaryExpr(CONTAINS, self, _bind(self.column, other))

    def contained_by(self, other: Any) -> BinaryExpr:
        """`self <@ other` -- reverse jsonb/array containment."""
        return BinaryExpr(CONTAINED_BY, self, _bind(self.column, other))

    def has_key(self, key: str) -> BinaryExpr:
        """`self ? key` -- the jsonb object has this top-level key."""
        self._require_jsonb("has_key")
        return BinaryExpr(HAS_KEY, self, _bind_as(key, Text))

    def has_any(self, keys: Any) -> BinaryExpr:
        """`self ?| keys` -- the jsonb object has any of these keys."""
        self._require_jsonb("has_any")
        return BinaryExpr(HAS_ANY, self, _bind_as(_nonempty(keys, "has_any"), TextArray))

    def has_all(self, keys: Any) -> BinaryExpr:
        """`self ?& keys` -- the jsonb object has all of these keys."""
        self._require_jsonb("has_all")
        return BinaryExpr(HAS_ALL, self, _bind_as(_nonempty(keys, "has_all"), TextArray))

    def path(self, elements: Any, *, as_json: bool = False) -> _JsonPath:
        """A jsonb path extraction completed by a comparison.

        `Model.data.path(["a", "b"]) == "x"` renders `(data #>> $1) = $2`.
        `as_json=True` extracts a sub-document with `#>` instead, usable with
        the jsonb operators.
        """
        if self.column.pg_type is not Json and self.column.pg_type is not Jsonb:
            raise DeclarationError(
                f".path() requires a json or jsonb column, not {self.column.pg_type.name}"
            )
        return _JsonPath(self, tuple(elements), as_json)

    def overlaps(self, other: Any) -> BinaryExpr:
        """`self && other` -- the arrays share at least one element."""
        self._require_array("overlaps")
        return BinaryExpr(
            OVERLAPS, self, _bind_as(_nonempty(other, "overlaps"), self.column.pg_type)
        )

    def any_eq(self, value: Any) -> BinaryExpr:
        """`value = ANY(self)` -- `value` is an element of the array."""
        element = self._require_array("any_eq")
        return BinaryExpr(ANY_EQ, _bind_as(value, element), self)

    def all_eq(self, value: Any) -> BinaryExpr:
        """`value = ALL(self)` -- every array element equals `value`."""
        element = self._require_array("all_eq")
        return BinaryExpr(ALL_EQ, _bind_as(value, element), self)

    def _require_jsonb(self, method: str) -> None:
        if self.column.pg_type is not Jsonb:
            raise DeclarationError(
                f".{method}() requires a Jsonb column, not {self.column.pg_type.name}"
            )

    def _require_array(self, method: str) -> Any:
        pg_type = self.column.pg_type
        if not isinstance(pg_type, _ArrayType):
            raise DeclarationError(
                f".{method}() requires an Array column, not {pg_type.name}"
            )
        return pg_type.element

    def asc(self) -> OrderExpr:
        return OrderExpr(self, ASC)

    def desc(self) -> OrderExpr:
        return OrderExpr(self, DESC)

    def __repr__(self) -> str:
        owner = getattr(self.column.owner, "__name__", "?")
        return f"<ColumnExpr {owner}.{self.column.python_name}>"


class RelatedColumnExpr(ColumnExpr):
    """A column reached by traversing relationships from the queried model.

    `Book.author.name` is one of these. It compares against `authors.name`
    while the query still selects books, so the compiler turns `path` into an
    INNER JOIN and renders this operand against that join's alias.

    Filtering is not loading: a query that only mentions `Book.author.name`
    joins `authors` to constrain rows and selects nothing from it, so
    `book.author` still raises unless the query also `.include()`s it. That
    keeps the rule that attribute access never performs I/O.

    A subclass of `ColumnExpr`, so every comparison, `in_`, and ordering
    method is inherited. Code that branches on `isinstance(node, ColumnExpr)`
    must therefore test for this type *first*.
    """

    __slots__ = ("path",)
    #: The relationship trail to this column. Declared so the type checker sees
    #: an attribute here, not the `path()` jsonb method inherited from ColumnExpr.
    path: tuple[Any, ...]

    def __init__(self, column: Any, path: tuple[Any, ...]) -> None:
        super().__init__(column)
        self.path = path

    def __repr__(self) -> str:
        trail = ".".join(item.python_name for item in self.path)
        return f"<RelatedColumnExpr {trail}.{self.column.python_name}>"


class ValueExpr(Expression):
    """One bound parameter. The value never reaches SQL text or a cache key."""

    __slots__ = ("pg_type", "value")

    def __init__(self, value: Any, pg_type: Any) -> None:
        self.value = value
        self.pg_type = pg_type

    def __repr__(self) -> str:
        return f"<ValueExpr {self.pg_type.name}>"


class BinaryExpr(Predicate):
    __slots__ = ("left", "operator", "right")

    def __init__(self, operator: str, left: Expression, right: Expression) -> None:
        self.operator = operator
        self.left = left
        self.right = right

    def __repr__(self) -> str:
        return f"<BinaryExpr {self.left!r} {self.operator} {self.right!r}>"


class InExpr(Predicate):
    """Membership against an explicit value list.

    Rendered as `IN ($1, $2, ...)` because the driver codecs exchange scalars
    rather than arrays; the operand count is therefore part of the query shape.
    """

    __slots__ = ("operator", "left", "values")

    def __init__(self, operator: str, left: Expression, values: tuple[ValueExpr, ...]) -> None:
        self.operator = operator
        self.left = left
        self.values = values

    def __repr__(self) -> str:
        return f"<InExpr {self.left!r} {self.operator} {len(self.values)} values>"


class BooleanExpr(Predicate):
    __slots__ = ("operands", "operator")

    def __init__(self, operator: str, operands: tuple[Predicate, ...]) -> None:
        self.operator = operator
        self.operands = operands

    def __repr__(self) -> str:
        return f"<BooleanExpr {self.operator} {len(self.operands)}>"


class UnaryExpr(Predicate):
    __slots__ = ("operand", "operator")

    def __init__(self, operator: str, operand: Expression) -> None:
        self.operator = operator
        self.operand = operand

    def __repr__(self) -> str:
        return f"<UnaryExpr {self.operator} {self.operand!r}>"


class OrderExpr:
    __slots__ = ("direction", "expression")

    def __init__(self, expression: ColumnExpr, direction: str) -> None:
        self.expression = expression
        self.direction = direction

    def __repr__(self) -> str:
        return f"<OrderExpr {self.expression!r} {self.direction}>"


def _bind(column: Any, value: Any) -> ValueExpr:
    if isinstance(value, Expression):
        raise TypeError(
            "column-to-column comparison is not supported; compare against a value"
        )
    # Coercing here rejects a mistyped comparison at the call site rather than
    # at execution, and guarantees the bound value matches the column's OID.
    return ValueExpr(column.pg_type.coerce(value), column.pg_type)


def _bind_many(column: Any, values: Any) -> tuple[ValueExpr, ...]:
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        raise TypeError("in_() requires an iterable of values")
    bound = tuple(_bind(column, item) for item in values)
    if not bound:
        raise ValueError("in_() requires at least one value")
    return bound


def _bind_as(value: Any, pg_type: Any) -> ValueExpr:
    """Bind `value` against an explicit type rather than the column's own.

    The jsonb key operators take `text`/`text[]` operands and `any_eq`
    takes the array's element type, so those bind through this rather than
    `_bind` (which would coerce against the column type).
    """
    if isinstance(value, Expression):
        raise TypeError(
            "column-to-column comparison is not supported; compare against a value"
        )
    return ValueExpr(pg_type.coerce(value), pg_type)


def _nonempty(values: Any, label: str) -> list[Any]:
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        raise TypeError(f"{label}() requires an iterable of values")
    materialized = list(values)
    if not materialized:
        raise ValueError(f"{label}() requires at least one value")
    return materialized


class _JsonPath:
    """A pending jsonb path extraction, completed by a comparison.

    `Model.data.path(["a", "b"])` renders nothing on its own; comparing it
    (or, for `as_json` paths, applying a jsonb operator) builds the predicate.
    Text paths compare against `text` (`#>>`); json paths against `jsonb`
    (`#>`). It is intentionally unhashable and cannot be used in a boolean
    context, for the same reason column expressions cannot.
    """

    __slots__ = ("_as_json", "_column", "_elements")

    __hash__ = None  # type: ignore[assignment]

    def __init__(self, column: ColumnExpr, elements: tuple[Any, ...], as_json: bool) -> None:
        if not elements:
            raise ValueError("path() requires at least one path element")
        self._column = column
        self._elements = elements
        self._as_json = as_json

    def _extract(self) -> BinaryExpr:
        operator = PATH_JSON if self._as_json else PATH_TEXT
        path = _bind_as([str(item) for item in self._elements], TextArray)
        return BinaryExpr(operator, self._column, path)

    def _operand_type(self) -> Any:
        return Jsonb if self._as_json else Text

    def _compare(self, operator: str, other: Any) -> BinaryExpr:
        return BinaryExpr(operator, self._extract(), _bind_as(other, self._operand_type()))

    def __bool__(self) -> bool:
        raise TypeError(
            "SQL predicates cannot be used in Python boolean context; "
            "combine them with & and | rather than and/or"
        )

    def __eq__(self, other: Any) -> BinaryExpr:  # ty: ignore[invalid-method-override]
        return self._compare(EQ, other)

    def __ne__(self, other: Any) -> BinaryExpr:  # ty: ignore[invalid-method-override]
        return self._compare(NE, other)

    def __lt__(self, other: Any) -> BinaryExpr:
        return self._compare(LT, other)

    def __le__(self, other: Any) -> BinaryExpr:
        return self._compare(LE, other)

    def __gt__(self, other: Any) -> BinaryExpr:
        return self._compare(GT, other)

    def __ge__(self, other: Any) -> BinaryExpr:
        return self._compare(GE, other)

    def like(self, pattern: str) -> BinaryExpr:
        return self._compare(LIKE, pattern)

    def ilike(self, pattern: str) -> BinaryExpr:
        return self._compare(ILIKE, pattern)

    def contains(self, other: Any) -> BinaryExpr:
        """`(data #> path) @> other` -- containment on the sub-document."""
        if not self._as_json:
            raise DeclarationError("contains() on a path requires path(..., as_json=True)")
        return BinaryExpr(CONTAINS, self._extract(), _bind_as(other, Jsonb))

    def has_key(self, key: str) -> BinaryExpr:
        """`(data #> path) ? key` -- key test on the sub-document."""
        if not self._as_json:
            raise DeclarationError("has_key() on a path requires path(..., as_json=True)")
        return BinaryExpr(HAS_KEY, self._extract(), _bind_as(key, Text))


def and_(*predicates: Predicate) -> Predicate:
    """Combine predicates with `AND`; a single predicate passes through."""
    if not predicates:
        raise ValueError("and_() requires at least one predicate")
    if len(predicates) == 1:
        return _as_predicate(predicates[0])
    return BooleanExpr(AND, tuple(_as_predicate(item) for item in predicates))


def or_(*predicates: Predicate) -> Predicate:
    """Combine predicates with `OR`; a single predicate passes through."""
    if not predicates:
        raise ValueError("or_() requires at least one predicate")
    if len(predicates) == 1:
        return _as_predicate(predicates[0])
    return BooleanExpr(OR, tuple(_as_predicate(item) for item in predicates))


def not_(predicate: Predicate) -> UnaryExpr:
    return UnaryExpr(NOT, _as_predicate(predicate))


__all__ = [
    "ALL_EQ",
    "AND",
    "ANY_EQ",
    "ASC",
    "CONTAINED_BY",
    "CONTAINS",
    "DESC",
    "EQ",
    "GE",
    "GT",
    "HAS_ALL",
    "HAS_ANY",
    "HAS_KEY",
    "ILIKE",
    "IN",
    "IS_NOT_NULL",
    "IS_NULL",
    "LE",
    "LIKE",
    "LT",
    "NE",
    "NOT",
    "NOT_IN",
    "OR",
    "OVERLAPS",
    "PATH_JSON",
    "PATH_TEXT",
    "BinaryExpr",
    "BooleanExpr",
    "ColumnExpr",
    "Expression",
    "InExpr",
    "OrderExpr",
    "Predicate",
    "UnaryExpr",
    "ValueExpr",
    "and_",
    "not_",
    "or_",
]
