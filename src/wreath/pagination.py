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

import datetime
import json
import uuid
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote_plus

from ._b64 import b64url_decode, b64url_encode
from ._native import _core
from .exceptions import BadRequest
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
MAX_CURSOR_COLUMNS = 8


class InvalidPagination(BadRequest, ValueError):
    """A client-supplied page, sort, filter, or cursor that is not accepted."""


def _rank_indices(
    scores: Sequence[float], *, page: int, size: int, descending: bool
) -> tuple[int, ...]:
    """Select a numeric page while the sort workspace remains native-owned."""
    return _core.rank_indices(scores, (page - 1) * size, size, descending)


__all__ = [
    "DEFAULT_SIZE",
    "MAX_PAGE",
    "MAX_CURSOR_COLUMNS",
    "MAX_SIZE",
    "InvalidPagination",
    "FilterField",
    "FilterSet",
    "Listing",
    "Page",
    "PageParams",
    "CursorPage",
    "CursorParams",
    "apply_filters",
    "apply_sort",
    "page_params",
    "cursor_params",
    "paginate",
    "paginate_cursor",
    "parse_sort",
    "sortable_fields",
]


_FILTER_OPERATORS = frozenset(
    {
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "in",
        "not_in",
        "like",
        "ilike",
        "is_null",
        "is_not_null",
    }
)


@dataclass(frozen=True, slots=True)
class FilterField:
    """One public filter name compiled to a model column and allowed operators."""

    column: str | None = None
    operators: tuple[str, ...] = ("eq",)

    def __post_init__(self) -> None:
        if self.column is not None and (not isinstance(self.column, str) or not self.column):
            raise ValueError("FilterField column must be a non-empty string or None")
        if not self.operators:
            raise ValueError("FilterField operators must not be empty")
        unknown = set(self.operators) - _FILTER_OPERATORS
        if unknown:
            raise ValueError(
                f"FilterField operators {sorted(unknown)!r} are unsupported; "
                f"use one of {sorted(_FILTER_OPERATORS)!r}"
            )
        if len(set(self.operators)) != len(self.operators):
            raise ValueError("FilterField operators must not contain duplicates")


class FilterSet:
    """A bounded declarative map from public filter names to ORM predicates.

    Scalar values mean equality. Rich operators use a nested mapping, for
    example `{"created": {"gte": start, "lt": end}, "state": {"in": states}}`.
    No lookup suffix or relationship path is interpreted.
    """

    __slots__ = ("_fields", "max_terms", "model")

    def __init__(
        self,
        model: type,
        fields: Mapping[str, FilterField | Iterable[str]],
        *,
        max_terms: int = 16,
    ) -> None:
        columns = getattr(model, "__wreath_column_map__", None)
        if not isinstance(columns, dict):
            raise TypeError(f"FilterSet model must be a mapped Wreath model, got {model!r}")
        if isinstance(max_terms, bool) or not isinstance(max_terms, int) or max_terms < 1:
            raise ValueError("FilterSet max_terms must be a positive integer")
        typed_columns = cast("dict[str, Any]", columns)
        column_names = frozenset(typed_columns)
        compiled: dict[str, tuple[Any, frozenset[str]]] = {}
        for public_name, declaration in fields.items():
            if not isinstance(public_name, str) or not public_name:
                raise ValueError("FilterSet public names must be non-empty strings")
            if isinstance(declaration, str):
                raise TypeError(
                    f"FilterSet field {public_name!r} operators must be an iterable "
                    f"such as ({declaration!r},), not a bare string"
                )
            field = declaration if isinstance(declaration, FilterField) else FilterField(
                operators=tuple(declaration)
            )
            compiled[public_name] = _compile_filter_field(
                model, typed_columns, column_names, public_name, field
            )
        self.model = model
        self.max_terms = max_terms
        self._fields = compiled

    def apply(self, query: Select, values: Mapping[str, Any]) -> Select:
        """Apply validated values to `query` in one immutable `where` copy."""
        if query.model is not self.model:
            raise TypeError(
                f"FilterSet for {self.model.__name__} cannot apply to {query.model.__name__}"
            )
        predicates = []
        remaining = self.max_terms
        for public_name, supplied in values.items():
            declaration = self._fields.get(public_name)
            if declaration is None:
                raise InvalidPagination(
                    f"filter {public_name!r} is not allowed; use one of "
                    f"{', '.join(self._fields) or 'none'}"
                )
            column, allowed = declaration
            operations = supplied if isinstance(supplied, Mapping) else {"eq": supplied}
            for operator, operand in operations.items():
                remaining -= 1
                if remaining < 0:
                    raise InvalidPagination(
                        f"filters contain more than max_terms={self.max_terms} predicates"
                    )
                if operator not in allowed:
                    raise InvalidPagination(
                        f"filter {public_name!r} does not allow operator {operator!r}; "
                        f"use one of {', '.join(sorted(allowed))}"
                    )
                predicates.append(_filter_predicate(column, operator, operand, public_name))
        return query.where(*predicates) if predicates else query


