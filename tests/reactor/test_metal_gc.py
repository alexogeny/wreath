"""Metal's loop-driven cycle collector.

CPython triggers collection off an allocation counter, so on a request-serving
loop it fires wherever the Nth container allocation happens to land -- inside a
request batch. That is invisible in a throughput average and is most of what a
p99 is made of. Metal takes the heap over: it freezes what startup built, raises
the automatic trigger so it stops firing mid-batch, and runs the collector in the
loop's own idle gaps.

Each of the three is separately switchable, because each is separately
measurable: `gc_mode="stock"` gives the heap back entirely, `gc_freeze=False`
keeps the startup heap traceable. These tests pin the contracts that make an
ablation mean anything -- above all the two that pull in opposite directions:
a loop with slack **must** collect in it, and a saturated loop **must not**.
"""
from __future__ import annotations

import asyncio
import gc
import selectors
import socket
import threading
import time
import weakref

import pytest

import wreath.reactor as reactor
from wreath.server import Server, ServerConfig


def _metal_loop(**kwargs):
    return reactor.metal_event_loop(diagnostics=True, **kwargs)


def _stock_metal_loop():
    return reactor.metal_event_loop(diagnostics=True, gc_mode="stock")


def test_a_gc_mode_other_than_stock_or_idle_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WREATH_METAL_GC", "eventually")

    with pytest.raises(ValueError, match="WREATH_METAL_GC must be 'stock' or 'idle'"):
        reactor.metal_event_loop()


class _Harness:
    """A real metal server, driven from a thread so the loop can actually idle.

    Driving the client from the loop under test would keep it busy for the whole
    run, and "the loop was idle" is the exact condition these tests are about.
    """

    def __init__(self, loop, *, body: bytes = b"ok"):
        self.loop = loop
        self.body = body
        self.server = None
        self.port = 0

    def __enter__(self):
        async def app(scope, receive, send):
            # A cycle per request: garbage that only the collector can reclaim,
            # so a run that never collects is observably different from one that
            # does.
            cycle: dict[str, object] = {}
            cycle["self"] = cycle
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": self.body})

        asyncio.set_event_loop(self.loop)
        self.server = Server(
            app, ServerConfig(host="127.0.0.1", port=0, lifespan="off"),
            self.loop)
        self.loop.run_until_complete(self.server._start(ssl=None))
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc):
        self.loop.run_until_complete(self.server.close())
        self.loop.close()
        asyncio.set_event_loop(None)
        return False

    def drive(self, requests: int, gap: float) -> None:
        """Serve `requests` keep-alive requests with `gap` seconds between them."""
        done = self.loop.create_future()
        body = self.body

        def client():
            sock = socket.create_connection(("127.0.0.1", self.port))
            try:
                for _ in range(requests):
                    sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                    buf = b""
                    while not buf.endswith(body):
                        chunk = sock.recv(65536)
                        if not chunk:
                            raise AssertionError("server closed mid-run")
                        buf += chunk
                    if gap:
                        time.sleep(gap)
            finally:
                sock.close()
                self.loop.call_soon_threadsafe(done.set_result, None)

        threading.Thread(target=client, daemon=True).start()
        self.loop.run_until_complete(done)


# --- policy installation ---------------------------------------------------

def test_metal_owns_its_collector_by_default() -> None:
    stock_threshold = gc.get_threshold()
    loop = _metal_loop()
    try:
        assert loop._poller.gc_loop_driven is True
        assert gc.get_threshold()[0] > stock_threshold[0]
        # Only the gen-0 trigger moves; the promotion ratios are CPython's.
        assert gc.get_threshold()[1:] == stock_threshold[1:]
    finally:
        loop.close()
    assert gc.get_threshold() == stock_threshold


