"""gRPC on HTTP/2: cancellation, backpressure, and the streams beside them.

Two rows of the gRPC plan were left unchecked with the same honest note: the
behaviour *rests* on HTTP/2 flow control and stream resets, which `wreath.grpc`
neither adds to nor bypasses, and that is an argument rather than a test.
Writing the test needs a client that stops reading mid-stream, which no unit
test of the framing had.

The `H2Driver` in this package is that client. It drives the native
`Http2Protocol` over a fake transport with no socket and no TLS, so a test can
do the two things a real client makes hard: **grant exactly zero window** and
**send `RST_STREAM` at a chosen moment**. Everything below is therefore
executable rather than reasoned, which is the whole point of the rows.

Three properties, and they are not separable in practice — which is why they
are in one file rather than three:

1. **A client cancellation reaches the application.** `RST_STREAM(CANCEL)`
   becomes `http.disconnect`, the handler's `CancelledError` runs its cleanup,
   and the server stops producing.
2. **A slow consumer causes bounded backpressure.** A server-streaming handler
   whose peer grants no window does not run ahead: the generator is suspended
   at the first frame it cannot send, and nothing is buffered on its behalf.
3. **An unrelated stream stays healthy** while that one is stalled. This is the
   property that makes the first two worth having: a design that bounded
   memory by stalling the *connection* would pass (2) and be useless.

`wreath.grpc` is exercised through its real router, so the frames on the wire
are the framed protobuf messages a `grpcio` client would read. Interop with
Google's client is proved separately in `tests/test_grpc_interop.py`; what
cannot be done there is refuse to read.
"""

from __future__ import annotations

import asyncio

from wreath import Wreath
from wreath._native import extension as _extension
from wreath.grpc import GrpcService
from wreath.protobuf import field, message

from . import support
from .conftest import requires_h2

pytestmark = requires_h2


@message
class Tick:
    index: int = field(1, kind="uint32")


def _headers(path: str, *, end_stream: bool = False) -> bytes:
    return support.build_headers_frame(
        1,
        support.request_headers(
            path=path.encode(),
            method=b"POST",
            extra=[
                (b"content-type", b"application/grpc+proto"),
                (b"te", b"trailers"),
            ],
        ),
        end_stream=end_stream,
    )


def _data_frames(driver, stream_id: int = 1):
    return [
        frame
        for frame in driver.frames()
        if frame.type == support.DATA and frame.stream_id == stream_id
    ]


def _tick_body() -> bytes:
    from wreath.grpc import frame_message
    from wreath.protobuf import encode

    return frame_message(encode(Tick(index=0)))


# --- 1. cancellation reaches the handler -------------------------------------


async def test_a_client_reset_cancels_a_server_streaming_handler(make_driver):
    """`RST_STREAM(CANCEL)` mid-stream stops the generator, and the cleanup runs.

    Both halves matter. A server that noticed the reset but left the generator
    suspended forever would leak one task per abandoned call, which is the shape
    that takes a process down during the incident everybody is already watching.
    """
    produced = 0
    cleaned = asyncio.Event()
    stopped_by: list[str] = []

    service = GrpcService("wreath.test.Pressure")

    @service.server_stream(request=Tick, response=Tick)
    async def Forever(request, message: Tick):
        nonlocal produced
        try:
            index = 0
            while True:
                yield Tick(index=index)
                produced += 1
                index += 1
                await asyncio.sleep(0)
        except (asyncio.CancelledError, GeneratorExit) as stop:
            # Either is a correct stop and the distinction is worth recording:
            # a task cancelled at an `await` raises `CancelledError`, while an
            # async generator finalised at a `yield` gets `GeneratorExit` from
            # `aclose()`. What must never happen is neither -- a generator left
            # suspended forever is one leaked task per abandoned call.
            stopped_by.append(type(stop).__name__)
            raise
        finally:
            # The `finally` is what a real handler releases a connection or a
            # file in, so it is the thing worth asserting rather than the
            # exception type alone.
            cleaned.set()

    app = Wreath()
    app.include_router(service.router())

    driver = make_driver(app)
    # A window big enough for a few messages and no more, so the handler is
    # genuinely mid-stream when the reset arrives rather than finished.
    await driver.preface({support.SETTINGS_INITIAL_WINDOW_SIZE: 64})
    await driver.feed_and_settle(
        support.build_headers_frame(
            1,
            support.request_headers(
                path=b"/wreath.test.Pressure/Forever",
                method=b"POST",
                extra=[
                    (b"content-type", b"application/grpc+proto"),
                    (b"te", b"trailers"),
                ],
            ),
            end_stream=False,
        )
    )
    await driver.feed_and_settle(
        support.encode_frame(support.DATA, 0x1, 1, _tick_body())
    )
    await driver.settle()
    assert produced >= 1, "the handler never started; this proves nothing"

    await driver.feed_and_settle(support.encode_rst_stream(1, support.CANCEL))
    await asyncio.wait_for(cleaned.wait(), timeout=2)
    assert stopped_by in (["CancelledError"], ["GeneratorExit"]), (
        f"the generator was stopped by {stopped_by or 'nothing'}: a `finally` "
        "that runs without either means the frame was collected rather than "
        "finalised, and cleanup order is then whatever the GC decides"
    )

    stopped_at = produced
    await driver.settle()
    await asyncio.sleep(0)
    assert produced == stopped_at, "the handler kept producing after the reset"