class Listing:
    """One reusable declaration for filtering and sorting a model listing."""

    __slots__ = ("_sort", "default_sort", "filters", "model")

    def __init__(
        self,
        filters: FilterSet,
        *,
        sort: Iterable[str] = (),
        default_sort: Iterable[str] = (),
    ) -> None:
        if not isinstance(filters, FilterSet):
            raise TypeError(f"Listing filters must be a FilterSet, got {filters!r}")
        allowed_sort = frozenset(sort)
        model = filters.model
        columns = frozenset(_as_model(model).__wreath_column_map__)
        for name in allowed_sort:
            if name not in columns:
                raise ValueError(
                    f"Listing sort field {name!r} is not a column of {model.__name__}"
                )
        defaults = tuple(default_sort)
        allowed_names = ", ".join(sorted(allowed_sort)) or "none"
        for token in defaults:
            name = token[1:] if token.startswith("-") else token
            if name not in allowed_sort:
                raise ValueError(
                    f"Listing default sort {token!r} is not allowed; use one of "
                    f"{allowed_names}"
                )
        self.filters = filters
        self.model = model
        self._sort = allowed_sort
        self.default_sort = defaults

    def apply(
        self,
        query: Select,
        *,
        filters: Mapping[str, Any] | None = None,
        sort: Iterable[str] | None = None,
    ) -> Select:
        """Apply both declarations without issuing a database query."""
        shaped = self.filters.apply(query, filters or {})
        order = self.default_sort if sort is None else tuple(sort)
        return apply_sort(shaped, order, allow=self._sort)


def _filter_predicate(column: Any, operator: str, operand: Any, name: str) -> Any:
    if operator == "eq":
        return column == operand
    if operator == "ne":
        return column != operand
    if operator == "lt":
        return column < operand
    if operator == "lte":
        return column <= operand
    if operator == "gt":
        return column > operand
    if operator == "gte":
        return column >= operand
    if operator == "in":
        return column.in_(operand)
    if operator == "not_in":
        return column.not_in(operand)
    if operator == "like":
        return column.like(operand)
    if operator == "ilike":
        return column.ilike(operand)
    if operand is not True:
        raise InvalidPagination(
            f"filter {name!r} operator {operator!r} takes the literal true"
        )
    if operator == "is_null":
        return column.is_null()
    return column.is_not_null()