def test_stock_gc_mode_leaves_cpython_in_charge() -> None:
    stock_threshold = gc.get_threshold()
    loop = _stock_metal_loop()
    try:
        assert loop._poller.gc_loop_driven is False
        assert gc.get_threshold() == stock_threshold
        assert loop.gc_stats() == {}
        assert loop.freeze_heap() == 0
    finally:
        loop.close()


def test_idle_gc_mode_requires_the_native_run_loop() -> None:
    # The idle gap belongs to the C poller: only it knows when it is about to
    # wait, and only it holds the arrival estimate that separates idle from
    # saturated. A policy with no hook to run in is a configuration error, not a
    # silent no-op.
    with pytest.raises(ValueError, match="native_loop"):
        reactor.EventLoop(
            selectors.EpollSelector(), backend="epoll", timers="wheel",
            native_loop=False, gc_mode="idle")


def test_unknown_gc_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="gc_mode"):
        reactor.EventLoop(
            selectors.EpollSelector(), backend="epoll", timers="wheel",
            native_loop=True, gc_mode="deferred")


def test_closing_the_loop_restores_the_heap_policy() -> None:
    stock_threshold = gc.get_threshold()
    loop = _metal_loop()
    loop.freeze_heap()
    assert gc.get_freeze_count() > 0
    loop.close()
    # A process outliving this loop -- a test suite, an embedding host -- gets
    # its collector back exactly as it lent it.
    assert gc.get_threshold() == stock_threshold
    assert gc.get_freeze_count() == 0


# --- freezing --------------------------------------------------------------

def test_startup_freezes_what_the_server_built() -> None:
    assert gc.get_freeze_count() == 0
    loop = _metal_loop()
    with _Harness(loop):
        # Server._start freezes as its last atomic step: modules imported, route
        # table compiled, lifespan run, listeners bound.
        assert gc.get_freeze_count() > 0
        assert loop.gc_stats()["frozen_objects"] > 0
    assert gc.get_freeze_count() == 0


def test_freezing_is_ablatable_without_touching_the_idle_policy() -> None:
    loop = reactor.metal_event_loop(diagnostics=True)
    loop._collector._freeze_enabled = False
    with _Harness(loop):
        assert gc.get_freeze_count() == 0
        # The other half of the policy is untouched: still loop-driven.
        assert loop._poller.gc_loop_driven is True


def test_freeze_collects_before_it_freezes() -> None:
    # Freezing garbage would retain it for the life of the process, so the
    # collection is not an optimization -- it is what keeps freeze() from being
    # a leak.
    class Node:  # a dict cannot be weak-referenced; the cycle still can be
        def __init__(self) -> None:
            self.self_ = self

    loop = _metal_loop()
    try:
        node = Node()
        witness = weakref.ref(node)
        del node
        loop.freeze_heap()
        assert witness() is None
    finally:
        loop.close()


# --- the two halves of the idle contract -----------------------------------

def test_a_loop_with_slack_collects_in_it() -> None:
    loop = _metal_loop()
    with _Harness(loop) as harness:
        # 0.5ms passed alone but was not enough slack beside five xdist peers;
        # 1ms stays below the slow-test tail and keeps this a load-stable proof.
        harness.drive(requests=200, gap=0.001)
        stats = loop.gc_stats()
    collections = (stats["idle_young_collections"]
                   + stats["idle_full_collections"])
    assert collections > 0, "a loop with 1ms of slack per request never collected"
    assert stats["idle_collect_nanoseconds"] > 0


def test_a_saturated_loop_does_not_collect_in_the_batch() -> None:
    # The inverse contract, and the one that a naive "collect when block_ms is
    # large" gate would break: under saturation the loop still computes a
    # multi-second keep-alive deadline and then returns from the enter
    # immediately. Gating on the arrival estimator instead means a loop with no
    # slack spends none, and defers to the raised automatic threshold.
    loop = _metal_loop()
    with _Harness(loop) as harness:
        harness.drive(requests=4000, gap=0.0)
        stats = loop.gc_stats()
    collections = (stats["idle_young_collections"]
                   + stats["idle_full_collections"])
    assert collections <= 2, (
        f"a saturated loop spent {collections} collections it had no slack for")


