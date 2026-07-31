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


@tracker.unary(request=PositionRequest, response=Position)
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
- `permissions=`, `dependencies=` and `middleware=` pass through to
  `RouteDefinition` unchanged.
- `permissions_router` and `wreath mutant` read those declarations from the same
  place they read a REST route's. **There is no second authorization model**,
  which is the whole reason a method is a route rather than a separate dispatch
  path.

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

**What a deadline does not yet do is stop in-flight database work.** The handler
is cancelled at the deadline, and whether that cancellation reaches a running
ORM query is an open question for Wreath generally, not one this module settles.
Treat `grpc-timeout` as a bound on the *response*, not a guarantee that the
server stopped working.

## Message size

Every message is bounded by `max_message_bytes` (4 MiB by default, the value
gRPC clients expect). The four-byte length prefix is attacker-controlled, so it
is checked against that limit **before** anything is allocated — a lie in those
bytes cannot make the server reserve what the peer never intends to send.

## What is not built

- **Server reflection.** It requires protobuf *descriptors*, which
  [`wreath.protobuf`](protobuf.md) deliberately does not build. The two
  decisions are coupled and would have to be reopened together.
- **A gRPC client.** `wreath.http_client` has no HTTP/2 at all, so calling gRPC
  means building an HTTP/2 client first — a subsystem in its own right. Wreath
  serves gRPC; it does not yet call it.
- **Compression.** `grpc-encoding: identity` only. A compressed message is
  refused by name rather than mis-parsed.
- gRPC-Web, the health-checking protocol, and client-side concerns (load
  balancing, retry configuration, xDS).
