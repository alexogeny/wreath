"""Immutable query objects.

Every builder method returns a new `Select`; nothing mutates in place, so a
query can be built once at import time and reused per request without a
defensive copy.
"""

from __future__ import annotations

from typing import Any

from .errors import DeclarationError
from .expressions import ColumnExpr, OrderExpr, Predicate
from .relations import LoadOption


class Select:
    """A compiled-on-demand SELECT for one model."""

    __slots__ = (
        "for_update_",
        "includes",
        "limit_",
        "model",
        "offset_",
        "orderings",
        "predicates",
        "projection",
    )

    def __init__(
        self,
        model: type,
        projection: tuple[ColumnExpr, ...],
        predicates: tuple[Predicate, ...],
        includes: tuple[LoadOption, ...],
        orderings: tuple[OrderExpr, ...],
        limit_: int | None,
        offset_: int | None,
        for_update_: bool,
    ) -> None:
        self.model = model
        self.projection = projection
        self.predicates = predicates
        self.includes = includes
        self.orderings = orderings
        self.limit_ = limit_
        self.offset_ = offset_
        self.for_update_ = for_update_

    @classmethod
    def build(cls, model: type, fields: tuple[Any, ...]) -> Select:
        # `Model.select()` with no arguments -- every column -- is the common
        # case and the one on the request path. Guarding it skips building and
        # draining a generator whose only job would be to produce the empty
        # tuple the caller already has.
        if not fields:
            return cls(model, (), (), (), (), None, None, False)
        projection = tuple(_check_field(model, item) for item in fields)
        return cls(model, projection, (), (), (), None, None, False)

    def _replace(self, **changes: Any) -> Select:
        return Select(
            changes.get("model", self.model),
            changes.get("projection", self.projection),
            changes.get("predicates", self.predicates),
            changes.get("includes", self.includes),
            changes.get("orderings", self.orderings),
            changes.get("limit_", self.limit_),
            changes.get("offset_", self.offset_),
            changes.get("for_update_", self.for_update_),
        )

    def where(self, *predicates: Predicate) -> Select:
        """Add predicates; repeated calls and multiple arguments combine with AND."""
        for item in predicates:
            if not isinstance(item, Predicate):
                raise TypeError(
                    f"where() takes SQL predicates such as User.id == 1, got {item!r}"
                )
        return self._replace(predicates=self.predicates + tuple(predicates))

    def rebound(self, predicates: tuple[Predicate, ...]) -> Select:
        """This query with its predicates *replaced* rather than extended.

        Every other builder method adds to a query; this one swaps one set of
        predicates for another, which is what a declared query in
        `wreath.queries` needs — it fixes a shape once and substitutes each
        call's values into it. The caller owes the invariant that the
        replacements have the same tree structure and column types as the
        originals; anything else changes the plan-cache key, and a declared
        query compiling once per shape is the whole point of declaring it.
        """
        return self._replace(predicates=predicates)

    def include(self, *load_options: LoadOption) -> Select:
        """Load relationships with this query."""
        for item in load_options:
            if not isinstance(item, LoadOption):
                raise TypeError(
                    "include() takes load options such as User.posts.selectin(), "
                    f"got {item!r}"
                )
        return self._replace(includes=self.includes + tuple(load_options))

    def order_by(self, *expressions: Any) -> Select:
        orderings = []
        for item in expressions:
            if isinstance(item, ColumnExpr):
                item = item.asc()
            elif not isinstance(item, OrderExpr):
                raise TypeError(
                    f"order_by() takes columns or .asc()/.desc(), got {item!r}"
                )
            _check_field(self.model, item.expression)
            orderings.append(item)
        return self._replace(orderings=self.orderings + tuple(orderings))

    def limit(self, value: int) -> Select:
        return self._replace(limit_=_check_bound(value, "limit"))

    def offset(self, value: int) -> Select:
        return self._replace(offset_=_check_bound(value, "offset"))

    def paginate(self, page: int, size: int) -> Select:
        """Shape this query for one page: `LIMIT size OFFSET (page - 1) * size`.

        Pure query-shaping; execution and the total count live in
        `wreath.pagination`.
        """
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError(f"page must be an integer >= 1, got {page!r}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ValueError(f"size must be an integer >= 1, got {size!r}")
        return self.limit(size).offset((page - 1) * size)

    def for_update(self) -> Select:
        """Lock matched rows; requires a write session inside a transaction."""
        return self._replace(for_update_=True)

    def __repr__(self) -> str:
        return (
            f"<Select {self.model.__name__} "
            f"fields={len(self.projection) or 'all'} "
            f"where={len(self.predicates)}>"
        )


def _check_field(model: type, field: Any) -> ColumnExpr:
    if not isinstance(field, ColumnExpr):
        raise TypeError(
            f"select() takes model columns such as {model.__name__}.id, got {field!r}"
        )
    if field.column.owner is not model:
        raise DeclarationError(
            f"{getattr(field.column.owner, '__name__', '?')}."
            f"{field.column.python_name} is not a column of {model.__name__}"
        )
    return field


def _check_bound(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {value!r}")
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


__all__ = ["Select"]
