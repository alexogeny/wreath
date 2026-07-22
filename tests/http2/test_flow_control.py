"""Flow control and concurrency (RFC 9113 s5.2, s6.9, s5.1.2)."""
from __future__ import annotations

import asyncio

import pytest

from . import support
from .conftest import ok_app, requires_h2

pytestmark = [requires_h2, pytest.mark.asyncio]


def _goaway_code(d):
    goaways = [f for f in d.frames() if f.type == support.GOAWAY]
    assert goaways
    return support.parse_goaway(goaways[-1].payload)[1]


async def _echo_request(make_driver, body_settings=None):
    async def echo(scope, receive, send):
        body = b""
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    d = make_driver(echo)
    await d.preface(body_settings)
    return d


# --- WINDOW_UPDATE validation ----------------------------------------------

async def test_window_update_zero_increment_stream_is_stream_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    await d.feed_and_settle(support.encode_window_update(1, 0))
    rst = [f for f in d.frames() if f.type == support.RST_STREAM and f.stream_id == 1]
    assert rst
    assert int.from_bytes(rst[-1].payload, "big") == support.PROTOCOL_ERROR


async def test_window_update_zero_increment_connection_is_protocol_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.encode_window_update(0, 0))
    assert _goaway_code(d) == support.PROTOCOL_ERROR


async def test_window_update_wrong_length_is_frame_size_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.encode_frame(support.WINDOW_UPDATE, 0, 0, b"\x00\x00"))
    assert _goaway_code(d) == support.FRAME_SIZE_ERROR


async def test_window_overflow_is_flow_control_error(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers(),
                                                        end_stream=False))
    # Push the stream window past 2^31-1.
    await d.feed_and_settle(support.encode_window_update(1, 0x7FFFFFFF))
    await d.feed_and_settle(support.encode_window_update(1, 0x7FFFFFFF))
    rst = [f for f in d.frames() if f.type == support.RST_STREAM and f.stream_id == 1]
    assert rst
    assert int.from_bytes(rst[-1].payload, "big") == support.FLOW_CONTROL_ERROR


# --- request-body flow control ---------------------------------------------

async def test_server_batches_window_update_after_consuming_body(make_driver):
    d = await _echo_request(make_driver)
    body = b"x" * (16 * 1024)
    headers = support.build_headers_frame(1, support.request_headers(
        method=b"POST", extra=[(b"content-length", str(len(body)).encode())]),
        end_stream=False)
    await d.feed_and_settle(headers)
    for offset in range(0, len(body), 4096):
        await d.feed_and_settle(support.encode_frame(
            support.DATA, 0, 1, body[offset:offset + 4096]))
    await d.feed_and_settle(support.encode_frame(
        support.DATA, support.FLAG_END_STREAM, 1, b""))
    wus = [f for f in d.frames() if f.type == support.WINDOW_UPDATE]
    assert wus, "consumed body credit should be replenished in a batch"
    assert len(wus) < len(body) // 4096


async def test_slow_application_withholds_receive_credit(make_driver):
    gate = asyncio.Event()
    consumed = asyncio.Event()

    async def slow(scope, receive, send):
        await gate.wait()
        while True:
            message = await receive()
            consumed.set()
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    d = make_driver(slow)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(
        1,
        support.request_headers(
            method=b"POST", extra=[(b"content-length", b"16384")]
        ),
        end_stream=False,
    ))
    for _ in range(4):
        d.feed(support.encode_frame(support.DATA, 0, 1, b"x" * 4096))
    await d.settle()
    assert not [frame for frame in d.frames() if frame.type == support.WINDOW_UPDATE]

    gate.set()
    await asyncio.wait_for(consumed.wait(), timeout=1)
    await d.settle()
    assert [frame for frame in d.frames() if frame.type == support.WINDOW_UPDATE]
    await d.feed_and_settle(support.encode_frame(
        support.DATA, support.FLAG_END_STREAM, 1, b""
    ))


async def test_data_exceeding_declared_content_length_is_protocol_error(make_driver):
    d = await _echo_request(make_driver)
    headers = support.build_headers_frame(1, support.request_headers(
        method=b"POST", extra=[(b"content-length", b"2")]), end_stream=False)
    await d.feed_and_settle(headers)
    await d.feed_and_settle(support.encode_frame(support.DATA,
                                                 support.FLAG_END_STREAM, 1, b"toolong"))
    frames = d.frames()
    rst = [f for f in frames if f.type == support.RST_STREAM and f.stream_id == 1]
    goaway = [f for f in frames if f.type == support.GOAWAY]
    assert rst or goaway


