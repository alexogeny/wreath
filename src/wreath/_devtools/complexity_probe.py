"""Prove or refute an algorithmic-complexity hypothesis with scaling ratios.

The native lints read C source and the profiler samples a process; neither can
answer *"is this operation O(n) or O(n**2)?"*. This tool does it empirically:
run an isolated operation at doubling sizes, time it, and read the growth
exponent off the log-log slope. A quadratic doubles into ~4x per step, a
linear into ~2x, a constant into ~1x -- no full benchmark rig required.

    uv run wreath-complexity-probe                    # run every registered probe
    uv run wreath-complexity-probe wheel-fire-batch   # one probe by name
    uv run wreath-complexity-probe --list
    uv run wreath-complexity-probe --sizes 1000,2000,4000,8000
    uv run wreath-complexity-probe --format json
    uv run wreath-complexity-probe --group metal-http1 --check

Registered probes pin named assumptions: the independently scaled axis, the
request stage it belongs to, and the maximum exponent permitted. Both the
global fit and the largest-three-size tail fit are checked, so a late threshold
cliff cannot hide in earlier points. Work that is faster than the upper bound is
not a regression. A timed result below its declared resolution is UNRESOLVED,
never silently successful.

Adding a probe: decorate a callable taking a size and returning either the
elapsed-seconds float for that size, or a (seconds, {counter: value}) pair.
Counters are deterministic work measures; a declared metric must be returned at
every size. Tiny operations must be batched inside the probe so the returned
elapsed time clears the resolution floor.

Timings run with GC disabled, best-of-`repeats` per size, warmed up at a small
size first. Sizes double so global, tail, and adjacent exponents can settle.
`--update-baseline` records the reviewed assumptions and observations;
`--check` rejects assumption drift and reruns the proof. This is a shape check,
not a throughput benchmark.
"""

from __future__ import annotations

import argparse
import gc
import importlib
import json
import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .native_lint import repo_root

ProbeFn = Callable[[int], "float | tuple[float, dict[str, int]]"]

#: Checked-in complexity assumptions for the request-hot probes.
BASELINE_PATH = Path("docs/agents/complexity-baseline.json")
BASELINE_VERSION = 1

#: exponent -> printable class, in fit order. Past cubic the names exist so a
#: scan can *report* what it measured; nothing in this tree is expected to be
#: quartic or worse, and a probe declaring one should be read as a defect.
_CLASSES = (
    (0.0, "O(1)"), (1.0, "O(n)"), (2.0, "O(n^2)"), (3.0, "O(n^3)"),
    (4.0, "O(n^4)"), (5.0, "O(n^5)"), (6.0, "O(n^6)"),
)

#: Plain-English degree names, so a report can say "quartic" rather than making
#: the reader count carets.
_DEGREE_NAMES = {
    0.0: "constant", 1.0: "linear", 2.0: "quadratic", 3.0: "cubic",
    4.0: "quartic", 5.0: "quintic", 6.0: "sextic",
}


def degree_name(exponent: float) -> str:
    """The nearest plain-English degree name for a fitted exponent."""
    nearest = min(_DEGREE_NAMES, key=lambda d: abs(d - exponent))
    return _DEGREE_NAMES[nearest]


@dataclass(frozen=True)
class Todo:
    """A probe that records a **defect** rather than a contract.

    An ordinary probe declares an upper bound and is content with anything
    faster. That is the right rule for a contract and the wrong one for a known
    defect: it would let the defect be fixed without anybody noticing, leaving a
    stale claim in the file that reads as though it were still true.

    So a marked probe is checked from *both* sides. Growing past
    ``degree + tolerance`` is a regression, exactly as before. Falling below
    ``degree - tolerance`` means the subject improved and this mark is now a
    lie -- the run goes red and asks for the mark to be retargeted or deleted.
    That second rule is the whole point: without it a mark decays into
    permission, which is how a backlog entry outlives the bug it describes.

    ``target`` is the degree the fix should reach; it is documentation, not an
    assertion. ``owner`` names the task or design that carries the fix, so the
    mark cannot become an orphan nobody recognises.
    """

    degree: float
    target: float
    reason: str
    owner: str

    def __post_init__(self) -> None:
        if self.target >= self.degree:
            raise ValueError(
                f"a fix-later mark must aim below what it records: "
                f"target n^{self.target} is not better than degree n^{self.degree}"
            )
        if not self.reason.strip() or not self.owner.strip():
            raise ValueError("a fix-later mark needs both a reason and an owner")

    def explain(self) -> str:
        return (
            f"FIX LATER: {degree_name(self.degree)} today (n^{self.degree:g}), "
            f"target {degree_name(self.target)} (n^{self.target:g}) -- "
            f"{self.reason} [{self.owner}]"
        )


@dataclass
class Probe:
    name: str
    fn: ProbeFn
    expect: float          # maximum expected growth exponent
    sizes: tuple[int, ...]
    doc: str = ""
    repeats: int = 3
    #: Growth above expect+tolerance fails. Faster growth is an improvement or
    #: fixed-cost domination, not a complexity regression -- unless `todo` is
    #: set, which makes the bound two-sided. See `Todo`.
    tolerance: float = 0.5
    #: Timed probes whose largest sample does not clear this are UNRESOLVED,
    #: never silently successful. Probe bodies should batch tiny operations.
    noise_floor: float = 1e-6
    #: name of a returned counter to fit the exponent on instead of wall time.
    metric: str | None = None
    #: The independently scaled input and the production assumption it tests.
    axis: str = "input size"
    assumption: str = ""
    stage: str = "component"
    group: str = "extended"
    #: Set when this probe pins a known defect rather than a contract.
    todo: Todo | None = None


_REGISTRY: dict[str, Probe] = {}


def probe(name: str, *, expect: float | None = None, sizes: tuple[int, ...],
          repeats: int = 3, tolerance: float = 0.5,
          metric: str | None = None, noise_floor: float = 1e-6,
          axis: str = "input size", assumption: str = "",
          stage: str = "component", group: str = "extended",
          todo: Todo | None = None,
          ) -> Callable[[ProbeFn], ProbeFn]:
    """Register `fn(size)` as a named complexity assumption.

    Pass `expect` for a contract, or `todo` for a known defect -- one or the
    other, never both. A marked probe takes its bound from `todo.degree`, so the
    recorded degree is written once and cannot drift from the bound enforcing
    it. That duplication is what the first hand-rolled mark had to keep in sync
    by comment.
    """
    if (expect is None) == (todo is None):
        raise ValueError(
            f"{name}: pass exactly one of expect= (a contract) or "
            f"todo= (a recorded defect)"
        )
    bound = todo.degree if todo is not None else expect
    assert bound is not None

    def register(fn: ProbeFn) -> ProbeFn:
        doc = (fn.__doc__ or "").strip()
        _REGISTRY[name] = Probe(
            name=name, fn=fn, expect=bound, sizes=sizes, doc=doc,
            repeats=repeats, tolerance=tolerance, noise_floor=noise_floor,
            metric=metric, axis=axis,
            assumption=assumption or (doc.splitlines()[0] if doc else ""),
            stage=stage, group=group, todo=todo,
        )
        return fn
    return register


def _measure(p: Probe, size: int) -> tuple[float, dict[str, int]]:
    best = math.inf
    counters: dict[str, int] = {}
    for _ in range(p.repeats):
        gc.collect()
        gc.disable()
        try:
            out = p.fn(size)
        finally:
            gc.enable()
        seconds, extra = out if isinstance(out, tuple) else (out, {})
        if seconds < best:
            best = seconds
            counters = dict(extra)
    return best, counters


def _fit_exponent(sizes: tuple[int, ...], times: list[float]) -> float:
    """Least-squares slope of log(time) over log(size)."""
    xs = [math.log(s) for s in sizes]
    ys = [math.log(max(t, 1e-9)) for t in times]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0.0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y)
               for x, y in zip(xs, ys, strict=True)) / denominator


def _classify(exponent: float) -> str:
    best = min(_CLASSES, key=lambda c: abs(c[0] - exponent))
    return best[1]


@dataclass
class Result:
    probe: Probe
    times: list[float]
    counters: list[dict[str, int]]
    exponent: float = field(init=False)
    tail_exponent: float = field(init=False)
    local_exponents: list[float] = field(init=False)
    status: str = field(init=False)
    ok: bool = field(init=False)

    def __post_init__(self) -> None:
        p = self.probe
        if p.metric is not None:
            missing = [size for size, counters in zip(p.sizes, self.counters, strict=True)
                       if p.metric not in counters]
            if missing:
                raise ValueError(
                    f"{p.name}: metric {p.metric!r} missing at sizes {missing}"
                )
            values = [float(c[p.metric]) for c in self.counters]
            below_floor = False
        else:
            values = self.times
            below_floor = max(self.times) <= p.noise_floor
        self.exponent = _fit_exponent(p.sizes, values)
        tail_count = min(3, len(p.sizes))
        self.tail_exponent = _fit_exponent(
            p.sizes[-tail_count:], values[-tail_count:]
        )
        self.local_exponents = [
            math.log(max(right, 1e-12) / max(left, 1e-12)) /
            math.log(right_size / left_size)
            for left_size, right_size, left, right in zip(
                p.sizes, p.sizes[1:], values, values[1:], strict=False
            )
        ]
        observed = max(self.exponent, self.tail_exponent)
        if below_floor:
            self.status = "UNRESOLVED"
        elif observed >= p.expect + p.tolerance:
            self.status = "FAIL"
        elif p.todo is not None and observed <= p.todo.degree - p.tolerance:
            # The recorded defect is gone. Both fits are used via `observed`
            # (the max), so a single noisy tail cannot declare a fix that the
            # global slope does not agree with -- staleness has to be the
            # honest reading of the whole curve, not the flattering half.
            self.status = "STALE"
        else:
            self.status = "PASS"
        self.ok = self.status == "PASS"


