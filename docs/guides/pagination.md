# Pagination, filtering, and sorting

List endpoints all want the same three things — a page, a sort, a filter — and all invent them slightly differently. Wreath gives you one dependency and one helper.

## User story: a data table of orders

> *As an API author, my `/orders` list backs a frontend data table: it asks for a
> page, a size, and a sort column, and expects the rows plus a total so it can
> draw the pager. I want one dependency to parse the query and one call to run
> the page — with sorting locked to columns I trust.*

```python
from typing import Annotated
from wreath.binding import Depends
from wreath.pagination import PageParams, page_params, paginate

@app.get("/orders")
async def list_orders(
    request, params: Annotated[PageParams, Depends(page_params)]
) -> dict:
    query = Order.select().where(Order.status == "paid")
    page = await paginate(
        request.app.state.session, query, params,
        allow_sort=("created_at", "total"),
    )
    return page.as_dict()
```

`page_params` binds `?page=&size=&sort=`; `paginate` returns a `Page` with
`items`, `total`, and the derived `pages`/`has_next`, and `as_dict()` is the JSON
your table wants. `allow_sort` is a hard allow-list — `?sort=secret_column` is
rejected, never handed to the SQL.

`page` is bounded above by `MAX_PAGE` (10 000) as well as below by 1.
`LIMIT/OFFSET` makes the database walk and discard every row before the offset,
so an unbounded page number is a full scan a caller can ask for at will; past
that depth, filter on a keyset instead.


## Bind the query parameters

`page_params` is a `Depends`-able that binds `?page=&size=&sort=` into a `PageParams`:

```python
from typing import Annotated
from wreath.binding import Depends
from wreath.pagination import page_params, paginate

@app.get("/llamas")
async def list_llamas(request, params: Annotated[PageParams, Depends(page_params)]):
    query = Llama.select().where(Llama.active == True)
    page = await paginate(request.app.state.session, query, params,
                          allow_sort=("name", "created_at"))
    return page.as_dict()
```

`paginate` fetches one page plus the total and returns a `Page[T]` (`items`, `total`, `page`, `size`, and the derived `pages`/`has_next`). `sort` tokens are `field` / `-field`; the `size` is capped, so a client can't ask for a million rows.

## Safe by construction

Sorting and filtering are **allow-list only** — a column name that isn't in the allow-list (or, by default, isn't a real column of the model) raises rather than reaching the SQL. There is no path from a query string to an arbitrary column or operator:

```python
from wreath.pagination import apply_sort, apply_filters

query = apply_sort(query, params.sort, allow=("name", "created_at"))
query = apply_filters(query, {"ranch_id": ranch_id}, allow=("ranch_id",))
```

Both helpers fold every token into a **single** query-builder call, so applying a request full of `?sort=` fields costs time linear in their number, not quadratic — a client cannot turn a long sort string into disproportionate server work. (Pinned by the `pagination-apply-sort` complexity probe.)

The total defaults to counting the rows that match the filtered query; pass an explicit `total=` if you already know it (or want to skip the count on a hot path).
