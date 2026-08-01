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

## Protocol Buffers

`PROTOBUF` serves `application/x-protobuf` from the same handler, for a mobile
client on a metered link or a service-to-service call where the bytes matter:

```python
from wreath.negotiation import JSON, PROTOBUF, serialize
from wreath.protobuf import field, message

@message
class Reading:
    sensor: int = field(1)
    celsius: float = field(2)

@app.get("/readings/{sensor}")
async def reading(request, sensor: int) -> Response:
    return serialize(request, Reading(sensor=sensor, celsius=21.5),
                     serializers=(PROTOBUF, JSON))
```

**It is deliberately not one of the defaults**, and that is the one thing worth
understanding before reaching for it. JSON and MessagePack are self-describing:
hand them any dict, list or dataclass and they encode it. Protobuf is
schema-driven — the field *numbers* are the wire contract, and there is nothing
to derive them from for an undeclared value. So `serialize` can only offer it
where the handler returns a class built by
[`@message`](protobuf.md), and it has to be named at the call site.

Adding it to `DEFAULT_SERIALIZERS` would mean every existing `serialize()` call
in an application — most of which return a plain dict — started failing for any
client that sent `Accept: application/x-protobuf`. Handing `PROTOBUF.encode` an
undeclared value refuses by name rather than raising from inside the codec:

```
TypeError: application/x-protobuf can only encode a class declared with
@message from wreath.protobuf; got dict.
```

That refusal reaches the caller. `Serializer.encode` never falls back to another
format, because a client that asked for protobuf and silently received JSON
would parse the bytes as a message and get garbage.

### The request half

Reading a protobuf **request body** is a separate question from negotiating a
response, and `wreath.binding` answers it symmetrically: a body annotated with a
`@message` class binds from protobuf bytes when `Content-Type` says so, and from
JSON otherwise. The annotation does not mean protobuf-only — see
[Binding](binding.md#a-protobuf-body), which also sets out why an unknown field
*name* in JSON is refused while an unknown field *number* on the protobuf wire
is preserved.

`PROTOBUF_MEDIA_TYPES` is the set binding matches against: Wreath emits
`PROTOBUF.media_type` and reads both that and `application/protobuf`, the IANA
registration. The set lives beside the serializer that emits one of them, so the
request half and the response half cannot disagree about what protobuf is
called.