def run_probe(p: Probe) -> Result:
    # Warm up allocators, import side effects, and branch predictors off-line.
    _measure(p, max(min(p.sizes) // 4, 1))
    times: list[float] = []
    counters: list[dict[str, int]] = []
    for size in p.sizes:
        seconds, extra = _measure(p, size)
        times.append(seconds)
        counters.append(extra)
    return Result(p, times, counters)


def _print_result(r: Result) -> None:
    p = r.probe
    on = f" on {p.metric}" if p.metric else ""
    fitted = (
        f"global n^{r.exponent:.2f}, tail n^{r.tail_exponent:.2f}{on} "
        f"({_classify(r.tail_exponent)})"
    )
    bound = (f"pinned at {_classify(p.expect)}" if p.todo
             else f"at most {_classify(p.expect)}")
    print(f"\n== {p.name} — {bound}, {fitted} [{r.status}]")
    if p.todo is not None:
        print(f"   {p.todo.explain()}")
    print(f"   axis: {p.axis}; stage: {p.stage}; assumption: {p.assumption}")
    if r.status == "STALE" and p.todo is not None:
        print(f"   STALE MARK: measured {degree_name(max(r.exponent, r.tail_exponent))} "
              f"(n^{max(r.exponent, r.tail_exponent):.2f}), below the recorded "
              f"n^{p.todo.degree:g}.\n"
              f"   The defect this mark records appears to be fixed. Retarget the "
              f"mark to what\n   the code does now, or delete it and give the probe a "
              f"real expect= contract.")
    counter_names = sorted({k for c in r.counters for k in c})
    header = f"{'size':>10} {'time':>12} {'ratio':>7}"
    header += "".join(f" {name:>14}" for name in counter_names)
    print(header)
    previous = None
    for size, seconds, extra in zip(p.sizes, r.times, r.counters, strict=True):
        ratio = f"{seconds / previous:.2f}x" if previous else "-"
        row = f"{size:>10} {seconds * 1e3:>10.3f}ms {ratio:>7}"
        row += "".join(f" {extra.get(name, ''):>14}" for name in counter_names)
        print(row)
        previous = seconds
    sys.stdout.flush()


# --- probes: native timing wheel ------------------------------------------
#
# Contracts fixed in reactor_wheel.c (tie-counted slot minima, deadline-jump
# drain): batch fire and cohort cancel are linear -- one slot rescan per
# batch, not per node -- and parked long timers cost nothing to skip past.

_WHEEL_RES = 0.001
_WHEEL_SLOTS = 4096


def _wheel():
    reactor: Any = importlib.import_module("wreath._native._reactor")
    return reactor.TimingWheel(
        resolution=_WHEEL_RES, slots=_WHEEL_SLOTS, base=0.0)


def _noop() -> None:
    pass


@probe("wheel-fire-batch", expect=1.0, sizes=(2000, 4000, 8000, 16000))
def _wheel_fire_batch(k: int):
    """advance() over k same-tick timers: linear, exactly one slot rescan."""
    w = _wheel()
    handles = [w.schedule(0.050, _noop) for _ in range(k)]
    rescans = w.slot_rescans
    start = time.perf_counter()
    due = w.advance(0.100)
    elapsed = time.perf_counter() - start
    assert len(due) == k, (len(due), k)
    del handles
    return elapsed, {"slot_rescans": w.slot_rescans - rescans}


@probe("wheel-cancel-cohort", expect=1.0, sizes=(2000, 4000, 8000, 16000))
def _wheel_cancel_cohort(k: int):
    """cancel() of k same-deadline timers: linear, exactly one slot rescan."""
    w = _wheel()
    handles = [w.schedule(0.050, _noop) for _ in range(k)]
    rescans = w.slot_rescans
    start = time.perf_counter()
    for handle in handles:
        handle.cancel()
    elapsed = time.perf_counter() - start
    return elapsed, {"slot_rescans": w.slot_rescans - rescans}


@probe("wheel-parked-rotation", expect=0.0,
       sizes=(50_000, 100_000, 200_000, 400_000))
def _wheel_parked_rotation(n: int):
    """advance() across one idle rotation with n parked long timers: O(1).

    The deadline-jump drain must never touch a timer that is not due; before
    the fix this walked every live node once per rotation to decrement a
    rounds counter (linear, cache-miss bound)."""
    w = _wheel()
    for i in range(n):
        w.schedule(30.0 + (i % _WHEEL_SLOTS) * _WHEEL_RES, _noop)
    start = time.perf_counter()
    due = w.advance(_WHEEL_SLOTS * _WHEEL_RES)
    elapsed = time.perf_counter() - start
    assert len(due) == 0, len(due)
    return elapsed


# --- probes: bitset route table -------------------------------------------


def _bitset_uniform_literal_table(r: int):
    """R param routes sharing literal 'api' at seg0, distinct literal subsets
    over 11 tail positions (value f'v{j}' at position j wherever present)."""
    from wreath._native import _core

    positions = 11
    table = _core.BitsetRouteTable()
    for i in range(r):
        mask = i + 1
        segments = ["api"]
        for j in range(positions):
            segments.append(f"v{j}" if mask & (1 << j) else f"{{p{j}}}")
        table.add("/" + "/".join(segments), "GET", object())
    table.compile()
    miss = "/" + "/".join(["zzz"] + [f"v{j}" for j in range(positions)])
    return table, miss


@probe("bitset-uniform-literal-miss", expect=0.0,
       sizes=(250, 500, 1000, 2000), repeats=5, metric="verified_per_match")
def _bitset_uniform_literal_miss(r: int):
    """match() missing only at a group-uniform literal position: O(1).

    A position every route shares (e.g. a common '/api' prefix segment)
    cannot narrow among hits, but a request that misses it matches nothing;
    the plan must still probe it (ordered last) rather than dropping it and
    letting the whole group fall through to per-route verification."""
    table, miss = _bitset_uniform_literal_table(r)
    before = dict(table.probe_stats())
    loops = 200
    start = time.perf_counter()
    for _ in range(loops):
        result = table.match("GET", miss)
    elapsed = (time.perf_counter() - start) / loops
    assert result is None, result
    after = dict(table.probe_stats())
    verified = (after.get("verify_routes", 0) -
                before.get("verify_routes", 0)) // loops
    return elapsed, {"verified_per_match": verified}


@probe("loop-timer-churn", expect=1.0, sizes=(5000, 10000, 20000, 40000))
def _loop_timer_churn(k: int):
    """k call_later+cancel cycles on the metal-config loop: linear, no rot.

    Guards the schedule-then-cancel path end to end: wheel cancels are O(1)
    amortized and a cancelled timer leaves the wheel immediately -- nothing
    accumulates awaiting deadline expiry (the failure mode that made
    native_loop+heap-timers a constructor error; see EventLoop.__init__)."""
    import selectors

    from wreath.reactor import EventLoop

    loop = EventLoop(selectors.EpollSelector(),
                     native_loop=True, timers="wheel")
    try:
        start = time.perf_counter()
        handles = [loop.call_later(60.0, _noop) for _ in range(k)]
        for handle in handles:
            handle.cancel()
        elapsed = time.perf_counter() - start
        assert loop._wheel is not None
        retained = loop._wheel.count
    finally:
        loop.close()
    return elapsed, {"retained_timers": retained}


# --- probes: native HTTP/1 receive queue ----------------------------------


class _SinkTransport:
    """The minimum asyncio.Transport surface the native protocol touches."""

    def __init__(self) -> None:
        self._extra = {"sockname": ("127.0.0.1", 8000),
                       "peername": ("127.0.0.1", 54321)}
        self.closed = False
        self.bytes_written = 0

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._extra.get(name, default)

    def write(self, data: Any) -> None:
        self.bytes_written += len(data)

    def writelines(self, chunks: Any) -> None:
        self.bytes_written += sum(map(len, chunks))
    def pause_reading(self) -> None: ...
    def resume_reading(self) -> None: ...
    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.closed = True


@probe(
    "http1-receive-queue-lockstep", expect=0.0,
    sizes=(16384, 32768, 65536, 131072),
    axis="retained receive-queue capacity",
    assumption="one pop plus one push is amortized O(1) in queue capacity",
    stage="ingress", group="metal-http1",
)
def _http1_receive_queue_lockstep(cap: int):
    """Chunk ingest with the receive queue in pop/push lockstep at capacity:
    amortized O(1) per message.

    An app consuming exactly one queued message per arriving chunk while the
    queue array sits at capacity must not pay a whole-array compaction per
    push (head == 1 reclaims one slot for cap-1 pointer moves)."""
    import asyncio

    from wreath.server import ServerConfig

    _server: Any = importlib.import_module("wreath._native._server")

    chunk = b"1\r\nx\r\n"
    iterations = 256

    async def run() -> float:
        loop = asyncio.get_running_loop()
        pace = asyncio.Semaphore(0)

        async def app(scope, receive, send):
            while True:
                await pace.acquire()
                message = await receive()
                if message["type"] != "http.request":
                    return

        # High-water far above the fill so ingest never pauses: the probe
        # sizes the queue *array* (a power of two after geometric growth),
        # not the flow-control limit.
        config = ServerConfig(read_high_water_messages=1 << 22)
        protocol = _server.HttpProtocol(app, config, loop, set())
        protocol.connection_made(_SinkTransport())
        protocol.data_received(
            b"POST / HTTP/1.1\r\nHost: x\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n")
        for _ in range(cap):        # queue array grows to exactly `cap`
            protocol.data_received(chunk)
        await asyncio.sleep(0)
        ingest = 0.0
        for _ in range(iterations):
            pace.release()
            await asyncio.sleep(0)  # app pops exactly one message
            await asyncio.sleep(0)
            # Time only the ingest: the semaphore/task scheduling around it
            # costs microseconds that would drown the per-push queue work.
            start = time.perf_counter()
            protocol.data_received(chunk)
            ingest += time.perf_counter() - start
        protocol.connection_lost(None)
        await asyncio.sleep(0)
        return ingest

    return asyncio.run(run())


async def _http1_bridge_trial(
    request_chunks: tuple[bytes, ...],
    *,
    response_headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[float, int]:
    """Drive the real native protocol -> Wreath -> one-shot response bridge."""
    import asyncio

    from wreath import Response, Wreath
    from wreath.server import ServerConfig

    _server: Any = importlib.import_module("wreath._native._server")

    app = Wreath()
    done = asyncio.Event()
    response = Response(b"x", headers=response_headers)

    @app.get("/")
    async def endpoint(request):
        done.set()
        return response

    loop = asyncio.get_running_loop()
    config = ServerConfig(max_header_bytes=1 << 22, max_header_count=1 << 16)
    protocol = _server.HttpProtocol(app, config, loop, set())
    transport = _SinkTransport()
    protocol.connection_made(transport)
    start = time.perf_counter()
    for chunk in request_chunks:
        protocol.data_received(chunk)
    await done.wait()
    # The handler sets the event immediately before returning. Let its task run
    # through _finish_http and the native one-shot response before stopping.
    await asyncio.sleep(0)
    elapsed = time.perf_counter() - start
    protocol.connection_lost(None)
    await asyncio.sleep(0)
    return elapsed, transport.bytes_written


@probe(
    "http1-fragmented-head", expect=1.0,
    sizes=(2000, 4000, 8000, 16000),
    axis="request-head bytes delivered one byte at a time",
    assumption="incremental delimiter scans visit each buffered byte O(1) times",
    stage="ingress", group="metal-http1",
)
def _http1_fragmented_head(n: int):
    """Byte-at-a-time native protocol ingestion is O(total request-head bytes)."""
    import asyncio

    request = b"GET / HTTP/1.1\r\nHost: x\r\nX-Pad: " + b"x" * n + b"\r\n\r\n"
    chunks = tuple(request[index:index + 1] for index in range(len(request)))
    elapsed, written = asyncio.run(_http1_bridge_trial(chunks))
    assert written > 0
    return elapsed


@probe(
    "http1-pipelined-requests", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="keep-alive requests queued on one protocol",
    assumption="request parsing, activation, and response emission are O(requests)",
    stage="request", group="metal-http1",
)
def _http1_pipelined_requests(n: int):
    """Queued keep-alive requests complete in linear time with amortized compaction."""
    import asyncio

    from wreath import Response, Wreath
    from wreath.server import ServerConfig

    _server: Any = importlib.import_module("wreath._native._server")
    request = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"

    async def run() -> float:
        app = Wreath()
        response = Response(b"x")
        completed = 0
        done = asyncio.Event()

        @app.get("/")
        async def endpoint(request):
            nonlocal completed
            completed += 1
            if completed == n:
                done.set()
            return response

        loop = asyncio.get_running_loop()
        protocol = _server.HttpProtocol(app, ServerConfig(), loop, set())
        transport = _SinkTransport()
        protocol.connection_made(transport)
        start = time.perf_counter()
        for _ in range(n):
            protocol.data_received(request)
        await done.wait()
        await asyncio.sleep(0)
        elapsed = time.perf_counter() - start
        protocol.connection_lost(None)
        await asyncio.sleep(0)
        assert transport.bytes_written > n
        return elapsed

    return asyncio.run(run())


@probe(
    "http1-response-headers", expect=1.0,
    sizes=(2000, 4000, 8000, 16000),
    axis="response header count at fixed value width",
    assumption="one-shot response validation and serialization are O(header bytes)",
    stage="egress", group="metal-http1",
)
def _http1_response_headers(n: int):
    """Native one-shot response serialization is linear in response headers."""
    import asyncio

    headers = [(f"x-h{index}".encode(), b"v" * 16) for index in range(n)]
    request = (b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",)
    elapsed, written = asyncio.run(
        _http1_bridge_trial(request, response_headers=headers)
    )
    assert written > n * 16
    return elapsed


@probe(
    "http1-chunked-body-frames", expect=1.0,
    sizes=(2000, 4000, 8000, 16000),
    axis="chunk frames in one chunked request body, one read per frame",
    assumption="per-frame size-line scan and buffer consumption are O(frames)",
    stage="ingress", group="metal-http1",
)
def _http1_chunked_body_frames(n: int):
    """Decoding an n-frame chunked body is O(n), one read per frame.

    `http1-fragmented-head` guards the request head; the chunked body is a
    separate axis with its own resumable scan cursor (`chunk_line_scan`) and
    its own consumption. A per-frame rescan of the buffered remainder, or a
    front-shift per frame rather than on the amortized gate, would make a
    slow-trickling upload quadratic in the frames it was split into."""
    import asyncio

    from wreath import Response, Wreath
    from wreath.server import ServerConfig

    _server: Any = importlib.import_module("wreath._native._server")

    payload = b"abcdefgh"
    head = (b"POST / HTTP/1.1\r\nHost: x\r\n"
            b"transfer-encoding: chunked\r\n\r\n")
    frame = b"%x\r\n%s\r\n" % (len(payload), payload)

    async def run() -> tuple[float, dict[str, int]]:
        app = Wreath()
        done = asyncio.Event()
        received = 0

        @app.post("/")
        async def endpoint(request):
            nonlocal received
            received = len(await request.body())
            done.set()
            return Response(b"x")

        loop = asyncio.get_running_loop()
        # This probe deliberately scales chunk count beyond the production
        # default. Raise only that budget so it continues measuring parser
        # complexity instead of exercising the separate DoS rejection guard.
        protocol = _server.HttpProtocol(
            app, ServerConfig(max_body_chunks=n), loop, set()
        )
        transport = _SinkTransport()
        protocol.connection_made(transport)
        start = time.perf_counter()
        protocol.data_received(head)
        for _ in range(n):
            protocol.data_received(frame)
        protocol.data_received(b"0\r\n\r\n")
        await done.wait()
        elapsed = time.perf_counter() - start
        protocol.connection_lost(None)
        await asyncio.sleep(0)
        assert received == n * len(payload)
        return elapsed, {"body_bytes": received}

    return asyncio.run(run())


@probe(
    "wheel-colliding-slot-chain", expect=1.0, tolerance=0.6,
    sizes=(500, 1000, 2000, 4000),
    axis="timers sharing one slot at distinct deadlines",
    assumption="pairing heaps and the slot tournament drain k colliding timers in O(k)",
    stage="timers", group="metal-host",
)
def _wheel_colliding_slot_chain(k: int):
    """k timers in one slot at distinct deadlines remain linear to drain.

    `wheel_slot()` masks the deadline, so deadlines congruent modulo `nslots`
    share a slot. Each slot is now a pairing heap and the wheel maintains a
    tournament over slot minima, so arrangement does not restore the former
    repeated linked-list rescan. This adversarial shape guards that fix."""
    import contextvars

    _reactor: Any = importlib.import_module("wreath._native._reactor")

    resolution = 0.001
    nslots = 512

    def noop() -> None:
        pass

    wheel = _reactor.TimingWheel(resolution=resolution, slots=nslots, base=0.0)
    context = contextvars.copy_context()
    # ticks = nslots * i for every i, so `deadline & (nslots - 1)` is 0 for all
    # of them: one slot, k distinct deadlines, k rotations apart.
    for index in range(1, k + 1):
        wheel.schedule_call(nslots * index * resolution, noop, (), context)
    assert wheel.count == k
    start = time.perf_counter()
    fired = wheel.advance_run(nslots * (k + 1) * resolution)
    elapsed = time.perf_counter() - start
    assert fired == k
    return elapsed, {"slot_rescans": wheel.slot_rescans}


@probe(
    "wheel-spread-slot-chain", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="timers spread one per slot at consecutive deadlines",
    assumption="draining k timers that do not share a slot is O(k)",
    stage="timers", group="metal-host",
)
def _wheel_spread_slot_chain(k: int):
    """The control for `wheel-colliding-slot-chain`: same k, no collisions.

    Draining k timers costs O(k) when consecutive deadlines land in distinct
    slots. Without this control the colliding probe proves only that the wheel
    is slow at some size, not that *arrangement* is what costs -- and the two
    differ by ~75x at k=4000 on identical work."""
    import contextvars

    _reactor: Any = importlib.import_module("wreath._native._reactor")

    resolution = 0.001

    def noop() -> None:
        pass

    wheel = _reactor.TimingWheel(resolution=resolution, slots=512, base=0.0)
    context = contextvars.copy_context()
    for index in range(1, k + 1):
        wheel.schedule_call(index * resolution, noop, (), context)
    assert wheel.count == k
    start = time.perf_counter()
    fired = wheel.advance_run((k + 2) * resolution)
    elapsed = time.perf_counter() - start
    assert fired == k
    return elapsed, {"slot_rescans": wheel.slot_rescans}


@probe(
    "metal-egress-writelines-chunks", expect=1.0,
    sizes=(2000, 4000, 8000, 16000),
    axis="chunks in one writelines() behind a blocked metal socket",
    assumption="gathered-write buffering is O(chunks), not O(chunks * buffered)",
    stage="egress", group="metal-host",
)
def _metal_egress_writelines_chunks(n: int):
    """One writelines() of n chunks onto a blocked socket is O(n).

    `metal-egress-backpressure` guards repeated single writes; this guards the
    gathered form, which is the shape a streaming response emits. Appending
    each chunk by rebuilding the retained write buffer would be quadratic in
    the chunk count for exactly the response that fragments most."""
    import asyncio
    import socket

    from wreath.reactor import metal_event_loop

    class Protocol(asyncio.Protocol):
        pass

    loop = metal_event_loop(diagnostics=True)
    client, server = socket.socketpair()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    chunks = [b"y" * 64] * n
    try:
        transport = loop._make_socket_transport(server, Protocol())
        loop.run_until_complete(asyncio.sleep(0))
        # Block the peer first, so every chunk lands in the retained buffer
        # rather than going straight out to the socket.
        transport.write(b"x" * 65536)
        start = time.perf_counter()
        transport.writelines(chunks)
        elapsed = time.perf_counter() - start
        queued = transport.get_write_buffer_size()
        assert queued > 0
        transport.abort()
        loop.run_until_complete(asyncio.sleep(0))
        return elapsed, {"queued_bytes": queued}
    finally:
        client.close()
        loop.close()


@probe(
    "metal-egress-backpressure", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="fixed-size writes retained behind a blocked metal socket",
    assumption="native egress enqueue is amortized O(writes) under backpressure",
    stage="egress", group="metal-host",
)
def _metal_egress_backpressure(n: int):
    """Real native-transport enqueue remains linear while the peer does not read."""
    import asyncio
    import socket

    from wreath.reactor import metal_event_loop

    class Protocol(asyncio.Protocol):
        pass

    loop = metal_event_loop(diagnostics=True)
    client, server = socket.socketpair()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    payload = b"x" * 4096
    try:
        transport = loop._make_socket_transport(server, Protocol())
        loop.run_until_complete(asyncio.sleep(0))
        start = time.perf_counter()
        for _ in range(n):
            transport.write(payload)
        elapsed = time.perf_counter() - start
        queued = transport.get_write_buffer_size()
        assert queued > 0
        transport.abort()
        loop.run_until_complete(asyncio.sleep(0))
        return elapsed, {"queued_bytes": queued}
    finally:
        client.close()
        loop.close()


# --- probes: native body validation ---------------------------------------


def _dataclass_plan(field_count: int):
    """A compiled native plan for a dataclass with `field_count` int fields."""
    import dataclasses

    from wreath import binding

    cls = dataclasses.make_dataclass(
        "Probe", [(f"field{i}", int) for i in range(field_count)])
    return binding._compile_plan(cls, frozenset())


@probe("validate-unexpected-fields", expect=1.0,
       sizes=(400, 800, 1600, 3200))
def _validate_unexpected_fields(n: int):
    """Rejecting a body with n extra keys against an n-field schema: O(n).

    The unexpected-field check must build the field-name set once and test
    membership in O(1); the old per-key linear rescan of every field name was
    O(V*F) -- quadratic when V (body keys) and F (schema fields) both grow."""
    from wreath._native import _core

    plan = _dataclass_plan(n)
    value = {f"field{i}": 1 for i in range(n)}
    value.update({f"extra{i}": 1 for i in range(n)})
    loops = 20
    start = time.perf_counter()
    for _ in range(loops):
        _core.run_validation(plan, value, ["body"])
    return (time.perf_counter() - start) / loops


def _union_bomb_plan(depth: int):
    """A native plan nesting binary unions `depth` deep, both options recursing
    into the same subtree, so a value failing the leaf forces 2**depth work."""
    from wreath import binding

    node = (binding._OP_INT,)
    for _ in range(depth):
        node = (binding._OP_UNION, 0, (node, node), "u")
    return node


@probe("validate-union-bomb", expect=0.0,
       sizes=(22, 24, 26, 28), repeats=1, noise_floor=0.0)
def _validate_union_bomb(depth: int):
    """A nested-union validation bomb stays bounded regardless of depth: O(1).

    A union tries every option against the whole value, so without a work
    budget nested unions are O(2**depth). The step ceiling caps total node
    visits, so past the budget knee (~depth 20 at 2M steps) validation time
    plateaus -- a regression that removes the budget makes this explode."""
    from wreath._native import _core

    plan = _union_bomb_plan(depth)
    start = time.perf_counter()
    _core.run_validation(plan, "x", ["body"])   # a str fails every int leaf
    return time.perf_counter() - start


# --- baseline probes: verified-clean hot paths ----------------------------
#
# These pin the linear/bounded shape of paths audited clean, so a future change
# that regresses one to superlinear is caught as a failing exponent rather than
# a silent latency cliff. Each scales the attacker- or app-controlled dimension.


@probe("json-encode", expect=1.0, sizes=(20_000, 40_000, 80_000, 160_000))
def _json_encode(n: int):
    """Encoding an n-element structure is O(n): the writer buffer grows
    geometrically, so total bytes copied is linear, not O(n^2) per token."""
    from wreath._native import _core

    obj = {"items": list(range(n))}
    start = time.perf_counter()
    _core.json_dumps(obj)
    return time.perf_counter() - start


@probe("json-decode", expect=1.0, sizes=(20_000, 40_000, 80_000, 160_000))
def _json_decode(n: int):
    """Decoding an n-element document is O(n): single-pass scan, parser-local
    direct-mapped key cache, geometric list growth -- no rescans."""
    from wreath._native import _core

    data = _core.json_dumps({"items": list(range(n))})
    start = time.perf_counter()
    _core.json_loads(data)
    return time.perf_counter() - start


@probe("parse-cookies", expect=1.0, sizes=(2000, 4000, 8000, 16000))
def _parse_cookies(n: int):
    """Parsing an n-cookie header is O(n): one forward pass, O(1) dict ops."""
    from wreath._native import _core

    header = b"; ".join(b"k%d=v%d" % (i, i) for i in range(n))
    start = time.perf_counter()
    _core.parse_cookies(header)
    return time.perf_counter() - start


@probe("parse-qs", expect=1.0, sizes=(2000, 4000, 8000, 16000))
def _parse_qs(n: int):
    """Parsing an n-field query string is O(n): one forward pass, list append."""
    from wreath._native import _core

    query = b"&".join(b"k%d=v%d" % (i, i) for i in range(n))
    start = time.perf_counter()
    _core.parse_qs(query)
    return time.perf_counter() - start


@probe("build-header-map", expect=1.0, sizes=(2000, 4000, 8000, 16000))
def _build_header_map(n: int):
    """Building the header map from n headers is O(n): one pass into a dict."""
    from wreath._native import _core

    headers = [(b"x-h%d" % i, b"v%d" % i) for i in range(n)]
    start = time.perf_counter()
    _core.build_header_map(headers)
    return time.perf_counter() - start


@probe("append-missing-headers-bounded", expect=1.0,
       sizes=(1000, 2000, 4000, 8000))
def _append_missing_headers(n: int):
    """Injecting a fixed set of default headers into an n-header response stays
    O(n): once n exceeds 256/additions the check switches to a set, so it never
    degrades to the O(n*additions) nested scan."""
    from wreath._native import _core

    additions = [(b"x-frame-options", b"DENY"),
                 (b"x-content-type-options", b"nosniff"),
                 (b"referrer-policy", b"no-referrer"),
                 (b"x-permitted-cross-domain-policies", b"none"),
                 (b"cross-origin-opener-policy", b"same-origin")]
    start = time.perf_counter()
    headers = [[b"x-h%d" % i, b"v%d" % i] for i in range(n)]
    headers = [(a, b) for a, b in headers]
    _core.append_missing_headers(headers, additions)
    return time.perf_counter() - start


@probe(
    "replace-reused-response-headers", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="middleware headers accumulated on a reused response",
    assumption="built-in egress header replacement is linear and leaves one current value",
    stage="egress", group="metal-http1",
)
def _replace_reused_response_headers(n: int):
    """Replacing built-in headers on a reused response is O(n), then bounded.

    Request ID, CSRF, and Server-Timing used to append a new line on every
    send. Security-header scans then made n sends quadratic and retained O(n)
    memory. This adversarial input starts with n stale copies of each header;
    each replacement compacts once, retains unrelated values, and leaves one
    current line rather than front-deleting from the list.
    """
    from wreath._native import _core

    headers = (
        [(b"x-request-id", b"old")] * n
        + [(b"set-cookie", b"wreath_csrf=old; Path=/")] * n
        + [(b"server-timing", b"total;dur=9")] * n
    )
    start = time.perf_counter()
    _core.replace_response_header(headers, b"x-request-id", b"current")
    _core.replace_cookie(headers, b"wreath_csrf=", b"wreath_csrf=current; Path=/")
    _core.replace_server_timing(headers, b"total", b"total;dur=1")
    elapsed = time.perf_counter() - start
    return elapsed, {"remaining_headers": len(headers)}


@probe(
    "reused-response-lifecycle", expect=1.0,
    sizes=(250, 500, 1000, 2000),
    axis="requests returning the same mutable response",
    assumption="the standard middleware lifecycle is linear in sends and keeps headers bounded",
    stage="egress", group="metal-http1",
)
def _reused_response_lifecycle(n: int):
    """Sending one response n times stays O(n) with O(1) retained headers.

    This is the complete exploit chain, not just the replacement primitive:
    request ID, timing, CSRF, and security middleware all run around the same
    response instance. Appending observability headers made later security
    scans progressively longer, so the former lifecycle was quadratic.
    """
    import asyncio

    from wreath import Response, Wreath
    from wreath._devtools.measure import run, scope
    from wreath._devtools.sample_app import MIDDLEWARE_FACTORIES

    async def drive() -> tuple[float, dict[str, int]]:
        app = Wreath()
        for factory in MIDDLEWARE_FACTORIES:
            app.add_middleware(factory())
        response = Response(b"ok")

        @app.get("/", response_only=True)
        async def endpoint(request: Any) -> Response:
            return response

        template = scope(
            "GET",
            "/",
            {
                "host": "example.com",
                "origin": "https://example.com",
                "x-forwarded-for": "203.0.113.7",
            },
        )
        await run(app, template, 1)
        start = time.perf_counter()
        await run(app, template, n)
        elapsed = time.perf_counter() - start
        return elapsed, {"retained_headers": len(response.headers)}

    return asyncio.run(drive())


@probe("multipart-many-parts", expect=1.0, sizes=(1000, 2000, 4000, 8000))
def _multipart_many_parts(n: int):
    """Parsing an n-part multipart body is O(body): the boundary search advances
    monotonically (Two-Way memmem), so no consumed bytes are rescanned."""
    from wreath._native import _core

    part = (b"--B\r\nContent-Disposition: form-data; name=\"f%d\"\r\n\r\n"
            b"value%d\r\n")
    body = b"".join(part % (i, i) for i in range(n)) + b"--B--\r\n"
    start = time.perf_counter()
    _core.multipart_parse(body, b"B")
    return time.perf_counter() - start


# --- probes: routing match scale (the core design claim) ------------------
#
# The load-bearing routing assumption is that per-request match cost is
# independent of how many routes are registered -- adding routes must not slow
# matching. These build tables of N param routes sharing a segment count (one
# bitset group / one trie level) and time a fixed batch of matches; a router
# that degraded to scanning candidates would turn these O(1)-in-N curves O(n).

_MATCH_LOOPS = 4000


@probe(
    "bitset-router-static-scale", expect=0.0,
    sizes=(1000, 4000, 16000, 64000), repeats=1,
    axis="unrelated static route count",
    assumption="static route activation is O(1) in total route count",
    stage="routing", group="metal-http1",
)
def _bitset_router_static_scale(n: int):
    """Bitset (default) static-route match is O(1) in total route count.

    Distinct static paths resolve through the `_static` dict -- a single hash
    lookup -- so matching stays flat no matter how many routes are registered.
    This is the guarantee real route tables (mostly distinct paths spread over
    many shapes) actually rely on."""
    from wreath._native import _core

    table = _core.BitsetRouteTable()
    for i in range(n):
        table.add(f"/route{i}", "GET", object())
    table.compile()
    path = f"/route{n // 2}"
    start = time.perf_counter()
    for _ in range(_MATCH_LOOPS):
        table.match("GET", path)
    return time.perf_counter() - start


@probe(
    "bitset-router-same-group-scale", expect=1.0,
    sizes=(8000, 16000, 32000, 64000), repeats=1,
    axis="same-shape parameter route group size",
    assumption="worst-case bitset activation is O(group size / 64)",
    stage="routing", group="metal-http1",
)
def _bitset_router_same_group_scale(n: int):
    """Bitset match within ONE same-shape param group is O(group_size/64).

    This is the worst case, and the assumption "match is O(1) in route count"
    does NOT hold here: N param routes that share a (method, segment-count)
    group live in one survivor bitset, and the first intersection touches all
    N/64 words, so cost grows linearly in the group size (with a 1/64 constant).
    Benign in practice -- apps rarely register thousands of identically-shaped
    param routes -- but pinned so a regression to something worse than linear
    (e.g. a per-candidate rescan, O(group^2)) is caught. Distinct shapes or
    static paths are O(1); see bitset-router-static-scale."""
    from wreath._native import _core

    table = _core.BitsetRouteTable()
    for i in range(n):
        table.add(f"/seg{i}/{{id}}", "GET", object())   # one (GET, nseg=2) group
    table.compile()
    path = f"/seg{n // 2}/42"
    start = time.perf_counter()
    for _ in range(_MATCH_LOOPS):
        table.match("GET", path)
    return time.perf_counter() - start


@probe("trie-router-match-scale", expect=0.0, sizes=(500, 1000, 2000, 4000))
def _trie_router_match_scale(n: int):
    """Trie match cost is independent of route count: O(1) in N.

    Descent is O(path segments) with an O(1) hashed child lookup per segment,
    so registering more sibling routes must not slow a match."""
    from wreath._native import _core

    table = _core.RouteTable()
    for i in range(n):
        table.add(f"/seg{i}/{{id}}", "GET", object())
    path = f"/seg{n // 2}/42"
    start = time.perf_counter()
    for _ in range(_MATCH_LOOPS):
        table.match("GET", path)
    return time.perf_counter() - start


# --- baseline probes: ingress parse & codecs ------------------------------


@probe(
    "http-parse-request-headers", expect=1.0, sizes=(500, 1000, 2000, 4000),
    axis="request header count",
    assumption="request-head parsing is O(headers plus header bytes)",
    stage="ingress", group="metal-http1",
)
def _http_parse_request_headers(n: int):
    """Parsing a request with n headers is O(n): the header loop is a single
    forward pass, not an O(n^2) rescan or per-header dedup scan."""
    from wreath._native import _core

    data = (b"GET / HTTP/1.1\r\n"
            + b"".join(b"x-h%d: v%d\r\n" % (i, i) for i in range(n))
            + b"\r\n")
    start = time.perf_counter()
    _core.http_parse_request(data)
    return time.perf_counter() - start


@probe("json-decode-distinct-keys", expect=1.0,
       sizes=(10_000, 20_000, 40_000, 80_000))
def _json_decode_distinct_keys(n: int):
    """Decoding an object with n distinct keys is O(n): the parser-local key
    cache is direct-mapped (O(1) per key), so distinct keys do not degrade it
    into a probe chain."""
    from wreath._native import _core

    data = _core.json_dumps({f"key{i}": i for i in range(n)})
    start = time.perf_counter()
    _core.json_loads(data)
    return time.perf_counter() - start


@probe("percent-decode", expect=1.0, sizes=(20_000, 40_000, 80_000, 160_000))
def _percent_decode(n: int):
    """Percent-decoding an n-byte target is O(n): one forward pass."""
    from wreath._native import _core

    data = b"a%20b" * n
    start = time.perf_counter()
    _core.percent_decode(data)
    return time.perf_counter() - start


@probe("ws-unmask", expect=1.0, sizes=(50_000, 100_000, 200_000, 400_000))
def _ws_unmask(n: int):
    """XOR-unmasking an n-byte WebSocket payload is O(n): a single word-at-a-
    time pass, not a per-byte function-call loop."""
    from wreath._native import _core

    payload = b"x" * n
    key = b"\x01\x02\x03\x04"
    start = time.perf_counter()
    _core.ws_mask(payload, key)
    return time.perf_counter() - start


# --- baseline probe: middleware tape dispatch -----------------------------


@probe(
    "middleware-tape-fused-dispatch", expect=1.0, sizes=(8, 16, 32, 64),
    axis="fused synchronous middleware hook count",
    assumption="middleware dispatch is O(active hooks)",
    stage="middleware", group="metal-http1",
)
def _middleware_tape_fused_dispatch(n: int):
    """Dispatching a tape of n fused synchronous before hooks is O(n): the
    fused run is a single flat pass over the hooks (no per-hook coroutine),
    so adding middleware scales linearly, not worse."""
    import asyncio

    from wreath.middleware.base import MiddlewareHooks, _compile_tape

    async def endpoint(request):
        return "ok"

    tape = _compile_tape(
        endpoint,
        tuple(MiddlewareHooks(before_sync=lambda r: None) for _ in range(n)),
    )
    request: Any = object()   # the fused sync hooks ignore it
    loops = 20000

    async def run() -> float:
        for _ in range(1000):        # warm up
            await tape(request)
        start = time.perf_counter()
        for _ in range(loops):
            await tape(request)
        return time.perf_counter() - start

    return asyncio.run(run())


@probe(
    "middleware-tape-mixed-dispatch", expect=1.0, sizes=(8, 16, 32, 64),
    axis="mixed async before/after middleware pair count",
    assumption="tape dispatch is O(instructions), with a per-instruction cost "
               "that does not grow with the number of instruction kinds",
    stage="middleware", group="metal-http1",
)
def _middleware_tape_mixed_dispatch(n: int):
    """A tape of n async before+after pairs dispatches in O(instructions).

    Covers the branch the fused probe does not: a mixed tape runs the general
    dispatch loop, which decides what each instruction *is* before running it.
    That decision is made once at compile time (an opcode stored beside the
    instruction), not per instruction per request -- the previous `isinstance`
    ladder re-derived a compile-time constant on every step, at one C crossing
    per test. The request-boundary baseline does not cover this: its sample app
    registers *global* middleware, which runs from `_global_hooks` rather than
    a route tape, so that scenario never enters this loop. A regression to
    anything that rescans the tape shows up here as a higher exponent."""
    import asyncio

    from wreath.middleware.base import MiddlewareHooks, _compile_tape

    async def endpoint(request):
        return "ok"

    async def before(request):
        return None

    async def after(request, response):
        return response

    tape = _compile_tape(
        endpoint,
        tuple(MiddlewareHooks(before=before, after=after) for _ in range(n)),
    )
    assert len(tape.operations) == 2 * n + 1
    request: Any = object()
    loops = 2000

    async def run() -> float:
        for _ in range(200):         # warm up
            await tape(request)
        start = time.perf_counter()
        for _ in range(loops):
            await tape(request)
        return time.perf_counter() - start

    return asyncio.run(run())


# --- baseline probe: response coercion fast path --------------------------


@probe(
    "response-coerce-text", expect=1.0,
    sizes=(20_000, 40_000, 80_000, 160_000),
    axis="text response body bytes",
    assumption="response coercion is O(encoded body bytes)",
    stage="egress", group="metal-http1",
)
def _response_coerce_text(n: int):
    """Coercing an n-byte string handler return into a Response is O(n): the
    single-frame fast path does one utf-8 encode plus O(1) header assembly, so
    cost scales with body size, not worse."""
    from wreath.response import coerce_text

    body = "x" * n
    loops = 200
    start = time.perf_counter()
    for _ in range(loops):
        coerce_text(body)
    return time.perf_counter() - start


# --- probes: pure-Python consumer subsystems (web/orm facade) -------------
#
# The probes above pin the native tier (_core / reactor / server). These pin
# the pure-Python request-facing helpers an app actually calls per request:
# pagination query-shaping, AWS SigV4 signing, and retry backoff. Each scales a
# request- or config-controlled dimension, so a regression to superlinear here
# is a latency (or, for pagination, a mild DoS) cliff a native lint cannot see.


_PAG_MODEL: Any = None


def _pagination_model() -> Any:
    """A one-column ORM model reused across sizes (avoid re-registration)."""
    global _PAG_MODEL
    if _PAG_MODEL is None:
        from wreath.orm import Mapped, Model, column
        from wreath.orm.types import Int64, Text

        class _ProbePagRow(Model, table="_wreath_probe_pagination"):
            id: Mapped[int] = column(Int64, primary_key=True)
            name: Mapped[str] = column(Text)

        _PAG_MODEL = _ProbePagRow
    return _PAG_MODEL


@probe(
    "pagination-apply-sort", expect=1.0, sizes=(1000, 2000, 4000, 8000),
    axis="request ?sort= token count",
    assumption="folding sort tokens into the query is O(tokens)",
    stage="request", group="web",
)
def _pagination_apply_sort(n: int):
    """Applying n request-controlled sort tokens is O(n), not O(n^2).

    `Select` is immutable; a per-token `order_by` recopies a growing tuple
    (1+2+...+k) -- O(k^2) in the attacker-controlled `?sort=` token count.
    `apply_sort` folds every token into one `order_by` call, so this stays
    linear. A regression back to the per-token loop turns this quadratic."""
    from wreath.pagination import apply_sort

    base = _pagination_model().select()
    tokens = ("name",) * n
    start = time.perf_counter()
    shaped = apply_sort(base, tokens, allow=("name",))
    elapsed = time.perf_counter() - start
    assert len(shaped.orderings) == n
    return elapsed


@probe(
    "orm-hydrate-key-maps", expect=0.0, sizes=(250, 500, 1000, 2000),
    axis="rows in one hydrated result set",
    assumption="primary-key offsets are resolved per query shape, not per row",
    stage="handler", group="web", metric="key_map_builds",
)
def _orm_hydrate_key_maps(rows: int):
    """Resolving a projection's key offsets is O(1) in the row count.

    `_hydrate` used to rebuild a `{python_name: index}` dict for every row, off
    a mapping that is fixed by the compiled projection -- O(rows x columns) of
    pure repetition, paid again per join step per row on any joined shape (a
    joined load always takes this Record path rather than the native hydrate
    plan). Fitted on the deterministic build counter rather than wall time, so
    the contract holds regardless of machine noise; the timing column still
    shows the linear per-row hydration underneath it."""
    from wreath.orm.session import _count_key_map_builds

    spec, columns, session, make_row = _orm_hydrate_fixture()
    batch = [make_row(index) for index in range(rows)]
    with _count_key_map_builds() as counter:
        start = time.perf_counter()
        offsets = _orm_pk_offsets(spec, columns)
        for row in batch:
            session._hydrate(spec, columns, row, 0, offsets)
        elapsed = time.perf_counter() - start
    return elapsed, {"key_map_builds": counter[0]}


def _orm_pk_offsets(spec: Any, columns: Any) -> Any:
    from wreath.orm.session import _pk_offsets

    return _pk_offsets(spec, columns)


_ORM_HYDRATE_FIXTURE: Any = None


def _orm_hydrate_fixture() -> Any:
    """A registered model, its projection, and a detached session to hydrate into."""
    global _ORM_HYDRATE_FIXTURE
    if _ORM_HYDRATE_FIXTURE is None:
        import datetime

        from wreath.orm import Mapped, Model, column
        from wreath.orm.registry import Registry
        from wreath.orm.session import Session
        from wreath.orm.types import Int64, Text, Timestamp

        class _ProbeHydrateRow(Model, table="_wreath_probe_hydrate"):
            id: Mapped[int] = column(Int64, primary_key=True)
            email: Mapped[str] = column(Text)
            name: Mapped[str] = column(Text)
            created_at: Mapped[object] = column(Timestamp, nullable=True)

        registry = Registry(None, [_ProbeHydrateRow])
        spec = registry.spec_for(_ProbeHydrateRow)
        stamp = datetime.datetime(2026, 1, 1)

        def make_row(index: int) -> list[Any]:
            return [index, f"{index}@example.test", f"name{index}", stamp]

        _ORM_HYDRATE_FIXTURE = (
            spec, spec.columns, Session(registry, "read"), make_row,
        )
    return _ORM_HYDRATE_FIXTURE


@probe(
    "static-mount-match-scale", expect=1.0, sizes=(4, 8, 16, 32),
    axis="registered static mount count",
    assumption="an unmatched request scans mounts in registration order, and "
               "costs nothing per request-path character",
    stage="routing", group="web",
)
def _static_mount_match_scale(mounts: int):
    """Static-mount matching is O(mounts) and O(1) in path length.

    Precedence is first-registration, so the scan stops at the winner. The
    linear-in-mounts shape is the deliberate trade: this was a character trie,
    which is O(1) in mounts but pays a Python loop iteration and dict lookup
    per path character. Apps mount a handful of directories, so the scan wins
    at realistic sizes -- but that only holds while mount counts stay small,
    which is exactly what this pins. The measured path is long and matches the
    *last* mount, so it is the worst case for the scan."""
    from wreath.app import _StaticMatcher

    async def handler(request):
        return None

    matcher = _StaticMatcher()
    for index in range(mounts):
        matcher.add(f"/mount{index:04d}/", handler)
    # Deep path under the last-registered mount: full scan, and long enough
    # that a per-character implementation would show up in the constant.
    path = f"/mount{mounts - 1:04d}/" + "/".join(f"seg{i}" for i in range(24))
    loops = 20_000
    start = time.perf_counter()
    for _ in range(loops):
        result = matcher.match(path)
    elapsed = time.perf_counter() - start
    assert result is not None
    return elapsed


@probe(
    "static-mount-path-length", expect=0.0,
    sizes=(2000, 4000, 8000, 16000),
    axis="unmatched request path bytes",
    assumption="a missed static match does not scan the request path",
    stage="routing", group="web",
)
def _static_mount_path_length(length: int):
    """A request that matches no mount is O(1) in the path length.

    Reached on every 404, with an attacker-controlled path. `str.startswith`
    compares only the prefix, so a long path is rejected on its first bytes."""
    from wreath.app import _StaticMatcher

    async def handler(request):
        return None

    matcher = _StaticMatcher()
    for index in range(4):
        matcher.add(f"/mount{index}/", handler)
    path = "/other/" + "x" * length
    loops = 20_000
    start = time.perf_counter()
    for _ in range(loops):
        result = matcher.match(path)
    elapsed = time.perf_counter() - start
    assert result is None
    return elapsed


@probe(
    "graphql-parse-scale", expect=1.0, sizes=(500, 1000, 2000, 4000),
    axis="selected fields in one document",
    assumption="parsing is O(document size), never superlinear in field count",
    stage="request", group="web",
)
def _graphql_parse_scale(fields: int):
    """Parsing an n-field document is O(n).

    The document is attacker-controlled, so a superlinear parser is a
    denial-of-service primitive: a modest body would buy unbounded server work.
    Tokenizing is one `findall` and the descent visits each token a constant
    number of times, which is what keeps this linear. A regression to
    re-scanning (an earlier design re-skipped whitespace on every peek) shows
    up here as a higher constant; a regression to backtracking shows up as a
    higher exponent."""
    from wreath._graphql.parser import Limits, parse

    source = "{ " + " ".join(f"f{index}" for index in range(fields)) + " }"
    limits = Limits(
        max_complexity=10 * fields,
        max_steps=20 * fields,
        max_document_bytes=len(source) + 1,
    )
    loops = 20
    start = time.perf_counter()
    for _ in range(loops):
        document = parse(source, limits)
    elapsed = (time.perf_counter() - start) / loops
    assert document.complexity == fields
    return elapsed


@probe(
    "graphql-depth-rejection", expect=1.0, sizes=(2000, 4000, 8000, 16000),
    axis="nesting depth of a hostile document",
    assumption="rejecting an over-deep document is O(document size), and it is "
               "`max_document_bytes` -- not the depth check -- that bounds it",
    stage="request", group="web",
)
def _graphql_depth_rejection(depth: int):
    """Rejecting an over-nested document costs O(document size), not O(depth
    beyond the limit).

    This probe was written expecting O(1) and **failed**, which was the probe
    being wrong rather than the parser. The depth limit fires during the
    descent, but tokenization runs over the whole document first, so an
    over-deep document is fully tokenized before anything rejects it. Making
    the descent short-circuit tokenization would mean interleaving the two --
    the original design, measured slower for every legitimate document.

    The real bound is `max_document_bytes`, which is checked on `len()` before
    a character is scanned and therefore caps this whole curve regardless of
    how the depth check behaves. That is the right place for the O(1) guard,
    and it is why the shape here is allowed to be linear.

    What this still pins: rejection must not become *superlinear* in depth,
    which is what a backtracking or rescanning parser would produce."""
    from wreath._graphql.parser import GraphQLSyntaxError, Limits, parse

    source = "{" + "a{" * depth + "b" + "}" * depth + "}"
    limits = Limits(
        max_depth=8, max_complexity=10 * depth, max_steps=20 * depth,
        max_document_bytes=len(source) + 1,
    )
    loops = 20
    start = time.perf_counter()
    for _ in range(loops):
        try:
            parse(source, limits)
        except GraphQLSyntaxError as error:
            code = error.code
    elapsed = (time.perf_counter() - start) / loops
    assert code == "depth", code
    return elapsed


@probe(
    "sigv4-canonical-request", expect=1.0, sizes=(500, 1000, 2000, 4000),
    axis="signed request header count",
    assumption="SigV4 header signing is O(headers log headers)",
    stage="egress", group="web",
)
def _sigv4_canonical_request(n: int):
    """Signing a request with n headers is O(n log n): the canonical form sorts
    the headers once and joins them in a single pass, not a per-header rescan."""
    from wreath import _sigv4

    headers = {f"x-amz-meta-{i:05d}": f"value-{i}" for i in range(n)}
    start = time.perf_counter()
    _sigv4.sign(
        method="GET", host="bucket.s3.amazonaws.com", path="/obj",
        region="us-east-1", service="s3",
        access_key="AKIDEXAMPLE", secret_key="secret",
        amz_date="20260726T000000Z", headers=headers,
    )
    return time.perf_counter() - start


@probe(
    "compute-backoff-attempt", expect=0.0,
    sizes=(10_000, 20_000, 40_000, 80_000),
    axis="retry attempt number",
    assumption="backoff arithmetic is O(1) in the attempt number",
    stage="component", group="web",
)
def _compute_backoff_attempt(attempt: int):
    """Retry backoff is O(1) in the attempt number, not O(attempt).

    `base * factor**(attempt-1)` would grow the bignum exponent with the
    attempt count; the `min(attempt-1, 32)` cap holds the exponent constant so
    the arithmetic stays flat. A regression that drops the cap makes a hot
    retry-scheduling call scale with (and overflow at) large attempt counts."""
    from wreath._jobcore import compute_backoff

    loops = 20_000
    start = time.perf_counter()
    for _ in range(loops):
        compute_backoff(attempt, kind="exp")
    return time.perf_counter() - start


# --- checked assumption baseline ------------------------------------------


def _result_document(result: Result) -> dict[str, Any]:
    p = result.probe
    return {
        "probe": p.name,
        "group": p.group,
        "stage": p.stage,
        "axis": p.axis,
        "assumption": p.assumption,
        "expect_exponent": p.expect,
        "tolerance": p.tolerance,
        "fitted_exponent": round(result.exponent, 3),
        "tail_exponent": round(result.tail_exponent, 3),
        "local_exponents": [round(value, 3) for value in result.local_exponents],
        "class": _classify(result.tail_exponent),
        "degree_name": degree_name(result.tail_exponent),
        "todo": _todo_document(p.todo),
        "status": result.status,
        "sizes": list(p.sizes),
        "seconds": result.times,
        "counters": result.counters,
    }


@probe("cedar-set-dedupe", expect=1.0, sizes=(200, 400, 800, 1600),
       stage="handler", group="web",
       assumption="Building a Cedar set of n scalars is linear in n.")
def _cedar_set_dedupe(n: int):
    """Converting an n-element Cedar set: O(n), not O(n**2).

    A Cedar set is unordered with structural equality, so `_to_cedar_value`
    drops duplicates. Comparing each candidate against every kept one is
    quadratic, and this runs on every `is_authorized` call -- once for the
    context and once per entity attribute -- so a policy carrying a few hundred
    group ids paid it per authorization, and `/permissions` pays it once per
    (resource, action) pair. Scalars must dedupe through a hash set; only
    records and nested sets, which cannot be hashed, may compare pairwise."""
    from wreath._auth.cedar_engine import _to_cedar_value

    values = [f"group-{index}" for index in range(n)]
    loops = 5
    start = time.perf_counter()
    for _ in range(loops):
        _to_cedar_value(values, where="probe")
    return (time.perf_counter() - start) / loops


@probe("cedar-set-dedupe-comparisons", expect=0.0, sizes=(200, 400, 800, 1600),
       stage="handler", group="web", metric="comparisons",
       assumption="A Cedar set of scalars costs zero structural comparisons.")
def _cedar_set_dedupe_comparisons(n: int):
    """The same invariant stated as a count, so it cannot hide under a fast machine.

    Wall time can flatter a quadratic when the constant is small; the number of
    `_cedar_eq` calls cannot. For an all-scalar set it must be exactly zero at
    every size."""
    import wreath._auth.cedar_engine as engine

    values = [f"group-{index}" for index in range(n)]
    calls = 0
    original = engine._cedar_eq

    def counting(a: Any, b: Any) -> bool:
        nonlocal calls
        calls += 1
        return original(a, b)

    # Swapping the module's comparison is the point of this probe; the counter
    # is restored in `finally` so nothing outside it observes the substitution.
    engine._cedar_eq = counting  # ty: ignore[invalid-assignment]
    try:
        start = time.perf_counter()
        engine._to_cedar_value(values, where="probe")
        elapsed = time.perf_counter() - start
    finally:
        engine._cedar_eq = original
    return elapsed, {"comparisons": calls}


@probe("cedar-set-literal-eval", expect=1.0, sizes=(100, 200, 400, 800),
       stage="handler", group="web",
       assumption="Evaluating an n-member Cedar set literal is linear in n.")
def _cedar_set_literal_eval(n: int):
    """One authorization against a policy holding an n-member set: O(n).

    A set literal is re-evaluated on every `is_authorized` call, and the
    evaluator deduplicates it because a Cedar set is unordered with structural
    equality. Scanning the kept members per candidate is O(n**2), so a policy
    carrying an allowlist or a tenant list paid that per request -- measured
    before the fix at 4x per doubling, 2.4ms for 800 members.

    This exercises the shipped native evaluator; `_pure/cedar.py` carries the
    same algorithm and the differential tests hold their outputs identical."""
    from wreath.authorization import CedarEntity, CedarPolicies, EntityUid

    members = ", ".join(f'"m{index}"' for index in range(n))
    policies = CedarPolicies(
        f"permit(principal, action, resource) when {{ "
        f"[{members}].contains(principal.tag) }};"
    )
    entity = CedarEntity(uid=EntityUid("User", "bo"), attrs={"tag": "m0"})
    principal = EntityUid("User", "bo")
    action = EntityUid("Action", "read")
    resource = EntityUid("Doc", "1")

    loops = 5
    start = time.perf_counter()
    for _ in range(loops):
        decision = policies.is_authorized(
            principal=principal, action=action, resource=resource,
            entities=[entity])
    elapsed = (time.perf_counter() - start) / loops
    assert decision.allowed, decision
    return elapsed


@probe("authorize-any-clause-expansion", expect=0.0, sizes=(4, 8, 16, 32),
       stage="routing", group="web", metric="clauses",
       assumption="Repeated overlapping 'any' checks do not multiply clauses.")
def _authorize_any_clause_expansion(n: int):
    """n overlapping mode='any' role checks on one route: bounded clauses.

    Role and permission checks compile to disjunctive normal form, so each
    `any` check multiplies the clause list by its value count and
    `merge_requirements` accumulates them across nested routers -- K checks of
    V values each is V**K. `_eligible` then scans every clause on every
    request, so an unbounded expansion is a per-request cost, not just startup
    memory. Checks naming the same values (a role hierarchy repeated on a
    router and its parent) must collapse as they are folded in rather than
    multiplying first; distinct values are refused past a ceiling instead."""
    from wreath import Wreath
    from wreath._auth.requirements import requirement_for
    from wreath.authorization import roles

    async def handler(request):
        return {}

    endpoint = handler
    for _ in range(n):
        endpoint = roles("viewer", "editor", "owner", mode="any")(endpoint)
    app = Wreath()
    app.get("/probe")(endpoint)
    requirement = requirement_for(endpoint)
    app._compile_capabilities([requirement])

    loops = 5
    start = time.perf_counter()
    for _ in range(loops):
        clauses = app._requirement_clauses(requirement)
    elapsed = (time.perf_counter() - start) / loops
    return elapsed, {"clauses": len(clauses)}


@probe("orm-write-plan-cache", expect=0.0, sizes=(32, 64, 128, 256),
       stage="handler", group="web", metric="compiles",
       assumption="Flushing n rows of one shape compiles one statement.")
def _orm_write_plan_cache(n: int):
    """Inserting n rows of one model: one compiled statement, not n.

    The read path has compiled once per query *shape* since the compiler
    existed; the write path rebuilt its INSERT from the model spec for every
    instance -- the column filter, both `", ".join(...)` generator
    expressions, and the f-string assembly, once per row."""
    import asyncio
    import datetime
    import sys

    sys.path.insert(0, str(repo_root() / "tests"))
    from orm.conftest import FakeDatabase, Membership, Post, User  # type: ignore

    from wreath.orm.compiler import _count_write_sql_builds
    from wreath.orm.registry import Registry
    from wreath.orm.session import Session

    registry = Registry(
        FakeDatabase(), [User, Post, Membership], validate_schema="off")
    created = datetime.datetime(2024, 1, 1)

    async def flush_rows() -> None:
        session = Session(registry, "write")
        for index in range(n):
            session.add(User(id=index, email=f"u{index}@e.x",
                             name=f"n{index}", created_at=created))
        async with session.begin():
            await session.flush()

    loop = asyncio.new_event_loop()
    try:
        with _count_write_sql_builds() as builds:
            start = time.perf_counter()
            loop.run_until_complete(flush_rows())
            elapsed = time.perf_counter() - start
        compiles = builds[0]
    finally:
        loop.close()
    return elapsed, {"compiles": compiles}


# --- probes: the pure twins ------------------------------------------------
#
# Every router probe above drives the native table. Under the parity contract
# the pure twin is the *reference*, not a fallback -- it is what runs under
# `WREATH_PURE=1` and on any build without the extension -- so a pure twin that
# is quadratic where the native one is O(1) is a defect in its own right, and
# one nothing in this tree was measuring: before these, no probe in any group
# drove a single pure implementation.

@probe("pure-bitset-router-static-scale", expect=0.0,
       sizes=(200, 400, 800, 1600),
       axis="unrelated static route count",
       assumption="pure static route activation is O(1) in total route count",
       stage="routing", group="pure")
def _pure_bitset_router_static_scale(n: int):
    """The pure twin of `bitset-router-static-scale`: O(1) in route count.

    The native table resolves a distinct static path through a dict -- one hash
    lookup, flat in table size. The pure twin must do the same, or `WREATH_PURE=1`
    is a differently-shaped application rather than a slower one."""
    from wreath._pure.dtbitset import BitsetRouteTable

    table = BitsetRouteTable()
    for i in range(n):
        table.add(f"/route{i}", "GET", object())
    path = f"/route{n // 2}"
    start = time.perf_counter()
    for _ in range(_MATCH_LOOPS):
        table.match("GET", path)
    return time.perf_counter() - start


@probe("pure-bitset-router-same-group-scale", expect=1.0,
       sizes=(64, 128, 256, 512),
       axis="same-shape parameter route group size",
       assumption="pure parameter matching is at worst linear in group size",
       stage="routing", group="pure")
def _pure_bitset_router_same_group_scale(n: int):
    """The pure twin of `bitset-router-same-group-scale`.

    Every route here shares one (method, segment-count) group and carries a
    parameter -- the arrangement the bitset design exists to keep linear, and
    precisely where the decision tree grew super-linearly and lost. A pure twin
    that folded parameters the way the tree did would reintroduce the defeated
    design silently, on the path taken whenever the extension is absent."""
    from wreath._pure.dtbitset import BitsetRouteTable

    table = BitsetRouteTable()
    for i in range(n):
        table.add(f"/g{i}/{{id}}", "GET", object())
    path = f"/g{n // 2}/42"
    start = time.perf_counter()
    for _ in range(_MATCH_LOOPS):
        table.match("GET", path)
    return time.perf_counter() - start


def _pure_cancel_harness(k: int, order: str):
    """Queue k operations on a real Connection and cancel them in `order`.

    Only `_cancel_operation` is timed. The connection is built field-by-field
    rather than connected, because the method under test touches five attributes
    and none of them require a socket -- driving a real server here would
    measure the server."""
    import asyncio
    import collections

    from wreath._pure.postgres import Connection, Operation

    async def run() -> tuple[float, dict[str, int]]:
        loop = asyncio.get_running_loop()
        connection = object.__new__(Connection)
        connection._waiting = collections.deque()
        connection._waiting_live = k
        connection._transaction_barrier = False
        connection._current = None
        connection._backend_pid = 0
        connection._backend_key = 0
        operations = []
        for index in range(k):
            operation = Operation(
                index, "SELECT 1", (), "fetch", loop.create_future(), None)
            connection._waiting.append(operation)
            operations.append(operation)
        victims = operations if order == "front" else list(reversed(operations))
        start = time.perf_counter()
        for operation in victims:
            connection._cancel_operation(operation)
        elapsed = time.perf_counter() - start
        assert connection._waiting_live == 0
        assert len(connection._waiting) == k  # tombstones drain later in _flush
        for operation in operations:
            operation.future.cancel()
        return elapsed, {"cancelled": len(operations)}

    return asyncio.run(run())


@probe(
    "pure-pipeline-cancel-back-to-front", expect=1.0, tolerance=0.6,
    sizes=(500, 1000, 2000, 4000),
    axis="queued operations cancelled newest-first",
    assumption="tombstoning a queued operation is O(1), independent of position",
    stage="database", group="pure",
)
def _pure_pipeline_cancel_back_to_front(k: int):
    """Cancelling k queued operations newest-first remains O(k).

    Cancellation marks the operation and decrements `_waiting_live`; `_flush`
    drops the tombstone when it reaches the head. No cancellation searches the
    deque, so the adversarial newest-first unwind has the same linear total as
    the oldest-first control."""
    return _pure_cancel_harness(k, "back")


@probe(
    "pure-pipeline-cancel-front-to-back", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="queued operations cancelled oldest-first",
    assumption="cancelling the head of the queue is O(1) per operation",
    stage="database", group="pure",
)
def _pure_pipeline_cancel_front_to_back(k: int):
    """The order control for the newest-first cancellation probe."""
    return _pure_cancel_harness(k, "front")


def _todo_document(todo: Todo | None) -> dict[str, Any] | None:
    if todo is None:
        return None
    return {
        "degree": todo.degree,
        "target": todo.target,
        "reason": todo.reason,
        "owner": todo.owner,
    }


def _contract(p: Probe) -> dict[str, Any]:
    # `todo` rides in the contract, not the observation, so retargeting or
    # removing a mark is an assumption change that `--check` refuses until the
    # baseline is refreshed deliberately. A mark that could be edited without
    # the gate noticing would be a note, not a record.
    return {
        "group": p.group,
        "stage": p.stage,
        "axis": p.axis,
        "assumption": p.assumption,
        "expect_exponent": p.expect,
        "tolerance": p.tolerance,
        "metric": p.metric,
        "sizes": list(p.sizes),
        "todo": _todo_document(p.todo),
    }


def _baseline_path() -> Path:
    return repo_root() / BASELINE_PATH


def _write_baseline(names: list[str]) -> int:
    results = [run_probe(_REGISTRY[name]) for name in names]
    failures = [result for result in results if not result.ok]
    if failures:
        for result in failures:
            _print_result(result)
        print("wreath-complexity-probe: refusing to record a failing/unresolved baseline",
              file=sys.stderr)
        return 1
    payload = {
        "version": BASELINE_VERSION,
        "note": (
            "Checked complexity assumptions for the metal request path. Timings are "
            "observations, not absolute performance gates; --check reruns each probe "
            "and enforces its declared global and tail exponent bound."
        ),
        "probes": {
            result.probe.name: {
                "contract": _contract(result.probe),
                "observation": _result_document(result),
            }
            for result in results
        },
    }
    path = _baseline_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wreath-complexity-probe: wrote {BASELINE_PATH}")
    return 0


def _print_todo_summary(names: list[str]) -> None:
    """The marked-defect roll call, printed loudly enough that it cannot grow quietly.

    A backlog nobody reads is a backlog that only grows, so this is a header
    line with a count -- not a footnote after the results.
    """
    marked = [_REGISTRY[name] for name in names if _REGISTRY[name].todo is not None]
    if not marked:
        print("\nfix-later marks: none")
        return
    by_degree: dict[float, int] = {}
    for p in marked:
        assert p.todo is not None
        by_degree[p.todo.degree] = by_degree.get(p.todo.degree, 0) + 1
    tally = ", ".join(
        f"{count} {degree_name(degree)}"
        for degree, count in sorted(by_degree.items(), reverse=True)
    )
    print(f"\n=== fix-later marks: {len(marked)} ({tally}) ===")
    for p in sorted(marked, key=lambda q: (-(q.todo.degree if q.todo else 0), q.name)):
        assert p.todo is not None
        print(f"  {p.name:<36} n^{p.todo.degree:g} -> n^{p.todo.target:g}  "
              f"[{p.todo.owner}]")
        print(f"  {'':<36} {p.todo.reason}")


def _check_baseline(names: list[str]) -> int:
    path = _baseline_path()
    if not path.exists():
        print(f"wreath-complexity-probe: no baseline at {BASELINE_PATH}; "
              "create it with --update-baseline", file=sys.stderr)
        return 1
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != BASELINE_VERSION:
        print("wreath-complexity-probe: baseline version mismatch", file=sys.stderr)
        return 1
    recorded = payload.get("probes", {})
    failures = 0
    for name in names:
        p = _REGISTRY[name]
        before = recorded.get(name)
        if before is None or before.get("contract") != _contract(p):
            print(f"{name}: assumption differs from the baseline; run --update-baseline")
            failures += 1
            continue
        result = run_probe(p)
        _print_result(result)
        failures += 0 if result.ok else 1
    _print_todo_summary(names)
    if failures:
        print(f"wreath-complexity-probe: {failures} contract failure(s)", file=sys.stderr)
        return 1
    print(f"wreath-complexity-probe: all assumptions match {BASELINE_PATH}")
    return 0


# --- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-complexity-probe",
        description="Empirically verify complexity contracts of hot-path "
                    "operations via doubling-size scaling ratios.")
    parser.add_argument("probes", nargs="*",
                        help="probe names to run (default: all)")
    parser.add_argument("--list", action="store_true", dest="list_probes",
                        help="list registered probes and exit")
    parser.add_argument("--todos", action="store_true",
                        help="list probes marked as recorded defects and exit")
    parser.add_argument("--sizes", type=str, default=None,
                        help="comma-separated size override for every probe")
    parser.add_argument("--repeats", type=int, default=None,
                        help="best-of repeats per size (default: per probe)")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--group", help="run probes in one named group")
    parser.add_argument("--check", action="store_true",
                        help=f"rerun and check assumptions in {BASELINE_PATH}")
    parser.add_argument("--update-baseline", action="store_true",
                        help=f"record assumptions and observations in {BASELINE_PATH}")
    options = parser.parse_args(argv)

    if options.check and options.update_baseline:
        parser.error("--check and --update-baseline are exclusive")
    if options.list_probes:
        for p in _REGISTRY.values():
            bound = ("PINNED " if p.todo else "at most") + f" {_classify(p.expect):<7}"
            print(f"{p.name:<34} {p.group:<12} {bound} "
                  f"{p.axis}: {p.assumption}")
        return 0
    if options.todos:
        _print_todo_summary(list(_REGISTRY))
        return 0

    if options.probes and options.group:
        parser.error("probe names and --group are exclusive")
    names = (options.probes or
             ([name for name, p in _REGISTRY.items() if p.group == options.group]
              if options.group else list(_REGISTRY)))
    if options.group and not names:
        parser.error(f"unknown or empty group: {options.group}")
    unknown = [n for n in names if n not in _REGISTRY]
    if unknown:
        parser.error(f"unknown probe(s): {', '.join(unknown)} "
                     f"(--list shows registered names)")

    if options.update_baseline:
        return _write_baseline(names)
    if options.check:
        return _check_baseline(names)

    failures = 0
    documents: list[dict[str, Any]] = []
    for name in names:
        p = _REGISTRY[name]
        if options.sizes:
            p.sizes = tuple(int(s) for s in options.sizes.split(","))
        if options.repeats:
            p.repeats = options.repeats
        result = run_probe(p)
        if options.format == "table":
            _print_result(result)
        else:
            documents.append(_result_document(result))
        failures += 0 if result.ok else 1

    if options.format == "json":
        print(json.dumps(documents, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
