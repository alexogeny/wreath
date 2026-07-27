# Paginate, sort, and filter a list endpoint

Every list endpoint wants the same three things — a page, a sort, a filter — and
every framework reinvents them slightly differently. Wreath gives you three bound
parameters and one call to run the page, with sorting locked to columns you trust:

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

The bounds come from `wreath.pagination`, so the binding layer rejects
`?page=0` and `?size=100000` with a `422` before a query is built, and they
cannot drift from what `paginate` enforces. `wreath.pagination` also exports
`page_params`, which is meant to collapse those three into one `Depends` —
[it does not work today](../../guides/pagination.md#why-not-one-dependency), so
this recipe binds them directly.

`paginate` fetches one page plus the total and returns a
`Page` with `items`, `total`, and the derived
`pages`/`has_next`, and `as_dict()` is the JSON your data table wants. `allow_sort`
is a hard allow-list — `?sort=secret_column` is rejected, never handed to the SQL.
Sort tokens are `field` / `-field` for ascending/descending. For filtering the
same safe way, fold trusted columns in with `apply_filters(query, {...},
allow=(...))`; and pass an explicit `total=` when you already know the count or
want to skip it on a hot path.
