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


class Index:
    """A multi-column btree index, optionally ``UNIQUE``."""

    __slots__ = ("columns", "unique")

    def __init__(self, columns: tuple[str, ...], unique: bool) -> None:
        self.columns = columns
        self.unique = unique

    def __repr__(self) -> str:
        flag = ", unique=True" if self.unique else ""
        return f"index({', '.join(map(repr, self.columns))}{flag})"


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


def index(*columns: str, unique: bool = False) -> Index:
    """Declare a multi-column btree index by column name (``unique=`` for a unique one)."""
    _check_columns(columns, "index()")
    return Index(columns, unique)


__all__ = ["Index", "Unique", "index", "unique"]
