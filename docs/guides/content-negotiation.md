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

### The same value is representable, or neither format takes it

Both encoders accept the same things, so a handler cannot succeed on one
negotiated format and fail on the other. That matters most for dictionary keys:
MessagePack is happy to write an array as a map key, but no decoder can read one
back — a list is unhashable, so the map cannot be rebuilt on the far side. Both
encoders refuse it, in the same words `json.dumps` uses:

```python
serialize(request, {(1, 2): "point"})
# TypeError: keys must be str, int, float, bool, bytes or None, not tuple
```

Keys may be `str`, `int`, `float`, `bool`, `bytes` or `None`. (`bytes` is absent
from JSON's list because JSON has no way to write it; MessagePack does, and it
round-trips, so it is allowed there.) Values are unrestricted — nested lists and
dicts are fine, it is only the *key* position that has to stay scalar.

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
