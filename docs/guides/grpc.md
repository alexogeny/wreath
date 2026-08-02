# gRPC

**gRPC runs on Wreath's own server and nowhere else, and it requires TLS.** Both
are properties of the transport rather than choices this module made, and both
are worth knowing before you design around them:

- gRPC carries its call status in HTTP/2 **response trailers**. Wreath's native
  HTTP/2 server emits them; a foreign ASGI server — uvicorn, hypercorn — does
  not, so a gRPC service behind one answers `UNIMPLEMENTED` naming the reason
  rather than returning a `200` whose trailers never arrive.
- Wreath negotiates HTTP/2 through **ALPN** and never inspects the first
  application bytes to guess a protocol, so prior-knowledge `h2c` — plaintext
  HTTP/2 — is not available. `serve` refuses `h2` without `tls=` or `ssl=`.
  A `grpc.insecure_channel` therefore cannot reach a Wreath service; use
  `secure_channel`.

Everything else is ordinary Wreath. `wreath.grpc` is pure Python over the ASGI
messages the native server already understands: there is no C in it and no
change to the server.

## A method is a route

```python
from wreath import Wreath
from wreath.authorization import roles
from wreath.grpc import GrpcService
from wreath.protobuf import field, message


@message
class PositionRequest:
    collar_id: int = field(1)


@message
class Position:
    collar_id: int = field(1)
    recorded_at: int = field(2)


tracker = GrpcService("camera.Tracker")


@tracker.unary(request=PositionRequest, response=Position, action="Collar::read")
@roles("ranger")
async def GetPosition(request, message: PositionRequest) -> Position:
    """The collar's last known position."""
    return Position(collar_id=message.collar_id, recorded_at=0)


app = Wreath()
app.include_router(tracker.router())
```

`router()` returns an ordinary `Router` whose routes are
`POST /{service}/{method}` — because that *is* how a gRPC client addresses a
call, not a convention layered on top. The consequence is the point:

- `@roles`, `@permissions` and `@authorize` mean exactly what they mean on a
  REST route, and are enforced by the same middleware tape.
- `action=` (and its optional `resource=`) on the method decorator **is**
  `@authorize`, spelled at the declaration the way
  [`@mcp.tool(action=…)`](mcp.md) spells it. Write it either way; they build the
  same requirement, and the second `@authorize` on a method merges with the
  first exactly as two decorators do.
- `permissions=`, `dependencies=` and `middleware=` pass through to
  `RouteDefinition` unchanged. Anything else the route decorator does not accept
  is a `TypeError` at import — `roles=` and `rate_limit=` are **decorators**
  here, not keywords.
