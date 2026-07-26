# Binding, validation, and dependencies

Binding is where a raw HTTP request becomes the clean, typed arguments your
handler actually wants — and, fittingly for a framework named Wreath, it is the
place where the request is woven into your function. You declare what you expect
and where it comes from; Wreath compiles a validator for it when the application
starts, so validation costs almost nothing at request time. Anything that
doesn't fit becomes a structured `422` before your handler runs.

```python
from typing import Annotated

from wreath import Request
from wreath.binding import Path, Query, Header, Cookie, Body, Form, File, Depends

@app.get("/items/{id}")
async def show(
    request: Request,
    id: int,
    limit: Annotated[int, Query(minimum=1, maximum=100)] = 20,
    trace: Annotated[str | None, Header(alias="x-trace-id")] = None,
) -> dict:
    return {"id": id, "limit": limit, "trace": trace}
```

Each marker names exactly where the value is read from — the path, the query
string, a header, a cookie, the body, a form field, an uploaded file. There is
no cleverness to memorize: `Query` reads the query string, `Header` reads a
header. Markers ride inside `Annotated`, and a default stays an ordinary Python
default on the parameter — the signature never stops being plain Python. Most
of the time you need no marker at all: a name matching a path placeholder is a
path parameter, a parameter annotated with a dataclass (or an ORM model) is the
JSON body, and remaining scalar parameters read from the query string. A body
validated against an [ORM model](orm.md) is checked by that model's own
columns, so the same definition guards your database and your API.

`Query` also carries numeric bounds — `minimum`, `maximum`, and an `overflow`
of `"error"` (a structured `422`) or `"clamp"` (pin to the nearest bound, the
right answer for pagination). Query, header, and cookie values are scalars:
`str`, `int`, `float`, `bool`, or optional unions of them. Anything more
structured belongs in the body.

## User story: a search endpoint with safe bounds

> *As an API author, my `/search` endpoint takes a required `q`, an optional
> `limit` I never want above 100, and an optional category filter. I want bad
> input rejected with a clear `422` before my code runs — and I don't want to
> write the parsing or the bounds check by hand.*

```python
from typing import Annotated
from wreath.binding import Query

@app.get("/search")
async def search(
    request,
    q: str,
    limit: Annotated[int, Query(minimum=1, maximum=100, overflow="clamp")] = 20,
    category: str | None = None,
) -> dict:
    return await run_search(q, limit=limit, category=category)
```

`q` has no default, so its absence is a `422`; `limit` is pinned into `[1, 100]`
rather than trusted; `category` is an optional query scalar. Your handler only
ever sees clean, typed values — the raw query string never makes it past the
door.

## Dependencies

Handlers often need the same prepared value — the current user, a database
handle, a parsed pagination window. `Depends` resolves such a value once per
request and hands it to every function that asks for it:

```python
async def current_user(request: Request):
    ...

@app.get("/me")
async def me(request: Request, user = Depends(current_user)) -> dict:
    return {"user": user.id}
```

Binding, validation, and dependency resolution are deliberately one surface. We
did not split them into a container, an injector, and a resolver you would have
to wire together — that is complexity for its own sake. It is all `wreath.binding`,
and it all runs on the same compiled path.

**Reference:** [`wreath.binding`](../reference/binding.md).