# --- MAX_CONCURRENT_STREAMS -------------------------------------------------

async def test_exceeding_max_concurrent_streams_is_refused(make_driver):
    from wreath.server import ServerConfig

    async def never(scope, receive, send):
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return

    config = ServerConfig(protocols=("h2",), max_concurrent_streams=1)
    d = make_driver(never, config=config)
    await d.preface()
    # Open one stream (kept open), then a second concurrently.
    await d.feed_and_settle(support.build_headers_frame(1, support.request_headers(),
                                                        end_stream=False))
    await d.feed_and_settle(support.build_headers_frame(3, support.request_headers(),
                                                        end_stream=False))
    frames = d.frames()
    refused = [f for f in frames if f.type == support.RST_STREAM and f.stream_id == 3]
    assert refused
    assert int.from_bytes(refused[-1].payload, "big") in (
        support.REFUSED_STREAM, support.PROTOCOL_ERROR)


async def test_advertises_max_concurrent_streams(make_driver):
    from wreath.server import ServerConfig

    d = make_driver(ok_app, config=ServerConfig(protocols=("h2",),
                                                max_concurrent_streams=42))
    d.connection_made()
    await d.settle()
    settings = [f for f in d.frames() if f.type == support.SETTINGS
                and not f.flags & support.FLAG_ACK]
    assert settings
    parsed = support.parse_settings(settings[0].payload)
    assert parsed.get(support.SETTINGS_MAX_CONCURRENT_STREAMS) == 42


# --- response backpressure -------------------------------------------------
#
# A blocked `await send()` must stay pending until every byte of *that* ASGI
# body message has been framed. Flow control plus one outstanding awaited body
# is what bounds response memory: the app cannot run ahead of the peer's window
# by staging an unbounded copy of the response.

async def _blocked_driver(make_driver, app, *, initial_window=0):
    """Open one stream whose peer send window is `initial_window`."""
    d = make_driver(app)
    await d.preface({support.SETTINGS_INITIAL_WINDOW_SIZE: initial_window})
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(path=b"/")))
    return d


def _data_frames(d, stream_id=1):
    return [f for f in d.frames() if f.type == support.DATA and f.stream_id == stream_id]


async def test_transport_pause_stops_data_framing_until_resume(make_driver):
    state = {"returned": False}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"p" * 4096})
        state["returned"] = True

    d = make_driver(app)
    await d.preface()
    d.frames()  # discard the server preface
    d.protocol.pause_writing()
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(path=b"/paused")
    ))
    assert state["returned"] is False
    assert _data_frames(d) == []

    d.protocol.resume_writing()
    await d.settle()
    assert state["returned"] is True
    frames = _data_frames(d)
    assert b"".join(frame.payload for frame in frames) == b"p" * 4096


async def test_connection_window_credit_is_shared_fairly(make_driver):
    body_size = 128 * 1024

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x" * body_size})

    d = make_driver(app)
    await d.preface()
    d.protocol.pause_writing()
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(path=b"/one")
    ))
    await d.feed_and_settle(support.build_headers_frame(
        3, support.request_headers(path=b"/three")
    ))
    # Release both streams into the initial connection window together. They
    # consume roughly half each and remain eligible when connection credit returns.
    d.protocol.resume_writing()
    await d.settle()
    d.frames()  # discard bytes sent under the initial connection window

    await d.feed_and_settle(support.encode_window_update(0, 32 * 1024))
    payload_by_stream = {1: 0, 3: 0}
    for frame in d.frames():
        if frame.type == support.DATA and frame.stream_id in payload_by_stream:
            payload_by_stream[frame.stream_id] += len(frame.payload)

    assert payload_by_stream[1] > 0, payload_by_stream
    assert payload_by_stream[3] > 0, payload_by_stream
    assert sum(payload_by_stream.values()) == 32 * 1024


