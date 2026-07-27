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
[below](#why-not-one-dependency) for why this is not one `Depends`.

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

### Why not one dependency

`wreath.pagination` exports `page_params`, and its docstring calls it a
`Depends`-able. **It does not work as one today**, in either spelling, so this
guide binds the three parameters directly instead:

```python no-check="shows the two spellings that do not work; both are defects the surrounding prose explains"
params: Annotated[PageParams, Depends(page_params)]   # 400 "invalid JSON body"
params: PageParams = Depends(page_params)             # 500
```

A dependency is always called as `fn(request)`, and its own scalar parameters are
never bound from the query string — so `page_params` receives the request where
it expects a page number. In the `Annotated` spelling the parameter is instead
classified as a **request body**, which is why a `GET` answers `400 invalid JSON
body`: the framework reports a caller error for what is actually a wiring bug,
and the caller has no way to tell.

The direct form above is not a lesser version of the dependency — it is the same
three bounds, applied by the binding layer rather than inside a function the
binding layer never fills in. When `page_params` binds correctly this page will
show it, and the bounds will not have to change.

## Safe by construction

Sorting and filtering are **allow-list only** — a column name that isn't in the allow-list (or, by default, isn't a real column of the model) raises rather than reaching the SQL. There is no path from a query string to an arbitrary column or operator:

```python
from wreath.pagination import apply_sort, apply_filters

query = apply_sort(query, params.sort, allow=("name", "created_at"))
query = apply_filters(query, {"ranch_id": ranch_id}, allow=("ranch_id",))
```

Both helpers fold every token into a **single** query-builder call, so applying a request full of `?sort=` fields costs time linear in their number, not quadratic — a client cannot turn a long sort string into disproportionate server work. (Pinned by the `pagination-apply-sort` complexity probe.)

The total defaults to counting the rows that match the filtered query; pass an explicit `total=` if you already know it (or want to skip the count on a hot path).
