# OpenAPI and typed clients

Because your routes already declare their types, Wreath can describe your API to
the rest of the world without you writing that description by hand — and keep it
honest, because it is generated from the same signatures your handlers run on.

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

**Reference:** [`wreath.openapi`](../reference/openapi.md),
[`wreath.typegen`](../reference/typegen.md).