async def test_metal_rfc9218_urgency_preempts_lower_urgency(make_driver):
    body_size = 48 * 1024

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x" * body_size})

    d = make_driver(app, metal_scheduler=True)
    await d.preface()
    d.protocol.pause_writing()
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(
            path=b"/low", extra=[(b"priority", b"u=7, i")]
        )
    ))
    await d.feed_and_settle(support.build_headers_frame(
        3, support.request_headers(
            path=b"/high", extra=[(b"priority", b"u=0, i")]
        )
    ))
    d.protocol.resume_writing()
    await d.settle()
    data = [frame for frame in d.frames() if frame.type == support.DATA]
    assert data
    assert data[0].stream_id == 3
    high = sum(len(frame.payload) for frame in data if frame.stream_id == 3)
    low = sum(len(frame.payload) for frame in data if frame.stream_id == 1)
    assert high == body_size
    assert high > low


async def test_metal_rfc9218_priority_update_reorders_queued_stream(make_driver):
    body_size = 40 * 1024

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"y" * body_size})

    d = make_driver(app, metal_scheduler=True)
    await d.preface()
    d.protocol.pause_writing()
    priority = [(b"priority", b"u=7, i")]
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(path=b"/one", extra=priority)
    ))
    await d.feed_and_settle(support.build_headers_frame(
        3, support.request_headers(path=b"/three", extra=priority)
    ))
    update = support.encode_frame(
        0x10, 0, 0, (3).to_bytes(4, "big") + b"u=0, i"
    )
    await d.feed_and_settle(update)
    d.protocol.resume_writing()
    await d.settle()
    data = [frame for frame in d.frames() if frame.type == support.DATA]
    assert data
    assert data[0].stream_id == 3


async def test_metal_rfc9218_incremental_peers_share_same_urgency(make_driver):
    body_size = 128 * 1024

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"i" * body_size})

    d = make_driver(app, metal_scheduler=True)
    await d.preface()
    d.protocol.pause_writing()
    priority = [(b"priority", b"u=1, i")]
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(path=b"/one", extra=priority)
    ))
    await d.feed_and_settle(support.build_headers_frame(
        3, support.request_headers(path=b"/three", extra=priority)
    ))
    d.protocol.resume_writing()
    await d.settle()
    payload_by_stream = {1: 0, 3: 0}
    for frame in d.frames():
        if frame.type == support.DATA and frame.stream_id in payload_by_stream:
            payload_by_stream[frame.stream_id] += len(frame.payload)
    assert payload_by_stream[1] > 0
    assert payload_by_stream[3] > 0


async def test_metal_rfc9218_nonincremental_response_stays_sequential(make_driver):
    body_size = 24 * 1024

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"z" * body_size})

    d = make_driver(app, metal_scheduler=True)
    await d.preface()
    d.protocol.pause_writing()
    priority = [(b"priority", b"u=0")]
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(path=b"/one", extra=priority)
    ))
    await d.feed_and_settle(support.build_headers_frame(
        3, support.request_headers(path=b"/three", extra=priority)
    ))
    d.protocol.resume_writing()
    await d.settle()
    stream_order = [
        frame.stream_id for frame in d.frames()
        if frame.type == support.DATA and frame.payload
    ]
    assert stream_order
    first_three = stream_order.index(3)
    assert all(stream_id == 1 for stream_id in stream_order[:first_three])


async def test_send_stays_pending_while_windows_are_zero(make_driver):
    state = {"returned": False}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x" * 4096})
        state["returned"] = True

    d = await _blocked_driver(make_driver, app)
    await d.settle()
    assert state["returned"] is False, "send() completed with a zero send window"


async def test_window_update_releases_send_only_when_message_fully_framed(make_driver):
    state = {"returned": False}
    size = 40_000

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"y" * size})
        state["returned"] = True

    d = await _blocked_driver(make_driver, app)
    # Partial credit: some bytes flow, but the message is not finished.
    await d.feed_and_settle(support.encode_window_update(1, 16_000))
    assert state["returned"] is False, "send() completed before the whole message"
    assert sum(len(f.payload) for f in _data_frames(d)) == 16_000

    # Enough credit for the remainder releases the await.
    await d.feed_and_settle(support.encode_window_update(1, size))
    await d.settle()
    assert state["returned"] is True


