# Content negotiation

JSON is the default and stays the default. But the same handler can serve a
different format when the client asks for one — a mobile app sending
`Accept: application/msgpack` to save bytes, a service-to-service call preferring
a binary encoding. The handler returns plain data; the format is chosen from the
`Accept` header.

## User story: serve MessagePack to the clients that want it

> *As an API author, my `/report` endpoint returns a large JSON payload. Some
> mobile clients would rather receive MessagePack to cut bandwidth — but I don't
> want two endpoints or a query-string flag. I want one handler that respects
> `Accept`.*

```python
from wreath.negotiation import serialize

@app.get("/report")
async def report(request):
    data = {"rows": rows, "generated_at": ts}
    return serialize(request, data)
```

- `Accept: application/json` (or none, or `*/*`) → JSON
- `Accept: application/msgpack` → MessagePack
- `Accept: application/json;q=0.2, application/msgpack;q=0.9` → MessagePack (higher q)
- `Accept: application/xml` → `406 Not Acceptable`, listing what *is* available

Every negotiated response carries `Vary: Accept`, so a shared cache keys on the
chosen format instead of serving one client's MessagePack to another expecting
JSON. q-values are parsed per RFC 9110 §12.5.1.

## Registering your own formats

`serialize` takes a `serializers` list — a `Serializer` is just a media type and
an encode function, so a custom format (CSV, a versioned JSON, anything) is a few
lines:

```python
from wreath.negotiation import JSON, Serializer, serialize

CSV = Serializer("text/csv", lambda rows: to_csv(rows).encode())

@app.get("/export")
async def export(request):
    return serialize(request, rows, serializers=(JSON, CSV))
```

The first serializer in the list is the default (used for a missing or `*/*`
`Accept`), so put your preferred format first. Just picking a format without
serializing? `negotiate(accept_header, serializers)` returns the chosen
`Serializer` (or `None`).