async def test_a_reset_before_the_first_message_still_cleans_up(make_driver):
    """The other end of the same property: cancelled while awaiting its request.

    A client-streaming handler parked on `async for` is the case a
    reset-aware server is most likely to leak, because nothing has been sent
    and there is no send to fail.
    """
    cleaned = asyncio.Event()
    service = GrpcService("wreath.test.Pressure")

    @service.client_stream(request=Tick, response=Tick)
    async def Collect(request, messages) -> Tick:
        try:
            total = 0
            async for _ in messages:
                total += 1
            return Tick(index=total)
        finally:
            cleaned.set()

    app = Wreath()
    app.include_router(service.router())

    driver = make_driver(app)
    await driver.preface()
    await driver.feed_and_settle(_headers("/wreath.test.Pressure/Collect"))
    await driver.settle()
    assert not cleaned.is_set(), "the handler finished before it was reset"

    await driver.feed_and_settle(support.encode_rst_stream(1, support.CANCEL))
    await asyncio.wait_for(cleaned.wait(), timeout=2)


# --- 2. a slow consumer is bounded -------------------------------------------


async def test_a_server_stream_to_a_consumer_that_reads_nothing_is_bounded(
    make_driver,
):
    """The row this file exists for.

    A handler that would yield ten thousand messages, against a peer whose
    window is zero. If the response were buffered on the application's behalf,
    the generator would run to completion and the memory for every message
    would already have been spent. What must happen instead is that `send`
    suspends at the first frame that will not fit, so the *generator* is the
    thing that stops.

    The bound asserted is deliberately generous and structural rather than a
    byte count: at most a couple of messages may be in flight (one being
    framed, one queued behind it) out of ten thousand. A regression that
    reintroduced buffering does not creep past a threshold like that, it
    blows through it.
    """
    produced = 0
    service = GrpcService("wreath.test.Pressure")

    @service.server_stream(request=Tick, response=Tick)
    async def Flood(request, message: Tick):
        nonlocal produced
        for index in range(10_000):
            yield Tick(index=index)
            produced += 1

    app = Wreath()
    app.include_router(service.router())

    driver = make_driver(app)
    await driver.preface({support.SETTINGS_INITIAL_WINDOW_SIZE: 0})
    await driver.feed_and_settle(
        support.build_headers_frame(
            1,
            support.request_headers(
                path=b"/wreath.test.Pressure/Flood",
                method=b"POST",
                extra=[
                    (b"content-type", b"application/grpc+proto"),
                    (b"te", b"trailers"),
                ],
            ),
            end_stream=False,
        )
    )
    await driver.feed_and_settle(
        support.encode_frame(support.DATA, 0x1, 1, _tick_body())
    )
    # Several settles, so "it has not run yet" cannot be mistaken for "it is
    # bounded". A buffering implementation finishes all ten thousand here.
    for _ in range(20):
        await driver.settle()

    assert produced <= 2, (
        f"{produced} messages were produced against a zero window: the response "
        "is being buffered rather than back-pressured"
    )
    assert sum(len(f.payload) for f in _data_frames(driver)) == 0, (
        "bytes went out on a stream with no window"
    )


async def test_granting_window_lets_the_stream_resume_exactly_that_far(
    make_driver,
):
    """Backpressure that never releases is a hang, not a bound.

    The pair for the test above: the same handler, and a window granted after
    the fact, must produce *some* messages and then stop again rather than
    either staying stuck or running to completion.
    """
    produced = 0
    service = GrpcService("wreath.test.Pressure")

    @service.server_stream(request=Tick, response=Tick)
    async def Flood(request, message: Tick):
        nonlocal produced
        for index in range(10_000):
            yield Tick(index=index)
            produced += 1

    app = Wreath()
    app.include_router(service.router())

    driver = make_driver(app)
    await driver.preface({support.SETTINGS_INITIAL_WINDOW_SIZE: 0})
    await driver.feed_and_settle(
        support.build_headers_frame(
            1,
            support.request_headers(
                path=b"/wreath.test.Pressure/Flood",
                method=b"POST",
                extra=[
                    (b"content-type", b"application/grpc+proto"),
                    (b"te", b"trailers"),
                ],
            ),
            end_stream=False,
        )
    )
    await driver.feed_and_settle(
        support.encode_frame(support.DATA, 0x1, 1, _tick_body())
    )
    for _ in range(5):
        await driver.settle()
    assert produced <= 2

    # 64 bytes of credit: a handful of framed `Tick`s, not ten thousand.
    await driver.feed_and_settle(support.encode_window_update(1, 64))
    for _ in range(10):
        await driver.settle()

    sent = sum(len(f.payload) for f in _data_frames(driver))
    assert 0 < sent <= 64, f"{sent} bytes went out for 64 bytes of credit"
    assert produced < 10_000, "the credit released the whole stream, not part of it"


