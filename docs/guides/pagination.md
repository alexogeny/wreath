# Pagination, filtering, and sorting

List endpoints all want the same three things — a page, a sort, a filter — and all invent them slightly differently. Wreath gives you one dependency and one helper.

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

The total defaults to a `COUNT` over the filtered query; pass an explicit `total=` if you already know it (or want to skip the count on a hot path).
