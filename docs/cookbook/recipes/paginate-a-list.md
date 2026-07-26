# Paginate, sort, and filter a list endpoint

Every list endpoint wants the same three things — a page, a sort, a filter — and
every framework reinvents them slightly differently. Wreath gives you one
dependency to parse the query and one call to run the page, with sorting locked to
columns you trust:

```python
from typing import Annotated
from wreath.binding import Depends
from wreath.orm import FromORM, Session
from wreath.pagination import PageParams, page_params, paginate

@app.get("/orders")
async def list_orders(
    request,
    session: Annotated[Session, FromORM("main", workload="read")],
    params: Annotated[PageParams, Depends(page_params)],
) -> dict:
    query = Order.select().where(Order.status == "paid")
    page = await paginate(
        session, query, params,
        allow_sort=("created_at", "total"),
    )
    return page.as_dict()
```

`page_params` binds `?page=&size=&sort=` into a `PageParams` (with `size` capped,
so a client can't ask for a million rows); `paginate` fetches one page plus the
total and returns a `Page` with `items`, `total`, and the derived
`pages`/`has_next`, and `as_dict()` is the JSON your data table wants. `allow_sort`
is a hard allow-list — `?sort=secret_column` is rejected, never handed to the SQL.
Sort tokens are `field` / `-field` for ascending/descending. For filtering the
same safe way, fold trusted columns in with `apply_filters(query, {...},
allow=(...))`; and pass an explicit `total=` when you already know the count or
want to skip it on a hot path.
