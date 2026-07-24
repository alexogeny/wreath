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
from typing import Annotated, Any, Generic, TypeVar

from .binding import Query
from .orm.query import Select

T = TypeVar("T")

DEFAULT_SIZE = 20
MAX_SIZE = 100

__all__ = [
    "DEFAULT_SIZE",
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
class Page(Generic[T]):
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
    page: Annotated[int, Query(minimum=1)] = 1,
    size: Annotated[int, Query(minimum=1, maximum=MAX_SIZE)] = DEFAULT_SIZE,
    sort: Annotated[str, Query()] = "",
) -> PageParams:
    """A ``Depends``-able that binds ``?page=&size=&sort=`` into ``PageParams``."""
    return PageParams(page=page, size=size, sort=parse_sort(sort))


def sortable_fields(model: type) -> tuple[str, ...]:
    """Every column name of ``model`` -- the default sort/filter allow-list."""
    return tuple(model.__wreath_column_map__)


def _column(model: type, name: str, allowed: frozenset[str], what: str) -> Any:
    if name not in allowed:
        raise ValueError(f"cannot {what} by {name!r}; not in the allow-list")
    return getattr(model, name)


def apply_sort(query: Select, sort: Iterable[str], *, allow: Iterable[str] | None = None) -> Select:
    """Apply ``sort`` tokens (``field`` / ``-field``) against an allow-list."""
    model = query.model
    allowed = frozenset(allow) if allow is not None else frozenset(model.__wreath_column_map__)
    for token in sort:
        descending = token[:1] == "-"
        name = token[1:] if descending else token
        column = _column(model, name, allowed, "sort")
        query = query.order_by(column.desc() if descending else column.asc())
    return query


def apply_filters(
    query: Select, filters: Mapping[str, Any], *, allow: Iterable[str] | None = None
) -> Select:
    """Apply equality filters ``{field: value}`` against an allow-list.

    Values are handed to the ORM, which coerces/validates them against the
    column's type -- an out-of-range or wrong-typed value fails at bind time.
    Richer operators (ranges, ``in``, ``ilike``) are a deliberate follow-up.
    """
    model = query.model
    allowed = frozenset(allow) if allow is not None else frozenset(model.__wreath_column_map__)
    for name, value in filters.items():
        column = _column(model, name, allowed, "filter")
        query = query.where(column == value)
    return query


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

    Correctness-first fallback: re-project to the primary key and count rows.
    TODO: an efficient ``SELECT COUNT(*)`` once the compiler grows an aggregate
    projection -- that is a compiler change, out of this module's scope.
    """
    model = query.model
    primary_key = model.__wreath_primary_key__[0].python_name
    count_query = query._replace(
        projection=(getattr(model, primary_key),),
        orderings=(),
        limit_=None,
        offset_=None,
    )
    return len(await session.fetch(count_query))