def test_every_collection_under_load_is_one_the_loop_chose() -> None:
    """The point of the whole policy, stated as one assertion.

    With slack available, collection should be something the loop *decides* to
    do in a gap -- not something the allocator does to it mid-request. Counting
    every collection the process runs and subtracting the ones the poller ran
    leaves the automatic ones, and those are the ones that land on a request.
    """
    automatic: list[int] = []

    def watch(phase, info):
        if phase == "start":
            automatic.append(info["generation"])

    loop = _metal_loop()
    with _Harness(loop) as harness:
        harness.drive(requests=50, gap=0.0005)  # warm the arrival estimator
        gc.callbacks.append(watch)
        try:
            harness.drive(requests=400, gap=0.0005)
        finally:
            gc.callbacks.remove(watch)
        stats = loop.gc_stats()

    loop_driven = (stats["idle_young_collections"]
                   + stats["idle_full_collections"])
    assert loop_driven > 0
    # `watch` sees loop-driven and automatic collections alike, so anything left
    # over after subtracting the poller's own count fired on the request path.
    assert len(automatic) - loop_driven <= 1, (
        f"{len(automatic) - loop_driven} collections fired outside an idle gap")


def test_the_collection_floor_bounds_idle_churn() -> None:
    # One request wakes the loop several times -- receive completion, send
    # completion, ready-queue turn -- and each is honestly "work ran". Without a
    # floor a lightly loaded server re-collects a young generation it just
    # emptied, several times per request.
    loop = reactor.metal_event_loop(diagnostics=True)
    loop._collector._min_interval_seconds = 0.050
    with _Harness(loop) as harness:
        loop._collector.attach(loop._poller)  # re-install with the new floor
        started = time.monotonic()
        harness.drive(requests=100, gap=0.005)
        elapsed = time.monotonic() - started
        stats = loop.gc_stats()
    collections = (stats["idle_young_collections"]
                   + stats["idle_full_collections"])
    ceiling = int(elapsed / 0.050) + 2
    assert collections <= ceiling, (
        f"{collections} collections in {elapsed:.3f}s exceeds the 50ms floor")


# --- what a collection must never reap -------------------------------------

class _Echo(asyncio.Protocol):
    """A plain asyncio protocol: no wreath `Server` holding it up."""

    def connection_made(self, transport) -> None:
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        self.transport.write(b"pong")


def test_a_collection_does_not_reap_a_live_connection() -> None:
    """The ownership contract the idle policy depends on, stated directly.

    A stock asyncio loop keeps an accepted transport alive through the bound
    `_read_ready` it stored in `_add_reader`. Metal drives ingress from an
    io_uring multishot receive and registers no reader, so the poller's
    connection slab is the only thing that can hold it -- and while that slab
    held a borrowed pointer, an accepted connection was a transport<->protocol
    cycle reachable from no root. Any collection reaped it out from under a live
    socket, and the peer waited forever.

    Wreath's own `Server` tracks its protocols, so the HTTP path happened to be
    safe and hid this. Everything else on the loop -- `wreath.postgres`,
    `wreath.http_client`, any third-party `loop.create_server` -- was not, which
    is what made the `e2e` benchmark scenario hang: its in-process upstreams are
    exactly this shape.
    """
    async def main() -> None:
        loop = asyncio.get_running_loop()
        server = await loop.create_server(_Echo, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        writer.write(b"ping")
        await writer.drain()
        assert await reader.read(4) == b"pong"
        await asyncio.sleep(0.05)

        accepted = [o for o in gc.get_objects() if isinstance(o, _Echo)]
        assert accepted, "no accepted protocol to watch"
        witness = weakref.ref(accepted[0])
        del accepted

        gc.collect()
        assert witness() is not None, "the poller let go of a live connection"

        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(4), 5.0) == b"pong"

        writer.close()
        server.close()
        await server.wait_closed()

    # `gc_mode="stock"` on purpose: this is not a property of the idle policy,
    # it is the ownership rule the policy merely made it impossible to ignore.
    asyncio.run(main(),
                loop_factory=lambda: reactor.metal_event_loop(gc_mode="stock"))


