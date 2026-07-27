"""Table-level schema declarations found in a model body by type.

These sit alongside :func:`narrow` and :func:`rule`: you declare them as plain
class attributes and the metaclass collects them by type, so the attribute name
is only documentation::

    class Membership(Model, table="memberships", schema=TENANT_SCHEMA):
        org_id: Mapped[int] = column(Int64)
        user_id: Mapped[int] = column(Int64)
        _identity = unique("org_id", "user_id")
        _lookup = index("user_id", "org_id")

Constraints and indexes declared this way are named deterministically by the
migration engine (never by you), so a downgrade drops exactly what an upgrade
created. Columns are named by their database name (which is the attribute name).
"""

from __future__ import annotations

from .errors import DeclarationError


class Unique:
    """A composite ``UNIQUE`` constraint over two or more columns."""

    __slots__ = ("columns",)

    def __init__(self, columns: tuple[str, ...]) -> None:
        self.columns = columns

    def __repr__(self) -> str:
        return f"unique({', '.join(map(repr, self.columns))})"


class Eq:
    """``column = literal``."""

    __slots__ = ("column", "value")

    def __init__(self, column: str, value: object) -> None:
        self.column = column
        self.value = value

    def __repr__(self) -> str:
        return f"eq({self.column!r}, {self.value!r})"


class IsNull:
    """``column IS NULL``, or ``IS NOT NULL`` when *negated*."""

    __slots__ = ("column", "negated")

    def __init__(self, column: str, negated: bool) -> None:
        self.column = column
        self.negated = negated

    def __repr__(self) -> str:
        return f"is_not_null({self.column!r})" if self.negated else f"is_null({self.column!r})"


class InValues:
    """``column IN (...)`` over two or more literals."""

    __slots__ = ("column", "values")

    def __init__(self, column: str, values: tuple[object, ...]) -> None:
        self.column = column
        self.values = values

    def __repr__(self) -> str:
        return f"one_of({self.column!r}, {list(self.values)!r})"


class AllOf:
    """Two or more predicates joined by ``AND``."""

    __slots__ = ("operands",)

    def __init__(self, operands: tuple[object, ...]) -> None:
        self.operands = operands

    def __repr__(self) -> str:
        return f"all_of({', '.join(map(repr, self.operands))})"


#: Every predicate shape a partial index may carry. Deliberately small: each one
#: has a PostgreSQL normal form this codebase has measured and can reproduce
#: exactly, which is what keeps ``detect`` from reporting drift forever against
#: an index it just created. See :mod:`wreath.orm._index_predicate`.
PREDICATES = (Eq, IsNull, InValues, AllOf)


class Index:
    """A multi-column btree index, optionally ``UNIQUE`` and optionally partial.

    ``where`` carries a predicate built from :func:`eq`, :func:`is_null`,
    :func:`is_not_null`, :func:`one_of` and :func:`all_of`. Columns are named by
    string for the same reason :func:`index` names them by string -- a model
    cannot refer to its own attributes from inside its own class body, so the
    query language (``Job.state == "ready"``) is not available here.

    ``where_sql`` is the rendered predicate in PostgreSQL's own normal form, set
    by the registry once column types are known. It is ``None`` until then.
    """

    __slots__ = ("columns", "unique", "where", "where_sql")

    def __init__(
        self,
        columns: tuple[str, ...],
        unique: bool,
        where: object = None,
        where_sql: str | None = None,
    ) -> None:
        self.columns = columns
        self.unique = unique
        self.where = where
        self.where_sql = where_sql

    def __repr__(self) -> str:
        flag = ", unique=True" if self.unique else ""
        clause = f", where={self.where!r}" if self.where is not None else ""
        return f"index({', '.join(map(repr, self.columns))}{flag}{clause})"


def _check_columns(columns: tuple[str, ...], where: str) -> None:
    if not columns:
        raise DeclarationError(f"{where} needs at least one column name")
    for name in columns:
        if not isinstance(name, str) or not name:
            raise DeclarationError(
                f"{where} takes column-name strings such as \"user_id\", got {name!r}"
            )
    if len(set(columns)) != len(columns):
        raise DeclarationError(f"{where} names the same column twice")


def unique(*columns: str) -> Unique:
    """Declare a table-level composite unique constraint by column name."""
    _check_columns(columns, "unique()")
    return Unique(columns)


def _check_predicate_column(column: object, where: str) -> str:
    if not isinstance(column, str) or not column:
        raise DeclarationError(
            f'{where} takes a column-name string such as "state", got {column!r}'
        )
    return column


def eq(column: str, value: object) -> Eq:
    """``column = value``, for a text, integer, or boolean column."""
    return Eq(_check_predicate_column(column, "eq()"), value)


def is_null(column: str) -> IsNull:
    """``column IS NULL``."""
    return IsNull(_check_predicate_column(column, "is_null()"), False)


def is_not_null(column: str) -> IsNull:
    """``column IS NOT NULL`` -- the shape a unique partial index usually wants."""
    return IsNull(_check_predicate_column(column, "is_not_null()"), True)


def one_of(column: str, values: object) -> InValues:
    """``column IN (...)``, over two or more literals of one type.

    A single-element list is refused: PostgreSQL rewrites ``IN ('a')`` to
    ``= 'a'``, so the catalog would never match what was declared. Use
    :func:`eq`, which is what that means anyway.
    """
    name = _check_predicate_column(column, "one_of()")
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise DeclarationError(f"one_of() takes a list of values, got {values!r}")
    items = tuple(values)
    if len(items) < 2:
        raise DeclarationError(
            f"one_of({name!r}, {list(items)!r}) needs two or more values; PostgreSQL "
            "rewrites a one-element IN to =, so the catalog would never match the "
            "declaration -- use eq() instead"
        )
    return InValues(name, items)


def all_of(*predicates: object) -> AllOf:
    """Two or more predicates joined by ``AND``."""
    if len(predicates) < 2:
        raise DeclarationError("all_of() joins two or more predicates")
    for predicate in predicates:
        if isinstance(predicate, AllOf):
            raise DeclarationError(
                "all_of() does not nest; pass every predicate to one all_of() call"
            )
        if not isinstance(predicate, PREDICATES):
            raise DeclarationError(
                f"all_of() takes predicates from eq(), is_null(), is_not_null() and "
                f"one_of(), got {predicate!r}"
            )
    return AllOf(predicates)


def index(*columns: str, unique: bool = False, where: object = None) -> Index:
    """Declare a multi-column btree index by column name.

    ``unique=True`` makes it a unique index; ``where=`` makes it partial::

        _claim = index("queue", "run_at", where=eq("state", "ready"))
        _dedup = index("queue", "dedup_key", unique=True, where=is_not_null("dedup_key"))
    """
    _check_columns(columns, "index()")
    if where is not None and not isinstance(where, PREDICATES):
        raise DeclarationError(
            "index(where=...) takes a predicate from eq(), is_null(), is_not_null(), "
            f"one_of() or all_of(), got {where!r}"
        )
    return Index(columns, unique, where)


__all__ = [
    "AllOf",
    "Eq",
    "Index",
    "IsNull",
    "InValues",
    "Unique",
    "all_of",
    "eq",
    "index",
    "is_not_null",
    "is_null",
    "one_of",
    "unique",
]
