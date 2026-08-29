from __future__ import annotations

import datetime
import os

import pytest
from tracking.rpc import RELAY_METADATA
from tracking.wire import BatchReceipt, Position, milliseconds

from wreath.grpc import Status, Unframer, frame_message
from wreath.protobuf import decode, encode

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

skip_without_database = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the tracking stream tests",
)

PATH = "/tracking.Ingest/Relay"

#: The same window and the same collar the batch tests use, so a difference
#: between the two transports would show up as a difference in these numbers.
FIRST = datetime.datetime(2026, 5, 1, 6, 0, tzinfo=datetime.UTC)
COLLAR = 8
ANIMAL = 8

WALK = (
    (-1.9705, 36.1042),
    (-1.9705, 36.1069),
    (-1.9702, 36.1096),
    (-1.9698, 36.1123),
    (-1.9691, 36.1150),
)


def position(step: int, lat: float, lon: float, *, minutes: int = 20) -> Position:
    return Position(
        collar_id=COLLAR,
        recorded_at_ms=milliseconds(FIRST + datetime.timedelta(minutes=minutes * step)),
        lat=lat,
        lon=lon,
        accuracy_m=11.5,
        battery_pct=64,
        satellites=7,
    )


def stream_of(*positions: Position) -> bytes:
    """The wire body of a client-streaming call: one length-prefixed frame each.

    Sent as one ASGI body chunk, which is what a client that had already
    buffered its spool would produce. Framing is per message either way -- the
    server reads frames, not chunks -- so this is the same bytes a socket would
    deliver in ten pieces.
    """
    return b"".join(frame_message(encode(item)) for item in positions)