- `permissions_router` and `wreath mutant` read those declarations from the same
  place they read a REST route's, so a gRPC method's action is in the
  [permission manifest](permissions.md#one-vocabulary-four-protocols) beside
  every other surface's. **There is no second authorization model**, which is the
  whole reason a method is a route rather than a separate dispatch path.

gRPC routes carry `include_in_schema=False`: a gRPC method is not a REST
operation, and describing it as one would put a path in the OpenAPI document
that no HTTP client can call.

## The four call shapes

| Decorator | Handler signature |
| --- | --- |
| `@service.unary` | `async def M(request, message) -> Response` |
| `@service.server_stream` | `async def M(request, message)` — yields responses |
| `@service.client_stream` | `async def M(request, messages) -> Response` |
| `@service.bidi` | `async def M(request, messages)` — yields responses |

Streaming is expressed as async iterators in both directions, which is what
`Request.stream()`, `SSEResponse` and the WebSocket API already look like.

### A slow consumer, and a client that gives up

**A server-streaming response to a peer that stops reading does not buffer.**
HTTP/2 flow control is the mechanism and the native server implements it, so
`send` suspends at the first frame that will not fit and the *generator* is what
stops — the handler does not run ahead producing messages nobody has room for.
A client that gives up entirely sends `RST_STREAM`, which finalises the
generator so its `finally` runs; a generator left suspended forever would be one
leaked task per abandoned call, which is the shape that takes a process down
during the incident everyone is already watching.

Neither of those is an argument here.
`tests/http2/test_grpc_streaming_pressure.py` drives the native HTTP/2 protocol
with a client that grants **zero window** and resets at a chosen moment: a
handler that would yield ten thousand messages produces at most two, granting 64
bytes of credit releases exactly that much and no more, and an unrelated stream
on the same connection is answered throughout. That last one is the property
that makes the bound worth having — a design that bounded memory by stalling the
*connection* would satisfy every other assertion and be useless.

## Statuses

Raise `GrpcError(Status.PERMISSION_DENIED, "…")` when the refusal is the answer.
Anything else that escapes a handler is mapped conservatively:

| Raised | `grpc-status` |
| --- | --- |
| `GrpcError` | as given |
| `Forbidden` | `PERMISSION_DENIED` |
| `Unauthorized` | `UNAUTHENTICATED` |
| `NotFound` | `NOT_FOUND` |
| `UnprocessableEntity`, `BadRequest` | `INVALID_ARGUMENT` |
| `TooManyRequests`, `PayloadTooLarge` | `RESOURCE_EXHAUSTED` |
| `TimeoutError` | `DEADLINE_EXCEEDED` |
| anything else | `UNKNOWN` |

`UNKNOWN` is deliberate for the last row. `UNAVAILABLE` and `ABORTED` both tell a
client the call is worth retrying, and an exception nobody classified must never
say that.

**The HTTP status is always 200, including for a refusal.** In gRPC the
transport succeeded whenever the server was reached; the call's outcome is the
`grpc-status` trailer. A handler that fails *after* its first message is already
on the wire still reports its status there — re-raising would abort the stream
with no status at all, which a client reports as an unexplained transport error
rather than the refusal it was.

## Deadlines

A client's `grpc-timeout` is honoured. A malformed value is refused with
`INVALID_ARGUMENT` rather than ignored: treating an unparseable deadline as "no
deadline" would let a call outlive the caller waiting on it, which is the one
outcome the header exists to prevent.

**A deadline stops in-flight database work too.** The handler is cancelled at
the deadline, and the driver cancels a PostgreSQL backend whenever the task
awaiting it is cancelled — whatever did the cancelling — by sending a wire-level
`CancelRequest` on a second connection. So an expired `grpc-timeout` is a bound
on the *work*, not only on the response.

That is asserted rather than reasoned, because "the same chain" is an argument:
`tests/test_disconnect_cancels_query.py::test_a_grpc_deadline_stops_the_postgresql_backend`
drives a real `grpc-timeout` over HTTP/2 at a handler running a 30-second
`pg_sleep`, and reads the verdict out of `pg_stat_activity` on a **third**
connection. A companion asserts the pooled connection is usable afterwards,
because a cancelled query returned with its state half-consumed would answer the
next borrower with the previous one's rows.

The bound is on a query awaited through a wreath connection. Work that is not
awaiting anything — a tight CPU loop — is not interruptible, here or anywhere
else in the framework.

## Message size

Every message is bounded by `max_message_bytes` (4 MiB by default, the value
gRPC clients expect). The four-byte length prefix is attacker-controlled, so it
is checked against that limit **before** anything is allocated — a lie in those
bytes cannot make the server reserve what the peer never intends to send.

## Compression

`identity` and `gzip`, in both directions, with nothing to configure.

- A request whose `grpc-encoding` is `gzip` is decompressed per message. An
  encoding this server does not implement is `UNIMPLEMENTED` and names itself,
  and the response carries `grpc-accept-encoding: identity,gzip` so a client
  that can re-send knows what to re-send as.
- A response is compressed when the client's `grpc-accept-encoding` lists gzip.
  Naming a coding Wreath does not implement is *not* an error — the list is what
  the client can read, and identity is always readable — so the reply is simply
  uncompressed rather than refused.

Two details are worth knowing because they look like bugs from the outside:

**The flag is per message, and Wreath declines when compressing would grow
one.** gzip's header and trailer cost about twenty bytes, so a short reply comes
out larger compressed. A response may therefore carry `grpc-encoding: gzip` in
its headers and a flag byte of `0` on a small message; every gRPC client reads
that correctly, because the header says what the messages *may* be compressed
with rather than what they are.

**A compressed message is bounded twice.** The length prefix bounds the bytes on
the wire and says nothing about the decoded size — a couple of kilobytes of
zeros expand to megabytes — so `max_message_bytes` is applied again to the
decompressed result, through `wreath.compression`'s
`gzip_decompress(..., max_output_bytes=…)`. The two refusals read differently:
one names the wire length, the other names decompression.

zstd is not offered even though [`wreath.compression`](compression.md) has it.
`grpc-encoding` values are a registry shared with every other implementation,
and a coding a Go or Java client cannot name is a dialect rather than a feature.

## What is not built

- **Server reflection.** It requires protobuf *descriptors*, which
  [`wreath.protobuf`](protobuf.md) deliberately does not build. The two
  decisions are coupled and would have to be reopened together.
- **A gRPC client.** `wreath.http_client` has no HTTP/2 at all, so calling gRPC
  means building an HTTP/2 client first — a subsystem in its own right. Wreath
  serves gRPC; it does not yet call it.
- **Codings other than `identity` and `gzip`.** `deflate` and `snappy` are
  refused by name rather than mis-parsed; see [Compression](#compression).
- gRPC-Web, the health-checking protocol, and client-side concerns (load
  balancing, retry configuration, xDS).
