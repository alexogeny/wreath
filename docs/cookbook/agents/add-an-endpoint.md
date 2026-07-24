# Add an endpoint or model

## An endpoint

The smallest correct endpoint is four small things, in order:

1. Add the route to a `Router` module (or the app), with **typed parameters** —
   so it validates its input and appears in the OpenAPI document for free.
2. Return a `dict` for JSON, or a `wreath.response` type when you need control.
3. Add a `TestClient` test that drives it through the real pipeline.
4. Add at least one **adversarial** test that fuzzes it — a malformed body, a
   truncated connection, or a failing dependency — through
   [`wreath.replay`](../../reference/replay.md), and assert an owned outcome (a
   `422`, not a `500`; a released connection, not a leak). See
   [Fuzz your own routes](../recipes/fuzz-your-routes.md). A new endpoint is not
   done until you know how it fails.

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