def _compile_filter_field(
    model: type,
    columns: Mapping[str, Any],
    column_names: frozenset[str],
    public_name: str,
    field: FilterField,
) -> tuple[Any, frozenset[str]]:
    """Compile one declaration outside `FilterSet.__init__`'s field loop."""
    column_name = field.column or public_name
    if column_name not in column_names:
        raise ValueError(
            f"FilterSet field {public_name!r} names {column_name!r}, but "
            f"{model.__name__} declares {', '.join(columns) or 'no columns'}"
        )
    column = columns[column_name]
    operators = frozenset(field.operators)
    if not operators.isdisjoint({"like", "ilike"}) and column.pg_type.name not in {
        "text",
        "varchar",
    }:
        raise ValueError(
            f"FilterSet field {public_name!r} allows a text pattern operator, "
            f"but {model.__name__}.{column_name} is {column.pg_type.name}; "
            "use a Text or Varchar column"
        )
    return getattr(model, column_name), operators


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
        pages = 0 if self.size <= 0 else (self.total + self.size - 1) // self.size
        return {
            "items": list(self.items),
            "total": self.total,
            "page": self.page,
            "size": self.size,
            "pages": pages,
            "has_next": self.page < pages,
            "has_prev": self.page > 1,
        }


@dataclass(frozen=True, slots=True)
class PageParams:
    """Normalized paging + sort request. `sort` items may be `-field` for DESC."""

    page: int = 1
    size: int = DEFAULT_SIZE
    sort: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CursorParams:
    """A bounded keyset request: an opaque position, size, and stable order."""

    after: str | None = None
    size: int = DEFAULT_SIZE
    sort: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CursorPage[T]:
    """One keyset page and the opaque position for the next page."""

    items: Sequence[T]
    size: int
    next: str | None

    @property
    def has_next(self) -> bool:
        return self.next is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": list(self.items),
            "size": self.size,
            "next": self.next,
            "has_next": self.has_next,
        }


def parse_sort(raw: str) -> tuple[str, ...]:
    """`"name,-created_at"` -> `("name", "-created_at")`; blank -> `()`."""
    return tuple(token.strip() for token in raw.split(",") if token.strip())


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
    return _page_params(request, default_size=DEFAULT_SIZE)


def cursor_params(request: Any) -> CursorParams:
    """Bind `?after=&size=&sort=` for keyset pagination.

    The existing native query kernel owns size and sort parsing. Only the
    opaque `after` value is selected here; cursor decoding happens once in
    `paginate_cursor`, where the expected columns are known.
    """
    parsed = _page_params(request, default_size=DEFAULT_SIZE)
    after = None
    for field in request.query_string.split(b"&"):
        name, separator, value = field.partition(b"=")
        if separator and name == b"after":
            if len(value) > 4096:
                raise InvalidPagination("pagination cursor is longer than 4096 bytes")
            try:
                after = unquote_plus(value.decode("ascii")) or None
            except UnicodeError as error:
                raise InvalidPagination("pagination cursor must be ASCII base64url") from error
            break
    return CursorParams(after=after, size=parsed.size, sort=parsed.sort)


def _page_params(
    request: Any,
    *,
    default_size: int,
    max_page: int = MAX_PAGE,
    max_size: int = MAX_SIZE,
) -> PageParams:
    """The shared bounded query parser, with a caller-selected default size."""
    page, size, sort = _core.page_params(request.query_string, default_size, max_page, max_size)
    return PageParams(page=page, size=size, sort=sort)


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
        allowed_names = ", ".join(sorted(allowed)) or "(none)"
        raise InvalidPagination(
            f"cannot {what} by {name!r}; not in the allow-list. Use one of: {allowed_names}"
        )
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


def _cursor_value(value: Any) -> list[Any]:
    if value is None or type(value) in (str, int, float, bool):
        return ["scalar", value]
    if isinstance(value, uuid.UUID):
        return ["uuid", str(value)]
    if isinstance(value, datetime.datetime):
        return ["datetime", value.isoformat()]
    if isinstance(value, datetime.date):
        return ["date", value.isoformat()]
    raise TypeError(
        f"cursor column value must be a JSON scalar, UUID, date, or datetime, "
        f"not {type(value).__name__}"
    )