async def test_blocked_stream_does_not_block_another_stream(make_driver):
    """Multiplexing: a stream with credit progresses while another is blocked."""
    done = {"fast": False}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        if scope["path"] == "/slow":
            await send({"type": "http.response.body", "body": b"s" * 4096})
        else:
            await send({"type": "http.response.body", "body": b"f"})
            done["fast"] = True

    d = make_driver(app)
    # Stream 1 gets no credit; stream 3 is granted credit before it responds.
    await d.preface({support.SETTINGS_INITIAL_WINDOW_SIZE: 0})
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(path=b"/slow")))
    await d.feed_and_settle(support.build_headers_frame(
        3, support.request_headers(path=b"/fast")))
    await d.feed_and_settle(support.encode_window_update(3, 65_535))
    await d.settle()
    assert done["fast"] is True, "a blocked stream stalled an unrelated stream"
    assert sum(len(f.payload) for f in _data_frames(d, 3)) == 1


async def test_streaming_app_cannot_run_ahead_of_a_zero_window(make_driver):
    """A 64 MiB response as awaited 4 KiB chunks stops after one blocked chunk."""
    progress = {"chunks": 0}
    total, chunk = 64 * 1024 * 1024, 4096

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        payload = b"z" * chunk
        for i in range(total // chunk):
            await send({"type": "http.response.body", "body": payload,
                        "more_body": i < (total // chunk) - 1})
            progress["chunks"] += 1

    d = await _blocked_driver(make_driver, app)
    await d.settle()
    # The first chunk is pending; the app must not have constructed the rest.
    assert progress["chunks"] == 0
    assert sum(len(f.payload) for f in _data_frames(d)) == 0


async def test_end_stream_is_emitted_once_after_the_final_byte(make_driver):
    size = 40_000

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"y" * size})

    d = await _blocked_driver(make_driver, app)
    await d.feed_and_settle(support.encode_window_update(1, 16_000))
    ends = [f for f in _data_frames(d) if f.flags & support.FLAG_END_STREAM]
    assert ends == [], "END_STREAM emitted before the final pending byte"

    await d.feed_and_settle(support.encode_window_update(1, size))
    await d.settle()
    frames = _data_frames(d)
    assert sum(len(f.payload) for f in frames) == size - 16_000
    # Exactly one END_STREAM, and it is the last DATA frame.
    ends = [f for f in frames if f.flags & support.FLAG_END_STREAM]
    assert len(ends) == 1
    assert ends[0] is frames[-1]


async def test_empty_final_body_still_ends_the_stream(make_driver):
    """`body=b""` with more_body=False must carry END_STREAM on the wire."""
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hi", "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(support.build_headers_frame(
        1, support.request_headers(path=b"/")))
    await d.settle()
    frames = _data_frames(d)
    ends = [f for f in frames if f.flags & support.FLAG_END_STREAM]
    assert len(ends) == 1, f"expected exactly one END_STREAM, got {len(ends)}"
    assert b"".join(f.payload for f in frames) == b"hi"


async def test_reset_releases_a_blocked_send(make_driver):
    """RST_STREAM must unblock the app rather than strand it in send()."""
    released = {"done": False}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            await send({"type": "http.response.body", "body": b"x" * 4096})
        except BaseException:  # cancellation or an error both unblock  # noqa: BLE001
            pass
        released["done"] = True

    d = await _blocked_driver(make_driver, app)
    await d.settle()
    assert released["done"] is False
    await d.feed_and_settle(support.encode_rst_stream(1, support.CANCEL))
    await d.settle()
    assert released["done"] is True, "app left suspended in send() after RST_STREAM"


async def test_connection_loss_releases_a_blocked_send(make_driver):
    released = {"done": False}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            await send({"type": "http.response.body", "body": b"x" * 4096})
        except BaseException:  # noqa: BLE001
            pass
        released["done"] = True

    d = await _blocked_driver(make_driver, app)
    await d.settle()
    d.close()  # connection_lost
    await d.settle()
    assert released["done"] is True, "app left suspended in send() after connection loss"


async def test_second_body_while_one_is_pending_is_rejected(make_driver):
    """A conforming app cannot reach this; a defective one must not corrupt state."""
    seen = {"error": None}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        import asyncio
        first = asyncio.ensure_future(
            send({"type": "http.response.body", "body": b"x" * 4096,
                  "more_body": True}))
        await asyncio.sleep(0)
        try:
            await send({"type": "http.response.body", "body": b"second"})
        except RuntimeError as exc:
            seen["error"] = str(exc)
        first.cancel()

    d = await _blocked_driver(make_driver, app)
    await d.settle()
    assert seen["error"] == "HTTP/2 send already pending"
