# Binding, validation, and dependencies

Binding is where a raw HTTP request becomes the clean, typed arguments your
handler actually wants — and, fittingly for a framework named Wreath, it is the
place where the request is woven into your function. You declare what you expect
and where it comes from; Wreath compiles a validator for it when the application
starts, so validation costs almost nothing at request time. Anything that
doesn't fit becomes a structured `422` before your handler runs.

```python
from wreath import Request
from wreath.binding import Path, Query, Header, Cookie, Body, Form, File, Depends

@app.get("/items/{id}")
async def show(
    request: Request,
    id: int = Path(),
    fields: list[str] = Query(default=()),
    trace: str | None = Header(default=None),
) -> dict:
    return {"id": id, "fields": fields}
```

Each marker names exactly where the value is read from — the path, the query
string, a header, a cookie, the body, a form field, an uploaded file. There is
no cleverness to memorize: `Query` reads the query string, `Header` reads a
header. A body annotated as an [ORM model](orm.md) is validated against that
model's own columns, so the same definition guards your database and your API.

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