def test_a_closed_connection_is_given_back() -> None:
    """The other side of the contract: an owning poller must not pin the dead.

    A reference the poller never released would turn every finished connection
    into a leak -- the failure this fix could plausibly trade for.
    """
    async def main() -> weakref.ref:
        loop = asyncio.get_running_loop()
        server = await loop.create_server(_Echo, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping")
        await writer.drain()
        assert await reader.read(4) == b"pong"
        await asyncio.sleep(0.05)

        accepted = [o for o in gc.get_objects() if isinstance(o, _Echo)]
        witness = weakref.ref(accepted[0])
        del accepted

        writer.close()
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0.1)  # let connection_lost run
        return witness

    witness = asyncio.run(
        main(), loop_factory=lambda: reactor.metal_event_loop(gc_mode="stock"))
    gc.collect()
    assert witness() is None, "the poller kept a closed connection alive"


def test_the_poller_releases_connections_it_still_owns_at_close() -> None:
    """A loop closed with connections still up must not leak them.

    The owning reference has to be given back by `close()` rather than waiting
    for the poller object itself to be collected -- otherwise every closed loop
    strands its connections, and a process that builds loops in a row (a test
    suite, a worker respawn) accumulates them.
    """
    loop = reactor.metal_event_loop(gc_mode="stock")
    asyncio.set_event_loop(loop)

    async def setup():
        server = await loop.create_server(_Echo, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping")
        await writer.drain()
        assert await reader.read(4) == b"pong"
        await asyncio.sleep(0.05)
        accepted = [o for o in gc.get_objects() if isinstance(o, _Echo)]
        assert accepted
        witness = weakref.ref(accepted[0])
        # Returned, not closed: the loop goes down with the connection still up.
        return witness, server, writer

    witness, server, writer = loop.run_until_complete(setup())
    loop.close()
    del server, writer
    asyncio.set_event_loop(None)
    gc.collect()
    assert witness() is None, "a closed poller still pinned its connections"


# --- the C boundary --------------------------------------------------------

def test_collector_installation_validates_its_arguments() -> None:
    loop = _metal_loop()
    poller = loop._poller
    try:
        with pytest.raises(TypeError, match="callable"):
            poller._set_gc_collector(object(), 1e-4, 1e-2, 0.0)
        with pytest.raises(ValueError, match="idle threshold"):
            poller._set_gc_collector(gc.collect, 0.0, 1e-2, 0.0)
        with pytest.raises(ValueError, match="idle threshold"):
            # A full-collection threshold below the young one would make every
            # idle gap a full collection; that is a typo, not a policy.
            poller._set_gc_collector(gc.collect, 1e-2, 1e-4, 0.0)
        with pytest.raises(ValueError, match="interval"):
            poller._set_gc_collector(gc.collect, 1e-4, 1e-2, -1.0)
    finally:
        loop.close()


def test_the_collector_can_be_handed_back() -> None:
    loop = _metal_loop()
    poller = loop._poller
    try:
        assert poller.gc_loop_driven is True
        poller._set_gc_collector(None, 1.0, 1.0, 0.0)
        assert poller.gc_loop_driven is False
    finally:
        loop.close()


def test_a_closed_poller_drives_no_collector() -> None:
    loop = _metal_loop()
    poller = loop._poller
    loop.close()
    assert poller.gc_loop_driven is False
    # The counters survive close: a caller inspecting the run it just finished
    # is the main consumer of them.
    assert poller.gc_young_collections >= 0
