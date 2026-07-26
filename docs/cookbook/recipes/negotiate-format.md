# Serve JSON or MessagePack from one handler

JSON is the default and stays the default. But the same handler can serve a
different format when the client asks — a mobile app sending
`Accept: application/msgpack` to save bytes, a service-to-service call preferring
a binary encoding. Return plain data through `serialize`, and the format is
chosen from the `Accept` header:

```python
from wreath.negotiation import serialize

@app.get("/report")
async def report(request):
    data = {"rows": rows, "generated_at": ts}
    return serialize(request, data)
```

- `Accept: application/json` (or none, or `*/*`) → JSON
- `Accept: application/msgpack` → MessagePack
- `Accept: application/json;q=0.2, application/msgpack;q=0.9` → MessagePack
- `Accept: application/xml` → `406 Not Acceptable`, listing what *is* available

Every negotiated response carries `Vary: Accept`, so a shared cache keys on the
chosen format instead of handing one client's MessagePack to another expecting
JSON. q-values are parsed per RFC 9110 §12.5.1.

## Your own formats

`serialize` takes a `serializers` list — a `Serializer` is just a media type and
an encode function, so a custom format is a few lines. The first serializer is
the default (used for a missing or `*/*` `Accept`), so put your preferred format
first:

```python
from wreath.negotiation import JSON, Serializer, serialize

CSV = Serializer("text/csv", lambda rows: to_csv(rows).encode())

@app.get("/export")
async def export(request):
    return serialize(request, rows, serializers=(JSON, CSV))
```

Just picking a format without serializing? `negotiate(accept_header, serializers)`
returns the chosen `Serializer` (or `None`).
