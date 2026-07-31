"""Pagination, sorting, and filtering for the ORM query builder.

A thin, safe layer over `Select`: turn `?page=&size=&sort=` query parameters
into `LIMIT`/`OFFSET`/`ORDER BY` against an **allow-list** of columns (never
an arbitrary column name from the request), and return a `Page` with the total.

Consumer-wired, no `app.py` changes needed:

```python
from wreath.pagination import Page, PageParams, page_params, paginate
from wreath.binding import Depends

@app.get("/llamas")
async def list_llamas(request, params: PageParams = Depends(page_params)):
    page = await paginate(session, Llama.select(), params,
                          allow_sort=("name", "created_at"))
    return page.as_dict()
```

"""

from __future__ import annotations

from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ._codecs import parse_qs
from .orm.query import Select

if TYPE_CHECKING:
    from .orm.model import Model


def _as_model(model: type) -> type[Model]:
    """Narrow a `Select.model` (typed `type`) to a wreath model for the type
    checker, so the ORM-injected `__wreath_*__` class attributes resolve."""
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
        """How many pages of this size `total` rows fill, rounding up.

        `0` for a page size of zero, rather than a division error: a page
        object is often built to be rendered, and a template asking for the page
        count should not be where an invalid size surfaces.
        """
        if self.size <= 0:
            return 0
        return (self.total + self.size - 1) // self.size

    @property
    def has_next(self) -> bool:
        """Whether a page after this one exists, according to `total`.

        Derived from the count, not from having looked -- so it inherits the
        count's staleness. See `paginate` on the two snapshots.
        """
        return self.page < self.pages

    @property
    def has_prev(self) -> bool:
        """Whether this is past the first page. Pages are numbered from 1."""
        return self.page > 1

    def as_dict(self) -> dict[str, Any]:
        """This page as a JSON-ready dict, derived fields included.

        The shape a list endpoint returns: `items` plus `total`, `page`,
        `size`, `pages`, `has_next` and `has_prev`, so a client renders
        controls without recomputing any of them. `items` are passed through
        as they are -- serialising them is the response encoder's job.
        """
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
    """Normalized paging + sort request. `sort` items may be `-field` for DESC."""

    page: int = 1
    size: int = DEFAULT_SIZE
    sort: tuple[str, ...] = ()


def parse_sort(raw: str) -> tuple[str, ...]:
    """`"name,-created_at"` -> `("name", "-created_at")`; blank -> `()`."""
    return tuple(token.strip() for token in raw.split(",") if token.strip())


def _bounded(raw: str | None, default: int, ceiling: int) -> int:
    """One query value as an int in `[1, ceiling]`, falling back to `default`.

    Clamps rather than refusing. A caller asking for page 20,000 is not making a
    protocol error worth a 422 -- it is asking for a page past the end, and the
    honest answer is the last page wreath is willing to walk to. `MAX_PAGE`
    exists because `LIMIT/OFFSET` makes the database discard every row before
    the offset, which an anonymous caller could otherwise ask for repeatedly.
    """
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, min(value, ceiling))


def page_params(request: Any) -> PageParams:
    """Bind `?page=&size=&sort=` into `PageParams`. Written as a `Depends`.

    ```python
    from wreath.binding import Depends
    from wreath.pagination import PageParams, page_params, paginate

    @app.get("/llamas")
    async def list_llamas(request, params: PageParams = Depends(page_params)):
        page = await paginate(session, Llama.select(), params,
                              allow_sort=("name", "created_at"))
        return page.as_dict()
    ```

    It takes the request and reads the query string itself, because a
    dependency's own parameters are never bound from the request -- wreath calls
    a dependency as `fn(request, **nested_depends)` and nothing else. This
    signature used to be `page_params(page, size, sort)` carrying `Query()`
    markers, which meant the request object arrived *as* the page number and the
    first comparison against it was a 500. That shape is now refused at route
    compilation rather than failing per request.

    `page` and `size` are clamped into `[1, MAX_PAGE]` and `[1, MAX_SIZE]`;
    anything unparseable falls back to the default rather than raising, so a
    hand-edited URL degrades to the first page instead of a 422.

    Args:
        request: The request; only its query string is read.

    Returns:
        The normalized page, size, and sort tuple.
    """
    # `request.query_string`, not the scope: on the native server the scope is a
    # lazily materialized dict, so reading it here would build the whole thing
    # to reach one member. First value wins, matching the binding layer.
    values: dict[str, str] = {}
    for key, value in parse_qs(request.query_string):
        values.setdefault(key, value)
    return PageParams(
        page=_bounded(values.get("page"), 1, MAX_PAGE),
        size=_bounded(values.get("size"), DEFAULT_SIZE, MAX_SIZE),
        sort=parse_sort(values.get("sort") or ""),
    )