# --- 3. the streams beside it stay healthy -----------------------------------


async def test_a_stalled_grpc_stream_does_not_stall_an_unrelated_one(make_driver):
    """The property that makes backpressure worth having rather than merely safe.

    Bounding memory by stalling the *connection* would satisfy every assertion
    above and be useless: one slow reader would take out every other caller on
    the same connection, which is exactly the incident-time failure gRPC
    multiplexing exists to avoid.
    """
    service = GrpcService("wreath.test.Pressure")
    answered = asyncio.Event()

    @service.server_stream(request=Tick, response=Tick)
    async def Flood(request, message: Tick):
        for index in range(10_000):
            yield Tick(index=index)

    @service.unary(request=Tick, response=Tick)
    async def Quick(request, message: Tick) -> Tick:
        answered.set()
        return Tick(index=99)

    app = Wreath()
    app.include_router(service.router())

    driver = make_driver(app)
    await driver.preface({support.SETTINGS_INITIAL_WINDOW_SIZE: 0})

    def _open(stream_id: int, method: str) -> bytes:
        return support.build_headers_frame(
            stream_id,
            support.request_headers(
                path=f"/wreath.test.Pressure/{method}".encode(),
                method=b"POST",
                extra=[
                    (b"content-type", b"application/grpc+proto"),
                    (b"te", b"trailers"),
                ],
            ),
            end_stream=False,
        )

    await driver.feed_and_settle(_open(1, "Flood"))
    await driver.feed_and_settle(
        support.encode_frame(support.DATA, 0x1, 1, _tick_body())
    )
    await driver.feed_and_settle(_open(3, "Quick"))
    await driver.feed_and_settle(
        support.encode_frame(support.DATA, 0x1, 3, _tick_body())
    )
    # Credit for the second stream only. The first stays at zero.
    await driver.feed_and_settle(support.encode_window_update(3, 65_535))
    for _ in range(10):
        await driver.settle()

    assert answered.is_set(), "the unrelated handler never ran"
    assert sum(len(f.payload) for f in _data_frames(driver, 3)) > 0, (
        "a stalled stream stopped an unrelated one from being answered"
    )
    assert sum(len(f.payload) for f in _data_frames(driver, 1)) == 0


async def test_resetting_the_stalled_stream_leaves_the_other_running(make_driver):
    """Cancellation and multiplexing together, which is the real sequence: the
    slow caller gives up, and everybody else must be unaffected."""
    service = GrpcService("wreath.test.Pressure")
    cleaned = asyncio.Event()
    replied = asyncio.Event()

    @service.server_stream(request=Tick, response=Tick)
    async def Flood(request, message: Tick):
        try:
            for index in range(10_000):
                yield Tick(index=index)
        finally:
            cleaned.set()

    @service.unary(request=Tick, response=Tick)
    async def Quick(request, message: Tick) -> Tick:
        replied.set()
        return Tick(index=7)

    app = Wreath()
    app.include_router(service.router())

    driver = make_driver(app)
    await driver.preface({support.SETTINGS_INITIAL_WINDOW_SIZE: 0})

    def _open(stream_id: int, method: str) -> bytes:
        return support.build_headers_frame(
            stream_id,
            support.request_headers(
                path=f"/wreath.test.Pressure/{method}".encode(),
                method=b"POST",
                extra=[
                    (b"content-type", b"application/grpc+proto"),
                    (b"te", b"trailers"),
                ],
            ),
            end_stream=False,
        )

    await driver.feed_and_settle(_open(1, "Flood"))
    await driver.feed_and_settle(
        support.encode_frame(support.DATA, 0x1, 1, _tick_body())
    )
    await driver.feed_and_settle(support.encode_rst_stream(1, support.CANCEL))
    await asyncio.wait_for(cleaned.wait(), timeout=2)

    await driver.feed_and_settle(_open(3, "Quick"))
    await driver.feed_and_settle(
        support.encode_frame(support.DATA, 0x1, 3, _tick_body())
    )
    await driver.feed_and_settle(support.encode_window_update(3, 65_535))
    for _ in range(10):
        await driver.settle()

    assert replied.is_set(), "the connection did not survive the reset"


# --- the harness itself ------------------------------------------------------


def test_the_request_body_is_a_real_grpc_frame() -> None:
    """Every test above feeds `_tick_body()` as the request. If that were not a
    well-formed length-prefixed message the handlers would refuse before any of
    the properties under test were reached, and the file would be green about
    nothing."""
    from wreath.grpc import Unframer
    from wreath.protobuf import decode

    frames = Unframer().feed(_tick_body())
    assert [decode(Tick, frame) for frame in frames] == [Tick(index=0)]


def test_grpc_needs_http2_which_is_why_these_live_here() -> None:
    """A note in executable form: gRPC rides HTTP/2, which `wreath.server`
    serves and a foreign ASGI server does not, so the whole file is gated on
    `_server` rather than run against a portable transport."""

    assert hasattr(_extension("_server"), "Http2Protocol")
