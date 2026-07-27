"""Pagination, sorting, and filtering for the ORM query builder.

A thin, safe layer over ``Select``: turn ``?page=&size=&sort=`` query parameters
into ``LIMIT``/``OFFSET``/``ORDER BY`` against an **allow-list** of columns (never
an arbitrary column name from the request), and return a ``Page`` with the total.

Consumer-wired, no ``app.py`` changes needed::

    from wreath.pagination import Page, PageParams, page_params, paginate
    from wreath.binding import Depends

    @app.get("/llamas")
    async def list_llamas(request, params: Annotated[PageParams, Depends(page_params)]):
        page = await paginate(session, Llama.select(), params,
                              allow_sort=("name", "created_at"))
        return page.as_dict()
"""

from __future__ import annotations

from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, cast

from .binding import Query
from .orm.query import Select

if TYPE_CHECKING:
    from .orm.model import Model


def _as_model(model: type) -> type[Model]:
    """Narrow a ``Select.model`` (typed ``type``) to a wreath model for the type
    checker, so the ORM-injected ``__wreath_*__`` class attributes resolve."""
    return cast("type[Model]", model)

DEFAULT_SIZE = 20
MAX_SIZE = 100

#: Highest page number a request may ask for. `LIMIT/OFFSET` makes the database
#: walk and discard every row before the offset, so an unbounded page number is
#: a full scan an anonymous caller can ask for repeatedly. Past this, use a
#: keyset filter -- which is what a page this deep should have been doing.
MAX_PAGE = 10_000

__all__ = [
    "DEFAULT_SIZE",
    "MAX_PAGE",
    "MAX_SIZE",
    "Page",
    "PageParams",
    "apply_filters",
    "apply_sort",
    "page_params",
    "paginate",
    "parse_sort",
    "sortable_fields",
]


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of results plus the totals needed to render pagination controls."""

    items: Sequence[T]
    total: int
    page: int
    size: int

    @property
    def pages(self) -> int:
        if self.size <= 0:
            return 0
        return (self.total + self.size - 1) // self.size

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": list(self.items),
            "total": self.total,
            "page": self.page,
            "size": self.size,
            "pages": self.pages,
            "has_next": self.has_next,
            "has_prev": self.has_prev,
        }


@dataclass(frozen=True, slots=True)
class PageParams:
    """Normalized paging + sort request. ``sort`` items may be ``-field`` for DESC."""

    page: int = 1
    size: int = DEFAULT_SIZE
    sort: tuple[str, ...] = ()


def parse_sort(raw: str) -> tuple[str, ...]:
    """``"name,-created_at"`` -> ``("name", "-created_at")``; blank -> ``()``."""
    if not raw:
        return ()
    return tuple(token.strip() for token in raw.split(",") if token.strip())


def page_params(
    page: Annotated[int, Query(minimum=1, maximum=MAX_PAGE)] = 1,
    size: Annotated[int, Query(minimum=1, maximum=MAX_SIZE)] = DEFAULT_SIZE,
    sort: Annotated[str, Query()] = "",
) -> PageParams:
    """A ``Depends``-able that binds ``?page=&size=&sort=`` into ``PageParams``."""
    return PageParams(page=min(page, MAX_PAGE), size=size, sort=parse_sort(sort))


def sortable_fields(model: type) -> tuple[str, ...]:
    """Every column name of ``model`` -- the default sort/filter allow-list."""
    return tuple(_as_model(model).__wreath_column_map__)


def _column(model: type, name: str, allowed: frozenset[str], what: str) -> Any:
    if name not in allowed:
        raise ValueError(f"cannot {what} by {name!r}; not in the allow-list")
    return getattr(model, name)


def apply_sort(query: Select, sort: Iterable[str], *, allow: Iterable[str] | None = None) -> Select:
    """Apply ``sort`` tokens (``field`` / ``-field``) against an allow-list.

    All tokens are folded into a *single* ``order_by`` call. ``Select`` is
    immutable, so a per-token ``order_by`` would recopy a growing tuple on every
    step -- O(k^2) in the request-controlled token count. Building the orderings
    once and applying them together keeps this O(k).
    """
    model = _as_model(query.model)
    allowed = frozenset(allow) if allow is not None else frozenset(model.__wreath_column_map__)
    orderings = []
    for token in sort:
        descending = token[:1] == "-"
        name = token[1:] if descending else token
        column = _column(model, name, allowed, "sort")
        orderings.append(column.desc() if descending else column.asc())
    return query.order_by(*orderings) if orderings else query


def apply_filters(
    query: Select, filters: Mapping[str, Any], *, allow: Iterable[str] | None = None
) -> Select:
    """Apply equality filters ``{field: value}`` against an allow-list.

    Values are handed to the ORM, which coerces/validates them against the
    column's type -- an out-of-range or wrong-typed value fails at bind time.
    Richer operators (ranges, ``in``, ``ilike``) are a deliberate follow-up.
    """
    model = _as_model(query.model)
    allowed = frozenset(allow) if allow is not None else frozenset(model.__wreath_column_map__)
    predicates = []
    for name, value in filters.items():
        column = _column(model, name, allowed, "filter")
        predicates.append(column == value)
    # One combined ``where`` call: repeated per-filter calls would recopy the
    # immutable predicate tuple each time -- O(k^2) in the filter count.
    return query.where(*predicates) if predicates else query


async def paginate(
    session: Any,
    query: Select,
    params: PageParams,
    *,
    allow_sort: Iterable[str] | None = None,
    total: int | Awaitable[int] | None = None,
) -> Page[Any]:
    """Fetch one page of ``query`` and its total.

    Pass ``total`` (an int or awaitable) when you already have an efficient count;
    otherwise a correctness-first fallback counts primary keys (see ``_count``).
    """
    if params.sort:
        query = apply_sort(query, params.sort, allow=allow_sort)
    if total is None:
        resolved_total = await _count(session, query)
    elif isinstance(total, int):
        resolved_total = total
    else:
        resolved_total = await total
    items = await session.fetch(query.paginate(params.page, params.size))
    return Page(items=items, total=resolved_total, page=params.page, size=params.size)


async def _count(session: Any, query: Select) -> int:
    """Total rows matching ``query`` (ignoring paging/order).

    Uses the session's efficient ``SELECT COUNT(*)`` (``Session.count``) when it
    exposes one, so counting a large result set is a single aggregate round trip
    rather than transferring every matching row. Falls back to re-projecting to
    the primary key and counting client-side for a minimal session that only
    implements ``fetch`` (e.g. a lightweight test double).
    """
    counter = getattr(session, "count", None)
    if callable(counter):
        return await counter(query)
    model = _as_model(query.model)
    primary_key = model.__wreath_primary_key__[0].python_name
    count_query = query._replace(
        projection=(getattr(model, primary_key),),
        orderings=(),
        limit_=None,
        offset_=None,
    )
    return len(await session.fetch(count_query))