def sortable_fields(model: type) -> tuple[str, ...]:
    """Every column of `model` a caller may sort or filter by, in declared order.

    The default allow-list for `apply_sort` and `apply_filters`, and every column
    except the *retrieval* ones -- a `Vector` embedding and a `TsVector`.

    Those are excluded because `?sort=embedding` is the same request-triggered
    cost `MAX_PAGE` exists to bound, twenty lines above. pgvector gives `vector` a
    btree opclass, so `ORDER BY embedding` is valid SQL that runs: a full sort of
    the table on values that are kilobytes each, with no index that can serve it,
    from a query string an anonymous caller controls. Ordering by one is also
    meaningless -- a vector's btree order is a tie-break, not a ranking, and
    ranking by similarity is what `Vector.cosine_distance` and `TsVector.rank`
    are for.

    This is the default, not a prohibition: `apply_sort(..., allow=("embedding",))`
    still orders by it, for a caller who meant it.
    """
    from .orm.types import _is_retrieval_type

    return tuple(
        name
        for name, item in _as_model(model).__wreath_column_map__.items()
        if not _is_retrieval_type(item.pg_type)
    )


def _column(model: type, name: str, allowed: frozenset[str], what: str) -> Any:
    if name not in allowed:
        raise ValueError(f"cannot {what} by {name!r}; not in the allow-list")
    return getattr(model, name)


def apply_sort(query: Select, sort: Iterable[str], *, allow: Iterable[str] | None = None) -> Select:
    """Apply `sort` tokens (`field` / `-field`) against an allow-list.

    All tokens are folded into a *single* `order_by` call. `Select` is
    immutable, so a per-token `order_by` would recopy a growing tuple on every
    step -- O(k^2) in the request-controlled token count. Building the orderings
    once and applying them together keeps this O(k).

    The default allow-list is `sortable_fields(model)`, which is every column
    except the retrieval ones -- see there for why sorting by an embedding is a
    scan a caller can ask for. Passing `allow=` explicitly overrides that in
    both directions.
    """
    model = _as_model(query.model)
    allowed = frozenset(allow) if allow is not None else frozenset(sortable_fields(model))
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
    """Apply equality filters `{field: value}` against an allow-list.

    Values are handed to the ORM, which coerces/validates them against the
    column's type -- an out-of-range or wrong-typed value fails at bind time.
    Richer operators (ranges, `in`, `ilike`) are a deliberate follow-up.

    The allow-list defaults to `sortable_fields(model)`, so a retrieval column is
    not filterable by default either: an equality test against a `tsvector` or a
    1536-dimension embedding is a sequential scan that matches nothing.
    """
    model = _as_model(query.model)
    allowed = frozenset(allow) if allow is not None else frozenset(sortable_fields(model))
    predicates = []
    for name, value in filters.items():
        column = _column(model, name, allowed, "filter")
        predicates.append(column == value)
    # One combined `where` call: repeated per-filter calls would recopy the
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
    """Fetch one page of `query` and its total.

    Pass `total` (an int or awaitable) when you already have an efficient count;
    otherwise a correctness-first fallback counts primary keys (see `_count`).

    **The count and the page are two statements.** Outside an explicit
    transaction they see two snapshots, so a concurrent insert or delete can make
    `total` disagree with `items` -- the page is right and the total is a moment
    older, or the reverse. That is usually what a pager wants (a stale total
    draws a stale last-page number and nothing more); wrap the call in
    `async with session.begin():` when it is not.
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
    """Total rows matching `query` (ignoring paging/order).

    Uses the session's efficient `SELECT COUNT(*)` (`Session.count`) when it
    exposes one, so counting a large result set is a single aggregate round trip
    rather than transferring every matching row. Falls back to re-projecting to
    the primary key and counting client-side for a minimal session that only
    implements `fetch` (e.g. a lightweight test double).
    """
    counter = getattr(session, "count", None)
    if callable(counter):
        return await counter(query)
    # No `count`: re-project to the primary key and count client-side. That
    # transfers one row per match, which is why `Session` exposes `count` and
    # this is reached only by a minimal double -- documented above rather than
    # refused, because refusing it would break every such double.
    model = _as_model(query.model)
    primary_key = model.__wreath_primary_key__[0].python_name
    count_query = query._replace(
        projection=(getattr(model, primary_key),),
        orderings=(),
        limit_=None,
        offset_=None,
    )
    return len(await session.fetch(count_query))
