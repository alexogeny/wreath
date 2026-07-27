# Pagination, filtering, and sorting

List endpoints all want the same three things — a page, a sort, a filter — and all invent them slightly differently. Wreath gives you one dependency and one helper.

## User story: a data table of orders

> *As an API author, my `/orders` list backs a frontend data table: it asks for a
> page, a size, and a sort column, and expects the rows plus a total so it can
> draw the pager. I want one dependency to parse the query and one call to run
> the page — with sorting locked to columns I trust.*

```python
from typing import Annotated
from wreath.binding import Query
from wreath.orm import FromORM, Session
from wreath.pagination import MAX_PAGE, MAX_SIZE, PageParams, paginate, parse_sort

@app.get("/orders")
async def list_orders(
    request,
    session: Annotated[Session, FromORM("main", workload="read")],
    page: Annotated[int, Query(minimum=1, maximum=MAX_PAGE)] = 1,
    size: Annotated[int, Query(minimum=1, maximum=MAX_SIZE)] = 20,
    sort: str = "",
) -> dict:
    params = PageParams(page=page, size=size, sort=parse_sort(sort))
    query = Order.select().where(Order.status == "paid")
    result = await paginate(
        session, query, params, allow_sort=("created_at", "total")
    )
    return result.as_dict()
```

`paginate` returns a `Page` with `items`, `total`, and the derived
`pages`/`has_next`, and `as_dict()` is the JSON your table wants. `allow_sort` is
a hard allow-list — `?sort=secret_column` is rejected, never handed to the SQL.

The three query parameters are bound **on the handler**, and the bounds come from
`wreath.pagination`, so they cannot drift from what `paginate` enforces. See
[below](#or-one-dependency) for the one-`Depends` form and how it differs.

`page` is bounded above by `MAX_PAGE` (10 000) as well as below by 1.
`LIMIT/OFFSET` makes the database walk and discard every row before the offset,
so an unbounded page number is a full scan a caller can ask for at will; past
that depth, filter on a keyset instead.


## Bind the query parameters

Declare `page`, `size` and `sort` on the handler and fold them into a
`PageParams`:

```python
from typing import Annotated
from wreath.binding import Query
from wreath.orm import FromORM, Session
from wreath.pagination import MAX_PAGE, MAX_SIZE, PageParams, paginate, parse_sort

@app.get("/llamas")
async def list_llamas(
    request,
    session: Annotated[Session, FromORM("main", workload="read")],
    page: Annotated[int, Query(minimum=1, maximum=MAX_PAGE)] = 1,
    size: Annotated[int, Query(minimum=1, maximum=MAX_SIZE)] = 20,
    sort: str = "",
):
    params = PageParams(page=page, size=size, sort=parse_sort(sort))
    query = Llama.select().where(Llama.active == True)
    result = await paginate(
        session, query, params, allow_sort=("name", "created_at")
    )
    return result.as_dict()
```

`paginate` fetches one page plus the total and returns a `Page[T]` (`items`, `total`, `page`, `size`, and the derived `pages`/`has_next`). `sort` tokens are `field` / `-field`; the `size` is capped, so a client can't ask for a million rows.

Out-of-range values never reach your code: `?page=0` and `?size=100000` are both
a `422` from the binding layer, before a query is built.

### Or one dependency

The three parameters above are the explicit form, and it is worth knowing
because it is what you reach for the moment the bounds differ per route.
`wreath.pagination` also exports `page_params`, which does the same job in one
parameter:

```python
from wreath.binding import Depends
from wreath.pagination import PageParams, page_params, paginate

@app.get("/llamas")
async def list_llamas(
    request,
    session: Annotated[Session, FromORM("main", workload="read")],
    params: PageParams = Depends(page_params),
):
    result = await paginate(
        session, Llama.select(), params, allow_sort=("name", "created_at")
    )
    return result.as_dict()
```

The two differ in how they treat a value out of range, and the difference is
deliberate. The bound form **refuses**: `?page=0` is a `422` from the binding
layer before a query is built. `page_params` **clamps**: `?page=999999` becomes
`MAX_PAGE`, and `?page=abc` becomes page 1. Neither is more correct in general —
a hand-edited URL degrading to a page that exists is friendlier for a browsable
list, and a strict `422` is better for an API whose clients you control.

`page_params` takes the request and reads the query string itself, rather than
declaring `page`, `size` and `sort` as its own parameters. That is not a
stylistic choice: **a dependency's own parameters are never bound from the
request.** Wreath calls a dependency as `fn(request, **nested_depends)`, so a
`Query()` marker on one of its parameters binds nothing — and route compilation
now refuses that declaration rather than letting it fail per request.

## Safe by construction

Sorting and filtering are **allow-list only** — a column name that isn't in the allow-list (or, by default, isn't a real column of the model) raises rather than reaching the SQL. There is no path from a query string to an arbitrary column or operator:

```python
from wreath.pagination import apply_sort, apply_filters

query = apply_sort(query, params.sort, allow=("name", "created_at"))
query = apply_filters(query, {"ranch_id": ranch_id}, allow=("ranch_id",))
```

Both helpers fold every token into a **single** query-builder call, so applying a request full of `?sort=` fields costs time linear in their number, not quadratic — a client cannot turn a long sort string into disproportionate server work. (Pinned by the `pagination-apply-sort` complexity probe.)

The total defaults to counting the rows that match the filtered query; pass an explicit `total=` if you already know it (or want to skip the count on a hot path).
