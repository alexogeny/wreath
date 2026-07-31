# Accept a Protocol Buffers request body

A device on a metered link sends a batch of readings, and every byte costs
something. You want the endpoint to take protobuf, and you want a malformed
payload to come back as a `400`, not a `500`.

## Declare the message

```python
from wreath.protobuf import ProtobufDecodeError, decode, field, message

@message
class Reading:
    sensor_id: int = field(1)
    celsius: float = field(2, kind="float")
    at_unix_ms: int = field(3, kind="sfixed64")

@message
class Batch:
    station: str = field(1)
    readings: list[Reading] = field(2)
```

`float` rather than the default `double` halves the temperature field, and
`sfixed64` is a better fit than a varint for a timestamp, whose high bits are
always set. Neither choice changes the Python you write.

## Read it in a handler

The body arrives as bytes, so read it and decode:

```python
from wreath import Wreath
from wreath.exceptions import BadRequest

app = Wreath()

@app.post("/readings")
async def ingest(request) -> dict:
    body = await request.body()
    try:
        batch = decode(Batch, body)
    except ProtobufDecodeError as exc:
        # A malformed body is the client's mistake, and a 4xx is what stops a
        # retrying agent from hammering the endpoint forever.
        raise BadRequest(str(exc)) from exc

    for reading in batch.readings:
        await store(reading)
    return {"accepted": len(batch.readings)}
```

The refusal matters as much as the parse. Every malformed-input failure —
truncation, a length prefix past the end of the buffer, a varint longer than ten
bytes, invalid UTF-8 — raises `ProtobufDecodeError`, so one `except` covers the
lot. Letting those surface as a `500` would tell a retrying client to try again,
and it will, forever.

## Answer in protobuf too

```python
from wreath.protobuf import encode
from wreath.response import Response

@app.post("/readings")
async def ingest(request) -> Response:
    ...
    return Response(
        encode(Receipt(accepted=len(batch.readings))),
        media_type="application/x-protobuf",
    )
```

## Guard the size before you parse

An ingest endpoint reads bytes from someone you do not control, so bound the
body. The native server's `max_body_bytes` answers a request that is too large
with a `413` before it reaches your handler at all — which is much cheaper than
decoding a large body and rejecting it afterwards.

## Older clients keep working

If you later add a field to `Reading`, older devices simply do not send it and
decode to its default. If a *newer* device sends a field this build has never
heard of, the decoder keeps the bytes and hands them back on re-encode, so a
relay in the middle of your estate does not destroy data it was never taught
about. That is the difference between a field number and a JSON key, and it is
why this codec preserves unknown fields where
[binding](../../guides/binding.md) rejects extra ones.

See [the guide](../../guides/protobuf.md) for the full argument, and
[`wreath.protobuf`](../../reference/protobuf.md) for the API.
