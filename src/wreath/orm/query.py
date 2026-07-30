"""Immutable query objects.

Every builder method returns a new `Select`; nothing mutates in place, so a
query can be built once at import time and reused per request without a
defensive copy.
"""

from __future__ import annotations

from typing import Any

from .errors import DeclarationError
from .expressions import BinaryExpr, ColumnExpr, OrderExpr, Predicate
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
        "plain_orderings",
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
        #: Whether every ordering is a bare column, which is the shape the
        #: native plan-cache keyer and bind collector understand. An ordering by
        #: a *distance* carries a bound value and an operator, and both have to
        #: reach the key -- so those queries take the pure path deliberately
        #: rather than by catching an AttributeError out of C.
        self.plain_orderings = all(
            isinstance(item.expression, ColumnExpr) for item in orderings
        )
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
            # One type test for both refusals, and only then the operator
            # lookups: `where()` runs per query, and this is the guard, not the
            # common case. Two `isinstance` calls here cost a measurable
            # boundary crossing per request (`wreath-request-trace`).
            if isinstance(item, BinaryExpr):
                if item.is_distance:
                    # A distance is a number. PostgreSQL would refuse it here
                    # too, but with a message about "argument of WHERE must be
                    # boolean" rather than about the line that wrote it.
                    raise TypeError(
                        f"where() takes a predicate, and {item.operator!r} yields a "
                        "distance; compare it against a threshold "
                        "(embedding.cosine_distance(q) < 0.3) or order by it"
                    )
                if item.is_rank:
                    # Same shape, and the mistake is easier to make: `.rank()`
                    # reads like a filter. `.matches()` is the predicate; the
                    # rank orders what it kept.
                    raise TypeError(
                        f"where() takes a predicate, and {item.operator!r} yields a "
                        "relevance score; filter with search.matches(terms) and order "
                        "by search.rank(terms).desc(), or compare the rank against a "
                        "threshold"
                    )
        return self._replace(predicates=self.predicates + tuple(predicates))

    def rebound_orderings(self, orderings: tuple[OrderExpr, ...]) -> Select:
        """This query with its ORDER BY keys *replaced* rather than extended.

        The ordering counterpart of `rebound`, and it exists for the same
        caller: a declared vector search fixes `ORDER BY embedding <=> $n` at
        class-definition time and substitutes the query vector per call. Same
        invariant -- the replacements must have the same tree structure and
        column types, because anything else is a different plan-cache key.
        """
        return self._replace(orderings=orderings)

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
        """Order rows by columns, `.asc()`/`.desc()`, or a vector distance.

        A distance orders by similarity, which is the shape a vector search
        actually has:

        ```python
        Document.select().order_by(Document.embedding.cosine_distance(query))
        ```
        Ordering by a distance is what an HNSW or IVFFlat index answers; a
        distance in a `WHERE` is not, and the index will not be used for it.
        """
        orderings = []
        for item in expressions:
            if isinstance(item, ColumnExpr):
                item = item.asc()
            elif isinstance(item, BinaryExpr):
                item = item.asc()  # raises unless it is a distance
            elif not isinstance(item, OrderExpr):
                raise TypeError(
                    f"order_by() takes columns, .asc()/.desc(), or a vector "
                    f"distance, got {item!r}"
                )
            _check_ordering(self.model, item.expression)
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


def _check_ordering(model: type, expression: Any) -> None:
    """Check an ORDER BY key belongs to `model`.

    A bare column is checked as a projection is. A distance is checked through
    its left operand, which is the column the index would be on -- the right
    operand is the bound query vector and belongs to nobody.
    """
    if isinstance(expression, BinaryExpr):
        _check_field(model, expression.left)
        return
    _check_field(model, expression)


def _check_bound(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {value!r}")
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


__all__ = ["Select"]
