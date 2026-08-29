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


async def test_a_client_reset_cancels_a_server_streaming_handler(make_driver):
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
    await driver.feed_and_settle(support.encode_frame(support.DATA, 0x1, 1, _tick_body()))
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


async def test_a_server_stream_to_a_consumer_that_reads_nothing_is_bounded(
    make_driver,
):
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
    await driver.feed_and_settle(support.encode_frame(support.DATA, 0x1, 1, _tick_body()))
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
    await driver.feed_and_settle(support.encode_frame(support.DATA, 0x1, 1, _tick_body()))
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


async def test_a_stalled_grpc_stream_does_not_stall_an_unrelated_one(make_driver):
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
    await driver.feed_and_settle(support.encode_frame(support.DATA, 0x1, 1, _tick_body()))
    await driver.feed_and_settle(_open(3, "Quick"))
    await driver.feed_and_settle(support.encode_frame(support.DATA, 0x1, 3, _tick_body()))
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
    await driver.feed_and_settle(support.encode_frame(support.DATA, 0x1, 1, _tick_body()))
    await driver.feed_and_settle(support.encode_rst_stream(1, support.CANCEL))
    await asyncio.wait_for(cleaned.wait(), timeout=2)

    await driver.feed_and_settle(_open(3, "Quick"))
    await driver.feed_and_settle(support.encode_frame(support.DATA, 0x1, 3, _tick_body()))
    await driver.feed_and_settle(support.encode_window_update(3, 65_535))
    for _ in range(10):
        await driver.settle()

    assert replied.is_set(), "the connection did not survive the reset"


def test_the_request_body_is_a_real_grpc_frame() -> None:
    from wreath.grpc import Unframer
    from wreath.protobuf import decode

    frames = Unframer().feed(_tick_body())
    assert [decode(Tick, frame) for frame in frames] == [Tick(index=0)]


def test_grpc_needs_http2_which_is_why_these_live_here() -> None:

    assert hasattr(_extension("_server"), "Http2Protocol")
