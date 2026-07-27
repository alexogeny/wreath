# OpenAPI and typed clients

Because your routes already declare their types, Wreath can describe your API to
the rest of the world without you writing that description by hand — and keep it
honest, because it is generated from the same signatures your handlers run on.

## User story: fail CI when the frontend client drifts

> *As an API author, my frontend uses a generated TypeScript client. When I change
> a route's response type, I want CI to fail unless the committed client was
> regenerated — so the frontend can never quietly fall out of sync with the API.*

```bash
# regenerate after changing a handler, and commit the result
wreath typegen app:app --output ./client --react-query

# in CI: exit non-zero if the checked-in client is stale
wreath typegen app:app --output ./client --react-query --check
```

`--check` regenerates into a scratch area and compares against what's committed;
a drift is a non-zero exit — the same ergonomics as `wreath migrations check`.
Because the client and the running API both derive from your typed handler
signatures, a green check *is* the guarantee that they agree.

## OpenAPI

`wreath.openapi` produces an OpenAPI 3.1 document from your typed routes, and a
page to serve it:

```python
from wreath.openapi import generate_openapi
spec = generate_openapi(app)
```

## Typed clients

`wreath.typegen` goes a step further and renders a client for your frontend —
TypeScript types, a `fetch` client, and React-Query hooks — from that same
canonical model. Your frontend's understanding of your API can't drift from the
API itself, because both come from one source:

```bash
wreath typegen app:app --output ./client --react-query
```

Two things beyond the routes come along, when your application has them. The
authorization vocabulary becomes typed permission flags, so asking about an
action your API does not enforce is a compile error rather than a permanent
`false`. And each [calculated view](calculated-views.md) your routes use becomes
a `series.ts` type whose fields are the measure names you declared — a component
destructures `line.values` with the compiler already knowing whether it can
contain `null`. Neither module ships for an application that declares neither.

**Reference:** [`wreath.openapi`](../reference/openapi.md),
[`wreath.typegen`](../reference/typegen.md).