def _restore_cursor_value(value: Any) -> Any:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("cursor value has the wrong shape")
    kind, raw = value
    if kind == "scalar" and (raw is None or type(raw) in (str, int, float, bool)):
        return raw
    if kind == "uuid" and isinstance(raw, str):
        return uuid.UUID(raw)
    if kind == "datetime" and isinstance(raw, str):
        return datetime.datetime.fromisoformat(raw)
    if kind == "date" and isinstance(raw, str):
        return datetime.date.fromisoformat(raw)
    raise ValueError(f"cursor value kind is not supported: {kind!r}")


def _encode_cursor(order: tuple[str, ...], values: tuple[Any, ...]) -> str:
    payload = json.dumps(
        [list(order), [_cursor_value(value) for value in values]],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return b64url_encode(payload)


def _decode_cursor(token: str, order: tuple[str, ...]) -> tuple[Any, ...]:
    try:
        payload = json.loads(b64url_decode(token))
        encoded_order, encoded_values = payload
        if encoded_order != list(order) or len(encoded_values) != len(order):
            raise ValueError("cursor was issued for a different ordering")
        return tuple(_restore_cursor_value(value) for value in encoded_values)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidPagination(
            "invalid pagination cursor; use the unmodified next value from the previous page"
        ) from error


def _item_value(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        try:
            return item[name]
        except KeyError:
            pass
    else:
        try:
            return getattr(item, name)
        except AttributeError:
            pass
    raise TypeError(
        f"cursor field {name!r} is absent from returned {type(item).__name__}; "
        "include every cursor field in the query projection"
    )


async def paginate_cursor(
    session: Any,
    query: Select,
    params: CursorParams,
    *,
    allow_sort: Iterable[str] | None = None,
) -> CursorPage[Any]:
    """Fetch a stable keyset page without `OFFSET` or a count query.

    Caller-selected sorts are followed by every primary-key column as a stable
    tie-breaker. The cursor carries all order values and is bound to that exact
    order, preventing a token from one listing being silently reused for
    another.
    """
    model = _as_model(query.model)
    allowed = frozenset(allow_sort) if allow_sort is not None else frozenset(sortable_fields(model))
    requested = params.sort or tuple(column.python_name for column in model.__wreath_primary_key__)
    order = list(requested)
    descending = order[0].startswith("-") if order else False
    primary = tuple(column.python_name for column in model.__wreath_primary_key__)
    present = {token.removeprefix("-") for token in order}
    order.extend(("-" if descending else "") + name for name in primary if name not in present)
    normalized = tuple(order)
    if len(normalized) > MAX_CURSOR_COLUMNS:
        raise InvalidPagination(
            f"cursor pagination supports at most {MAX_CURSOR_COLUMNS} order columns, "
            f"got {len(normalized)}; use fewer sort fields"
        )

    columns: list[tuple[str, Any, bool]] = []
    cursor_allowed = allowed | frozenset(primary)
    for token in normalized:
        desc = token.startswith("-")
        name = token.removeprefix("-")
        columns.append((name, _column(model, name, cursor_allowed, "sort"), desc))
    query = query.order_by(
        *(column.desc() if desc else column.asc() for _, column, desc in columns)
    )

    if params.after is not None:
        values = _decode_cursor(params.after, normalized)
        predicate = None
        equal = None
        for (_, column, desc), value in zip(columns, values, strict=True):
            comparison = column < value if desc else column > value
            branch = comparison if equal is None else equal & comparison
            predicate = branch if predicate is None else predicate | branch
            equality = column == value
            equal = equality if equal is None else equal & equality
        if predicate is not None:
            query = query.where(predicate)

    fetched = list(await session.fetch(query.limit(params.size + 1)))
    more = len(fetched) > params.size
    items = fetched[: params.size]
    next_cursor = None
    if more and items:
        last = items[-1]
        next_cursor = _encode_cursor(
            normalized, tuple(_item_value(last, name) for name, _, _ in columns)
        )
    return CursorPage(items=items, size=params.size, next=next_cursor)


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
