# Add an endpoint or model

## An endpoint

The smallest correct endpoint is three small things, in order:

1. Add the route to a `Router` module (or the app), with **typed parameters** —
   so it validates its input and appears in the OpenAPI document for free.
2. Return a `dict` for JSON, or a `wreath.response` type when you need control.
3. Add a `TestClient` test that drives it through the real pipeline.

```python
from wreath import Router, Request
router = Router()

@router.get("/items/{id}")
async def show(request: Request, id: int) -> dict:
    return {"id": id}
```

## A model

Add a `wreath.orm.Model` whose columns double as the validator for incoming
bodies. Keep persistence and mapping in `wreath.orm`; keep raw connection and
pool work in `wreath.postgres`. The line between them is not a suggestion —
`wreath.postgres` must not import anything from `wreath.orm`.

When either is in place, walk the [gates](checks.md) and then
[verify the behaviour](verify-a-change.md).