async def call(
    app,
    body: bytes,
    *,
    relay: str | None = "relay-kimana",
    http_version: str = "2",
    chunks: int = 1,
) -> list[dict]:
    """One gRPC call through the ASGI app; the messages it sent back."""
    headers = [
        (b"content-type", b"application/grpc+proto"),
        (b"te", b"trailers"),
    ]
    if relay is not None:
        headers.append((RELAY_METADATA.encode(), relay.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": http_version,
        "method": "POST",
        "path": PATH,
        "raw_path": PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "https",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 443),
    }
    size = max(1, len(body) // chunks)
    pieces = [body[at : at + size] for at in range(0, len(body), size)] or [b""]
    incoming = [
        {"type": "http.request", "body": piece, "more_body": index < len(pieces) - 1}
        for index, piece in enumerate(pieces)
    ]
    sent: list[dict] = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def trailers(sent: list[dict]) -> dict[str, str]:
    """`grpc-status` and friends, off whichever message carried them.

    gRPC puts its status in trailers, and an early refusal puts it in the
    HEADERS frame instead -- both are legal and a client must read either. Note
    that `trailers` on `http.response.start` is a *flag*: the values arrive in a
    separate `http.response.trailers` message, which is the ASGI extension
    `wreath.grpc` is the first consumer of.
    """
    found: dict[str, str] = {}
    for message in sent:
        if message["type"] in ("http.response.start", "http.response.trailers"):
            for name, value in message.get("headers", ()) or ():
                found[name.decode()] = value.decode()
    return found


def body_of(sent: list[dict]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def frames_in(body: bytes) -> list[bytes]:
    """Wreath's own unframer, so the test reads what a client reads."""
    return Unframer().feed(body)


def receipt_of(sent: list[dict]) -> BatchReceipt:
    frames = frames_in(body_of(sent))
    assert len(frames) == 1, f"a client-streaming call answers once, got {len(frames)}"
    return decode(BatchReceipt, frames[0])


@pytest.fixture
async def app():
    """The real application on a seeded schema with no fixes, as `test_ingest` uses.

    `TestClient` is entered purely to run the lifespan -- the ORM registry binds
    to its database there, so a call made before it has run reaches nothing --
    and then the *application* is handed to the tests, because a gRPC call is
    driven through a synthetic HTTP/2 scope rather than through the client.
    """
    from _tracking import build_schema, drop_schema
    from tracking.app import build

    from wreath.postgres import connect
    from wreath.testing import TestClient

    connection = await connect(_DSN)
    try:
        await build_schema(connection, fixes=False)
    finally:
        await connection.close()

    application = build(cross_worker=False)
    try:
        async with TestClient(application):
            yield application
    finally:
        connection = await connect(_DSN)
        try:
            await drop_schema(connection)
        finally:
            await connection.close()


@skip_without_database
async def test_a_streamed_walk_lands_the_same_rows_a_batch_would(app) -> None:
    sent = await call(app, stream_of(*(position(s, *WALK[s]) for s in range(5))))
    assert trailers(sent).get("grpc-status") == "0", trailers(sent)

    receipt = receipt_of(sent)
    assert receipt.accepted == 5
    assert receipt.rejected == 0
    assert receipt.watermark_ms == milliseconds(FIRST + datetime.timedelta(minutes=80))


@skip_without_database
async def test_the_frames_do_not_have_to_arrive_whole(app) -> None:
    sent = await call(app, stream_of(*(position(s, *WALK[s]) for s in range(5))), chunks=20)
    assert trailers(sent).get("grpc-status") == "0", trailers(sent)
    assert receipt_of(sent).accepted == 5


@skip_without_database
async def test_one_impossible_position_is_counted_rather_than_fatal(app) -> None:
    walk = [position(step, *WALK[step]) for step in range(4)]
    walk.append(position(4, 91.0, 36.0))
    sent = await call(app, stream_of(*walk))

    assert trailers(sent).get("grpc-status") == "0", trailers(sent)
    receipt = receipt_of(sent)
    assert receipt.accepted == 4
    assert receipt.rejected == 1


@skip_without_database
async def test_a_replayed_stream_lands_nothing_twice(app) -> None:
    from wreath.testing import TestClient

    body = stream_of(*(position(step, *WALK[step]) for step in range(5)))
    assert receipt_of(await call(app, body)).accepted == 5
    assert receipt_of(await call(app, body)).accepted == 5

    async with TestClient(app) as client:
        actor = client.acting_as("relay-1", roles=["ranger"], type="Observer")
        listed = await actor.get(f"/animals/{ANIMAL}/track?since=2026-05-01&days=1")
    assert len(listed.json()["fixes"]) == 5, "a replayed stream duplicated rows"


@skip_without_database
async def test_a_stream_that_sends_nothing_is_an_empty_receipt_not_an_error(
    app,
) -> None:
    sent = await call(app, b"")
    assert trailers(sent).get("grpc-status") == "0", trailers(sent)
    receipt = receipt_of(sent)
    assert receipt.accepted == 0
    assert receipt.watermark_ms == 0


@skip_without_database
async def test_a_stream_that_names_no_relay_is_refused_as_an_argument(app) -> None:
    sent = await call(app, stream_of(position(0, *WALK[0])), relay=None)
    found = trailers(sent)
    assert found.get("grpc-status") == str(int(Status.INVALID_ARGUMENT)), found
    assert "relay" in found.get("grpc-message", ""), found


@skip_without_database
async def test_a_call_over_http1_is_refused_naming_the_transport(app) -> None:
    sent = await call(app, stream_of(position(0, *WALK[0])), http_version="1.1")
    found = trailers(sent)
    assert found.get("grpc-status") == str(int(Status.UNIMPLEMENTED)), found
    assert "HTTP/2" in found.get("grpc-message", ""), found


@skip_without_database
async def test_the_rest_relay_still_answers(app) -> None:
    from tracking.wire import MEDIA_TYPE, PositionBatch

    from wreath.testing import TestClient

    body = encode(
        PositionBatch(
            relay="relay-kimana",
            positions=[position(step, *WALK[step]) for step in range(3)],
        )
    )
    async with TestClient(app) as client:
        actor = client.acting_as("relay-1", roles=["ranger"], type="Observer")
        response = await actor.post(
            "/ingest/positions", content=body, headers={"content-type": MEDIA_TYPE}
        )
    assert response.status == 200, response.text
    assert decode(BatchReceipt, response.body).accepted == 3


def test_the_streaming_frame_helpers_round_trip() -> None:
    messages = [position(step, *WALK[step]) for step in range(3)]
    assert [decode(Position, f) for f in frames_in(stream_of(*messages))] == messages
    # And a boundary anywhere still reassembles, which is the property the
    # chunked test above rests on.
    body = stream_of(*messages)
    unframer = Unframer()
    recovered = [m for at in range(0, len(body), 3) for m in unframer.feed(body[at : at + 3])]
    assert [decode(Position, frame) for frame in recovered] == messages
