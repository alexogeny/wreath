"""Stage six: the field station streams positions instead of batching them.

The REST relay in `routers.py` is a `POST` of a whole `PositionBatch`, which is
the right shape for a station that dials in, drains its spool and hangs up. A
station on a permanent link has a different problem: it hears a collar every few
seconds and has to choose between a request per fix and a delay while it
accumulates one worth sending. Streaming removes the choice — the connection
stays open and each position goes up as it arrives.

Three things are worth knowing before reading the code.

**This runs on wreath's own server and nowhere else.** gRPC needs HTTP/2 to
carry response trailers, `_pure/server.py` has no HTTP/2, and `serve` negotiates
`h2` through ALPN — so it also needs TLS. A call arriving over HTTP/1.1 is
refused with `UNIMPLEMENTED` naming the transport rather than failing obscurely.
The REST relay is not going anywhere; a deployment behind somebody else's ASGI
server keeps using it.

**The relay's name is call metadata, not a field.** `PositionBatch` names the
relay once for a whole batch, and a stream has no batch to hang that on. gRPC
metadata is exactly the right place — it is sent with the request headers,
before the first message, which is when the server wants it. The refusal for a
stream that names no relay is the same `IngestRefused` the `POST` path raises,
so the rule lives in `ingest.accept` and not in two places.

**One receipt, at the end.** This is a *client*-streaming method: the station
sends many, the server answers once. The alternative — a receipt per position,
bidirectionally — would let the station retire its spool sooner, and it is
deliberately not built here, because the whole point of the example is that the
landing logic is shared. `accept()` repairs the legs a new position splits,
which is a per-batch operation over the affected days; per-message receipts
would need it per message, and that is a different ingest, not a different
transport.
"""

from __future__ import annotations

import datetime
from typing import Any

from wreath.grpc import GrpcError, GrpcService, Status
from wreath.orm import Session

from .ingest import MAX_POSITIONS, IngestRefused, accept
from .live import LiveMap
from .wire import BatchReceipt, Position, PositionBatch, milliseconds

#: The gRPC metadata key carrying the relay's name. Lowercase because HTTP/2
#: header names are, and a client that sends `Relay` gets the same header.
RELAY_METADATA = "relay"


def ingest_service(registry: Any, live: LiveMap) -> GrpcService:
    """The streaming ingest service, bound to one ORM registry and one map.

    Takes the registry rather than importing one, for the reason
    `camera_trap.app.open_session` gives: the registry does not exist until
    `app.orm(...)` has run, so a module-level session factory would close over
    nothing.

    Args:
        registry: The ORM registry from `app.orm(...)`.
        live: The live map, so a streamed position reaches an open browser the
            same way a POSTed one does.

    Returns:
        A `GrpcService`; call `.router()` and include it like any other.
    """
    service = GrpcService("tracking.Ingest")

    @service.client_stream(request=Position, response=BatchReceipt)
    async def Relay(request: Any, positions: Any) -> BatchReceipt:
        """Stream positions up; get one receipt back.

        The stream is bounded by `MAX_POSITIONS` for the same reason the batch
        is, and it matters more here: a `POST` body has a length the server can
        refuse before reading, while a stream is only as long as the client
        decides to make it. Counting as they arrive and refusing past the limit
        is what keeps one station from filling this worker's memory.
        """
        relay = request.header(RELAY_METADATA) or ""
        collected: list[Position] = []
        async for position in positions:
            collected.append(position)
            if len(collected) > MAX_POSITIONS:
                raise GrpcError(
                    Status.RESOURCE_EXHAUSTED,
                    f"more than {MAX_POSITIONS} positions in one stream; open a "
                    "second call to drain a long outage",
                )

        session = Session(registry, "write")
        try:
            try:
                receipt = await accept(
                    session,
                    PositionBatch(relay=relay, positions=collected),
                    now=datetime.datetime.now(tz=datetime.UTC),
                )
            except IngestRefused as error:
                # INVALID_ARGUMENT and not UNKNOWN: the station sent something
                # this server will never accept, so retrying is pointless and a
                # retryable code would turn one bad stream into a loop.
                raise GrpcError(Status.INVALID_ARGUMENT, str(error)) from error
        finally:
            await session.close()

        # After the write, never before -- the same ordering the REST relay
        # keeps, and for the same reason: a broadcast for positions that then
        # failed to land would put fixes on every open map that are in no table.
        await live.publish(receipt.published)

        return BatchReceipt(
            accepted=receipt.accepted,
            rejected=receipt.rejected,
            watermark_ms=(
                milliseconds(receipt.watermark) if receipt.watermark else 0
            ),
        )

    return service
