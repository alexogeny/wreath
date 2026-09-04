from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wreath.server import ServerConfig

from . import support
from .conftest import ok_app, requires_h2

pytestmark = [requires_h2, pytest.mark.asyncio]

_NATIVE = Path(__file__).parents[2] / "src" / "wreath" / "_native"


def _c_function(source: str, name: str, next_name: str) -> str:
    """Slice a C function body out of a source file (see test_cpu_pressure_red)."""
    start = source.index(f"\n{name}(")
    end = source.index(f"\n{next_name}(", start)
    return source[start:end]


def _goaways(d):
    return [f for f in d.frames() if f.type == support.GOAWAY]


async def _open_stream(d, sid=1, *, end_stream=False):
    d.feed(support.build_headers_frame(sid, support.request_headers(), end_stream=end_stream))
    await d.settle()


async def test_empty_data_frames_are_charged_no_flow_control(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await _open_stream(d, 1, end_stream=False)

    frame = support.encode_frame(support.DATA, 0x0, 1, b"")
    flood = frame * 20_000
    d.feed(flood)
    await d.settle()

    # The unproductive-frame budget must trip and GOAWAY before 20k zero-cost
    # frames are processed; if it regresses, no GOAWAY is emitted.
    assert _goaways(d), (
        "no GOAWAY after 20k zero-cost empty DATA frames: connection has no "
        "no-progress / frame-flood budget (F1)"
    )


async def test_empty_data_flood_amplifies_into_asgi_receive(make_driver):
    wakes = {"n": 0}

    async def counting_app(scope, receive, send):
        while True:
            msg = await receive()
            wakes["n"] += 1
            if msg["type"] == "http.disconnect" or not msg.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    d = make_driver(counting_app)
    await d.preface()
    await _open_stream(d, 1, end_stream=False)

    n = 200
    frame = support.encode_frame(support.DATA, 0x0, 1, b"")
    for _ in range(n):
        d.feed(frame)
        await d.settle()

    # Without a cap this is 1:1 (one ASGI wakeup per 9-byte frame); the budget
    # must cut the connection off before all n frames drive wakeups.
    assert wakes["n"] < n, (
        f"{wakes['n']} ASGI wakeups from {n} empty 9-byte DATA frames: "
        "1:1 amplification with no cap (F1)"
    )


async def test_nonempty_data_frames_share_the_body_chunk_budget(make_driver):
    config = ServerConfig(protocols=("h2",), max_body_chunks=4)
    d = make_driver(ok_app, config)
    await d.preface()
    await _open_stream(d, 1, end_stream=False)
    d.frames()  # discard handshake output

    for _ in range(config.max_body_chunks + 1):
        d.feed(support.encode_frame(support.DATA, 0, 1, b"x"))
        await d.settle()

    resets = [
        frame for frame in d.frames() if frame.type == support.RST_STREAM and frame.stream_id == 1
    ]
    assert resets, "positive DATA frames bypass max_body_chunks"
    assert int.from_bytes(resets[-1].payload, "big") == support.ENHANCE_YOUR_CALM


async def test_data_flood_on_completed_stream_bounds_reset_reflection(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    await _open_stream(d, 1, end_stream=True)
    d.frames()

    frame_count = 1_000
    d.feed(support.encode_frame(support.DATA, 0, 1, b"late") * frame_count)
    await d.settle()

    frames = d.frames()
    resets = [
        frame for frame in frames if frame.type == support.RST_STREAM and frame.stream_id == 1
    ]
    goaways = [frame for frame in frames if frame.type == support.GOAWAY]
    assert goaways, (
        f"closed-stream DATA did not consume the no-progress budget ({len(resets)} resets)"
    )
    assert len(resets) < frame_count, "closed-stream DATA produced one RST_STREAM per frame"


async def test_goaway_flood_consumes_the_no_progress_budget(make_driver):
    d = make_driver(ok_app)
    await d.preface()
    d.frames()

    frame = support.encode_frame(support.GOAWAY, 0, 0, b"\x00" * 8)
    d.feed(frame * 1_000)
    await d.settle()

    goaways = [item for item in d.frames() if item.type == support.GOAWAY]
    assert goaways, "GOAWAY frames bypassed the no-progress budget"
    assert support.parse_goaway(goaways[-1].payload)[1] == support.ENHANCE_YOUR_CALM


async def test_ping_flood_is_throttled(make_driver):
    d = make_driver(ok_app)
    await d.preface()

    ping = support.encode_ping(b"\x00" * 8)
    d.feed(ping * 10_000)
    await d.settle()

    acks = [f for f in d.frames() if f.type == support.PING and (f.flags & support.FLAG_ACK)]
    # A hardened server bounds looped control frames: it must either stop
    # reflecting ACKs before 10k or GOAWAY. Unbounded 1:1 reflection regresses.
    assert len(acks) < 10_000 or _goaways(d), (
        f"{len(acks)} PING ACKs emitted for 10k PINGs with no GOAWAY: "
        "unthrottled control-frame reflection (F2)"
    )


async def test_settings_flood_is_throttled(make_driver):
    d = make_driver(ok_app)
    await d.preface()

    settings = support.encode_settings({})
    d.feed(settings * 10_000)
    await d.settle()

    acks = [f for f in d.frames() if f.type == support.SETTINGS and (f.flags & support.FLAG_ACK)]
    # As with PING: bounded reflection or GOAWAY; 1:1 for 10k frames regresses.
    assert len(acks) < 10_000 or _goaways(d), (
        f"{len(acks)} SETTINGS ACKs for 10k empty SETTINGS with no GOAWAY: "
        "unthrottled control-frame reflection (F3)"
    )


async def test_unknown_extension_frame_flood_is_throttled(make_driver):
    d = make_driver(ok_app)
    await d.preface()

    unknown = support.encode_frame(0xFA, 0, 0, b"")
    d.feed(unknown * 10_000)
    await d.settle()

    assert _goaways(d), (
        "no GOAWAY after 10k unknown nine-byte frames: extension frames "
        "bypass the no-progress budget (F6)"
    )


@pytest.mark.parametrize(
    "ack",
    [
        support.encode_frame(support.SETTINGS, support.FLAG_ACK, 0, b""),
        support.encode_frame(support.PING, support.FLAG_ACK, 0, b"ackflood"),
    ],
    ids=("settings", "ping"),
)
async def test_ack_only_frame_flood_is_throttled(make_driver, ack):
    d = make_driver(ok_app)
    await d.preface()

    d.feed(ack * 10_000)
    await d.settle()

    assert _goaways(d), (
        "no GOAWAY after 10k ACK-only frames: ignored ACKs bypass the no-progress budget (F7)"
    )


async def test_reset_stream_cancels_inflight_handler(make_driver):
    started = {"n": 0}
    finished = {"n": 0}
    cancelled = {"n": 0}
    gate = asyncio.Event()

    async def slow_app(scope, receive, send):
        started["n"] += 1
        try:
            await gate.wait()
            finished["n"] += 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        except asyncio.CancelledError:
            cancelled["n"] += 1
            raise

    d = make_driver(slow_app)
    await d.preface()

    d.feed(support.build_headers_frame(1, support.request_headers(), end_stream=True))
    await d.settle()
    d.feed(support.encode_rst_stream(1, 0x8))  # CANCEL
    await d.settle()

    # Let the (supposedly cancelled) handler proceed.
    gate.set()
    await d.settle()

    # A defended server cancels an off-stream handler on reset so it never
    # finishes its (now-pointless) work; regressing lets it run to completion.
    assert cancelled["n"] == 1 and finished["n"] == 0, (
        f"handler ran to completion after RST_STREAM "
        f"(started={started['n']} finished={finished['n']} "
        f"cancelled={cancelled['n']}): reset does not reclaim CPU (F4)"
    )


async def test_connection_window_update_flood_is_throttled(make_driver):
    async def idle_app(scope, receive, send):
        # Never completes: keeps its stream in the open-stream dict so each
        # WINDOW_UPDATE has streams to walk.
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return

    d = make_driver(idle_app)
    await d.preface()
    for sid in range(1, 41, 2):  # 20 open streams, under max_concurrent
        d.feed(support.build_headers_frame(sid, support.request_headers(), end_stream=False))
    await d.settle()

    flood = b"".join(support.encode_window_update(0, 1) for _ in range(10_000))
    d.feed(flood)
    await d.settle()

    # The budget must trip on this vector too; regressing leaves it unthrottled.
    assert _goaways(d), (
        "no GOAWAY after 10k connection-level WINDOW_UPDATE frames: this "
        "control-frame vector has no frame-flood budget (F5)"
    )


async def test_connection_window_update_flush_walk_is_bounded() -> None:
    source = (_NATIVE / "server_http2.c").read_text()
    fn = _c_function(source, "process_window_update", "process_rst_stream")
    assert "PyDict_Next(self->streams" not in fn, (
        "process_window_update walks the entire open-stream dict on every "
        "connection-level WINDOW_UPDATE: O(open_streams) work per 13-byte "
        "frame with no bound on frame rate (F5)"
    )
