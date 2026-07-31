# Serve a gRPC method

You have a service in another language whose contract is a `.proto`, and it
wants to call you over gRPC.

## Declare the messages and the service

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
    latitude: float = field(2)
    longitude: float = field(3)


tracker = GrpcService("camera.Tracker")


@tracker.unary(request=PositionRequest, response=Position)
@roles("ranger")
async def GetPosition(request, message: PositionRequest) -> Position:
    """The collar's last known position."""
    row = await request.state.db.fetch_position(message.collar_id)
    return Position(collar_id=row.id, latitude=row.lat, longitude=row.lon)


app = Wreath()
app.include_router(tracker.router())
```

`@roles("ranger")` is the same decorator a REST route uses, enforced by the same
middleware tape. A caller without a ranger identity is refused before
`GetPosition` runs.

## Run it

gRPC needs HTTP/2 **with TLS** — Wreath negotiates h2 through ALPN and never
guesses a protocol from the first bytes, so plaintext `h2c` is not available:

```python
from wreath.server import ServerConfig, TLSConfig, serve

server = await serve(
    app,
    ServerConfig(host="0.0.0.0", port=8443, protocols=("h2",)),
    tls=TLSConfig(certfile="server.pem", keyfile="server.key"),
)
```

## Call it

```python
import grpc

credentials = grpc.ssl_channel_credentials(open("ca.pem", "rb").read())
async with grpc.aio.secure_channel("tracker.example:8443", credentials) as channel:
    call = channel.unary_unary(
        "/camera.Tracker/GetPosition",
        request_serializer=encode,
        response_deserializer=lambda data: decode(Position, data),
    )
    position = await call(PositionRequest(collar_id=7), timeout=2.0)
```

The path is `/{service}/{method}` because that is how gRPC addresses a call.
`timeout=` becomes a `grpc-timeout` header the server honours.

## Stream instead

```python
@tracker.server_stream(request=PositionRequest, response=Position)
async def Track(request, message: PositionRequest):
    """Every position as it arrives."""
    async for row in follow(message.collar_id):
        yield Position(collar_id=row.id, latitude=row.lat, longitude=row.lon)
```

`client_stream` and `bidi` take an async iterator of requests instead of a
single message. All four shapes are async iterators in whichever direction
streams, matching `Request.stream()` and the WebSocket API.

## Refuse with a status

```python
from wreath.grpc import GrpcError, Status

raise GrpcError(Status.NOT_FOUND, "no such collar")
```

Wreath's own exceptions map too — `Forbidden` becomes `PERMISSION_DENIED`,
`UnprocessableEntity` becomes `INVALID_ARGUMENT`. Anything unclassified becomes
`UNKNOWN` rather than a code that would tell the client to retry.

Reference: [`wreath.grpc`](../../reference/grpc.md) ·
Guide: [gRPC](../../guides/grpc.md)
