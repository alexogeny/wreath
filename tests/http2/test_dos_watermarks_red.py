"""Regression guards for HTTP/2 resource-exhaustion high-watermarks.

These began as RED proofs: each asserts the *hardened* behaviour a defended
HTTP/2 server should exhibit, and every one failed against the pre-hardening
implementation. They now pass -- the mitigations (a per-connection unproductive
frame budget, a bounded connection-window flush, and CPU reclamation on reset)
have landed -- so they stand as regression guards: removing a defence turns the
matching test red again. None of them assert wall-clock time; they assert
deterministic protocol-level facts (frames on the wire, ASGI wakeups,
flow-control accounting), so they belong in correctness CI.

Findings guarded here (see the accompanying red-team report):

  F1  Empty DATA-frame flood (CVE-2019-9518 class): 0-length DATA frames cost
      zero flow-control credit yet drive one ASGI receive() wakeup each. There
      is no per-connection budget on "no-progress" frames, so one connection can
      pin a CPU indefinitely with 9-byte frames.
  F2  PING flood (CVE-2019-9512 class): every PING is ACKed 1:1 with no rate cap.
  F3  SETTINGS flood (CVE-2019-9515 class): every empty SETTINGS is ACKed 1:1
      with no rate cap.
  F4  Reset does not cancel in-flight work (CVE-2019-9514 / rapid-reset residual):
      RST_STREAM abandons the stream but the dispatched ASGI task runs to
      completion. The concurrency cap holds (good), but the server performs the
      full, expensive handler work for a request the client has already thrown
      away -- up to max_concurrent wasted executions in flight, refilled as each
      completes.
  F5  Connection-level WINDOW_UPDATE flood: a distinct unthrottled control-frame
      vector (not covered by F1-F3), and worse -- every sid=0 WINDOW_UPDATE walks
      the *entire* open-stream dict to look for streams to unblock, even when the
      connection window was never the binding constraint and nothing can flush.
      That is O(open_streams) of wasted work per 13-byte frame, on top of having
      no frame budget at all.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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
    d.feed(support.build_headers_frame(sid, support.request_headers(),
                                       end_stream=end_stream))
    await d.settle()


# --- F1: empty DATA flood ---------------------------------------------------

async def test_empty_data_frames_are_charged_no_flow_control(make_driver):
    """Thesis: 0-length DATA frames consume no receive-window credit.

    An attacker can therefore send unlimited empty DATA frames on one open
    stream without ever exhausting the flow-control window that is supposed to
    bound peer send volume. We prove it by flooding empty DATA frames and
    showing the server never emits a WINDOW_UPDATE (nothing was consumed) and
    never errors the connection.

    A defended server either charges a minimum cost per frame or trips a
    no-progress budget; this asserts that defence fired (GOAWAY emitted).
    """
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
    """Thesis: each empty DATA frame forces one full ASGI receive() round-trip.

    9 wire bytes -> one future resolution + one message dict, unbounded. We feed
    frames interleaved with loop turns so the handler re-awaits between them.
    """
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


# --- F2: PING flood ---------------------------------------------------------

async def test_ping_flood_is_throttled(make_driver):
    """Thesis: PINGs are ACKed 1:1 with no rate limit (CVE-2019-9512 class)."""
    d = make_driver(ok_app)
    await d.preface()

    ping = support.encode_ping(b"\x00" * 8)
    d.feed(ping * 10_000)
    await d.settle()

    acks = [f for f in d.frames()
            if f.type == support.PING and (f.flags & support.FLAG_ACK)]
    # A hardened server bounds looped control frames: it must either stop
    # reflecting ACKs before 10k or GOAWAY. Unbounded 1:1 reflection regresses.
    assert len(acks) < 10_000 or _goaways(d), (
        f"{len(acks)} PING ACKs emitted for 10k PINGs with no GOAWAY: "
        "unthrottled control-frame reflection (F2)"
    )


# --- F3: SETTINGS flood -----------------------------------------------------

async def test_settings_flood_is_throttled(make_driver):
    """Thesis: empty SETTINGS frames are ACKed 1:1 with no cap (CVE-2019-9515)."""
    d = make_driver(ok_app)
    await d.preface()

    settings = support.encode_settings({})
    d.feed(settings * 10_000)
    await d.settle()

    acks = [f for f in d.frames()
            if f.type == support.SETTINGS and (f.flags & support.FLAG_ACK)]
    # As with PING: bounded reflection or GOAWAY; 1:1 for 10k frames regresses.
    assert len(acks) < 10_000 or _goaways(d), (
        f"{len(acks)} SETTINGS ACKs for 10k empty SETTINGS with no GOAWAY: "
        "unthrottled control-frame reflection (F3)"
    )


# --- F4: reset does not cancel in-flight work -------------------------------

async def test_reset_stream_cancels_inflight_handler(make_driver):
    """Thesis: RST_STREAM should stop the server doing work for an abandoned
    request. Today the dispatched ASGI task runs to completion after the reset,
    so a client can make the server execute an arbitrarily expensive handler and
    then throw the result away for free.
    """
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


# --- F5: connection-level WINDOW_UPDATE flood -------------------------------

async def test_connection_window_update_flood_is_throttled(make_driver):
    """Thesis: sid=0 WINDOW_UPDATE frames are a distinct unthrottled vector.

    They are not DATA, PING, or SETTINGS, so F1-F3 do not cover them. We hold
    several streams open (so the per-frame flush walk is non-trivial) and flood
    tiny +1 connection-window increments; the server processes all of them and
    never trips a frame budget.
    """
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
        d.feed(support.build_headers_frame(sid, support.request_headers(),
                                           end_stream=False))
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
    """Thesis: each sid=0 WINDOW_UPDATE walks the whole open-stream dict.

    `process_window_update` unconditionally iterates `self->streams` with
    PyDict_Next on the connection-window path, so cost is O(open_streams) per
    frame regardless of whether the connection window was ever the binding
    constraint. A hardened design tracks the set of streams actually blocked on
    the connection window (or skips the walk when the window was not the
    constraint), making the flush O(blocked), not O(all streams).

    This is a hot-path structural proof in the same idiom as
    test_cpu_pressure_red.py; it fails if the full-dict walk is reintroduced.
    """
    source = (_NATIVE / "server_http2.c").read_text()
    fn = _c_function(source, "process_window_update", "process_rst_stream")
    assert "PyDict_Next(self->streams" not in fn, (
        "process_window_update walks the entire open-stream dict on every "
        "connection-level WINDOW_UPDATE: O(open_streams) work per 13-byte "
        "frame with no bound on frame rate (F5)"
    )
