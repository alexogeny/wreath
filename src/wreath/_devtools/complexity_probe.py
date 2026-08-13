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
    if bound is None:
        raise RuntimeError(f"{name}: complexity bound was not resolved")

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
    batch = 2000
    start = time.perf_counter()
    due = ()
    for _ in range(batch):
        due = w.advance(_WHEEL_SLOTS * _WHEEL_RES)
    elapsed = time.perf_counter() - start
    if len(due) != 0:
        raise RuntimeError(f"parked timing wheel emitted {len(due)} timers")
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
    from wreath._devtools.sample_app import POLICY_FACTORIES, policy_from_components

    async def drive() -> tuple[float, dict[str, int]]:
        app = Wreath(
            http_policy=policy_from_components(
                [factory() for factory in POLICY_FACTORIES]
            )
        )
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


# --- probes: the Python consumer subsystems (web/orm facade) ---------------
#
# The probes above pin the C tier (_core / reactor / server). These pin the
# Python request-facing helpers an app actually calls per request: pagination
# query-shaping, AWS SigV4 signing, and retry backoff. Each scales a request- or
# config-controlled dimension, so a regression to superlinear here is a latency
# (or, for pagination, a mild DoS) cliff a native lint cannot see.


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
    from wreath.orm.session import _count_key_map_builds, _row_plan

    spec, columns, session, make_row = _orm_hydrate_fixture()
    batch = [make_row(index) for index in range(rows)]
    with _count_key_map_builds() as counter:
        start = time.perf_counter()
        plan = _row_plan(spec, columns)
        for row in batch:
            session._hydrate(spec, plan, row, 0)
        elapsed = time.perf_counter() - start
    return elapsed, {"key_map_builds": counter[0]}


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


def _graphql_policy_plan_harness(field_count: int, *, unique: bool) -> float:
    """Price native selection planning with a same-size alias control."""
    from wreath._graphql.ast import Field
    from wreath._graphql.schema import SchemaField, policy_resource
    from wreath._native import _core

    if unique:
        schema_fields = {
            f"field{index}": SchemaField(
                name=f"field{index}",
                type_name="String",
                non_null=False,
                is_list=False,
                policy=f"Report.field{index}",
            )
            for index in range(field_count)
        }
        fields = tuple(
            Field(name=f"field{index}", key=f"field{index}")
            for index in range(field_count)
        )
    else:
        schema_fields = {
            "shared": SchemaField(
                name="shared",
                type_name="String",
                non_null=False,
                is_list=False,
                policy="Report.shared",
            )
        }
        fields = tuple(
            Field(name="shared", key=f"alias{index}")
            for index in range(field_count)
        )
    schema = _core.graphql_policy_schema(
        tuple(item.policy for item in schema_fields.values()), policy_resource
    )
    before = time.perf_counter()
    for _ in range(500):
        state = _core.graphql_policy_state(schema)
        plan = _core.graphql_policy_prepare(
            schema, state, schema_fields, fields, None, "report"
        )
        del plan, state
    return time.perf_counter() - before


@probe(
    "graphql-policy-plan-unique",
    expect=1.0,
    sizes=(8, 16, 32, 64),
    axis="distinct selected field policies",
    assumption="native policy planning is linear in selected field count",
    stage="graphql-authorization",
    group="web",
)
def _graphql_policy_plan_unique(field_count: int):
    """Each selected field adds one schema lookup and native decision slot."""
    return _graphql_policy_plan_harness(field_count, unique=True)


@probe(
    "graphql-policy-plan-alias-control",
    expect=1.0,
    sizes=(8, 16, 32, 64),
    axis="selected aliases sharing one policy",
    assumption="the same-size alias control remains linear while deduplicating",
    stage="graphql-authorization",
    group="web",
)
def _graphql_policy_plan_alias_control(field_count: int):
    """Same-size control: every selected alias resolves to one decision."""
    return _graphql_policy_plan_harness(field_count, unique=False)


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
       assumption="The same-size flat-value control is linear in n.")
def _cedar_set_dedupe(n: int):
    """Same-size scalar control for nested Cedar structural deduplication.

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


@probe(
    "cedar-nested-set-dedupe", expect=1.0, sizes=(200, 400, 800, 1600),
    stage="handler", group="web",
    axis="singleton nested sets in one Cedar value",
    assumption="structural identities make nested-set deduplication linear",
)
def _cedar_nested_set_dedupe(n: int):
    """Nested structural values do not restore the former pairwise scan.

    This is the adversarial arm for `cedar-set-dedupe`: both carry n outer
    members, but every member here needs a structural identity.  Before the
    native identity table this measured quadratic, reaching about 70 million
    retired instructions at n=800.
    """
    from wreath._auth.cedar_engine import _to_cedar_value

    values = [[f"group-{index}"] for index in range(n)]
    loops = 5
    start = time.perf_counter()
    for _ in range(loops):
        _to_cedar_value(values, where="probe")
    return (time.perf_counter() - start) / loops


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

    This exercises the shipped evaluator."""
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


# --- probes: the driver's Python half --------------------------------------

def _cancel_harness(k: int, order: str):
    """Queue k operations on a real Connection and cancel them in `order`.

    `_cancel_operation` is one of the methods `_native._postgres.Connection`
    *inherits* from `wreath._pgdriver`, so this times the shipped driver.

    Only `_cancel_operation` is timed. The connection is built field-by-field
    rather than connected, because the method under test touches five attributes
    and none of them require a socket -- driving a real server here would
    measure the server."""
    import asyncio
    import collections

    from wreath._pgdriver import Connection, Operation

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
    "pipeline-cancel-back-to-front", expect=1.0, tolerance=0.6,
    sizes=(500, 1000, 2000, 4000),
    axis="queued operations cancelled newest-first",
    assumption="tombstoning a queued operation is O(1), independent of position",
    stage="database", group="web",
)
def _pipeline_cancel_back_to_front(k: int):
    """Cancelling k queued operations newest-first remains O(k).

    Cancellation marks the operation and decrements `_waiting_live`; `_flush`
    drops the tombstone when it reaches the head. No cancellation searches the
    deque, so the adversarial newest-first unwind has the same linear total as
    the oldest-first control."""
    return _cancel_harness(k, "back")


@probe(
    "pipeline-cancel-front-to-back", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="queued operations cancelled oldest-first",
    assumption="cancelling the head of the queue is O(1) per operation",
    stage="database", group="web",
)
def _pipeline_cancel_front_to_back(k: int):
    """The order control for the newest-first cancellation probe."""
    return _cancel_harness(k, "front")


def _series_reconcile_harness(bucket_count: int, *, populated: bool) -> float:
    """Price dense output construction with and without successful lookups."""
    from wreath.series import reconcile

    buckets = tuple(range(bucket_count))
    identities = tuple((f"tenant-{index}", False) for index in range(8))
    fills = {"requests": 0, "errors": 0, "latency": None, "saturation": None}
    if populated:
        sparse = {
            identity: {
                bucket: {
                    "requests": bucket,
                    "errors": bucket & 3,
                    "latency": bucket / 10,
                    "saturation": bucket / 100,
                }
                for bucket in buckets
            }
            for identity in identities
        }
    else:
        # Keep the same series identities, bucket run, measures and output
        # cardinality as the subject. Only successful per-cell lookups differ.
        sparse = {identity: {} for identity in identities}
    start = time.perf_counter()
    dense = reconcile(buckets, sparse, fills)
    elapsed = time.perf_counter() - start
    if len(dense) != len(identities) * len(fills):
        raise RuntimeError("series reconciliation emitted the wrong number of rows")
    return elapsed


@probe(
    "series-reconcile-populated", expect=1.0,
    sizes=(250, 500, 1000, 2000),
    axis="dense buckets at eight series and four measures",
    assumption="reconciling populated cells is linear in emitted cell count",
    stage="series", group="web",
)
def _series_reconcile_populated(bucket_count: int):
    """Successful sparse lookups add constant work to every emitted cell."""
    return _series_reconcile_harness(bucket_count, populated=True)


@probe(
    "series-reconcile-empty-control", expect=1.0,
    sizes=(250, 500, 1000, 2000),
    axis="dense buckets at eight series and four measures",
    assumption="the same-size missing-cell control is linear in emitted cell count",
    stage="series", group="web",
)
def _series_reconcile_empty_control(bucket_count: int):
    """Same-size control: identical output shape, with every lookup missing."""
    return _series_reconcile_harness(bucket_count, populated=False)


def _series_chart_spine_harness(bucket_count: int, *, populated: bool) -> float:
    """Price native range projection with a same-shape empty-data control."""
    import datetime

    from wreath.series import project_chart_spine
    from wreath.temporal import Day, Instant, spine, zone

    timezone = zone("Pacific/Auckland")
    start = Instant.of(datetime.datetime(2025, 1, 1, 11, tzinfo=datetime.UTC))
    end = start + datetime.timedelta(days=bucket_count)
    identities = tuple((f"tenant-{index}", False) for index in range(4))
    fills = {"requests": 0.0, "latency": None}
    if populated:
        buckets = spine(start, end, bucket=Day, in_zone=timezone)
        sparse = {
            identity: {
                bucket: {"requests": float(index), "latency": index / 10}
                for index, bucket in enumerate(buckets)
            }
            for identity in identities
        }
    else:
        sparse = {identity: {} for identity in identities}
    before = time.perf_counter()
    result = project_chart_spine(
        start,
        end,
        bucket=Day,
        in_zone=timezone,
        sparse=sparse,
        fills=fills,
        downsample_rows=(0, 2, 4, 6),
        full_rows=(1,),
        threshold=128,
        tick_target=9,
    )
    elapsed = time.perf_counter() - before
    if result[0] != len(identities) * len(fills):
        raise RuntimeError("series chart projection emitted the wrong row count")
    return elapsed


@probe(
    "series-chart-spine-populated", expect=1.0,
    sizes=(250, 500, 1000, 2000),
    axis="native dense buckets at four series and two measures",
    assumption="range reconciliation and path emission are linear in bucket count",
    stage="series", group="web",
)
def _series_chart_spine_populated(bucket_count: int):
    """A populated native-owned range has one bounded pass per selected row."""
    return _series_chart_spine_harness(bucket_count, populated=True)


@probe(
    "series-chart-spine-empty-control", expect=1.0,
    sizes=(250, 500, 1000, 2000),
    axis="native dense buckets at four series and two measures",
    assumption="the same-size empty range projection remains linear",
    stage="series", group="web",
)
def _series_chart_spine_empty_control(bucket_count: int):
    """Same-size control: identical paths and axes, with no populated cells."""
    return _series_chart_spine_harness(bucket_count, populated=False)


def _trajectory_grid_harness(fix_count: int, *, inside: bool) -> float:
    """Price packed trajectory scans with and without occupied cells."""
    import datetime

    from wreath.geospatial import BoundingBox, Coordinate, Trajectory, grid

    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    latitude = -27.5 if inside else -40.0
    trajectory = Trajectory(
        (
            start + datetime.timedelta(seconds=index),
            Coordinate(lat=latitude, lon=152.9 + (index % 20) * 0.001),
        )
        for index in range(fix_count)
    )
    lattice = grid(BoundingBox(-28.0, -27.0, 152.4, 153.4), metres=20_000)
    before = time.perf_counter()
    trajectory.grid_summary(
        start, start + datetime.timedelta(seconds=fix_count + 1), lattice
    )
    return time.perf_counter() - before


@probe(
    "trajectory-grid-inside", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="packed fixes scanned inside one grid",
    assumption="distance and occupancy are accumulated in one linear pass",
    stage="geospatial", group="web",
)
def _trajectory_grid_inside(fix_count: int):
    """The occupied-cell subject scans each native record once."""
    return _trajectory_grid_harness(fix_count, inside=True)


@probe(
    "trajectory-grid-outside-control", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="packed fixes scanned outside one grid",
    assumption="the same-size no-occupancy control is linear",
    stage="geospatial", group="web",
)
def _trajectory_grid_outside_control(fix_count: int):
    """Same-size control: every distance leg remains, no cell is emitted."""
    return _trajectory_grid_harness(fix_count, inside=False)


def _validation_list_harness(n: int, *, typed: bool, response: bool) -> float:
    """Price a homogeneous list contract beside an Any-item control."""
    import json

    from wreath import binding
    from wreath._native import _core

    plan = binding._compile_plan(list[int] if typed else list[Any], frozenset())
    value = list(range(n))
    wire = json.dumps(value).encode()
    loops = 20
    start = time.perf_counter()
    for _ in range(loops):
        if response:
            body, errors = _core.run_validation_json(plan, value, ("response",))
            if body is None:
                raise RuntimeError(errors)
        else:
            result, errors = _core.decode_json_validation_tape(
                wire, plan, ("body",)
            )
            if len(result) != n:
                raise RuntimeError(errors)
        if errors:
            raise RuntimeError(errors)
    return (time.perf_counter() - start) / loops


@probe(
    "validation-json-int-list", expect=1.0,
    sizes=(2000, 4000, 8000, 16000),
    axis="integers in one typed JSON request array",
    assumption="fused decode and homogeneous scalar validation are linear",
    stage="validation", group="web",
)
def _validation_json_int_list(n: int):
    """Typed request arrays validate without per-item location objects."""
    return _validation_list_harness(n, typed=True, response=False)


@probe(
    "validation-json-any-list-control", expect=1.0,
    sizes=(2000, 4000, 8000, 16000),
    axis="values in one untyped JSON request array",
    assumption="the same-size decode-only control is linear",
    stage="validation", group="web",
)
def _validation_json_any_list_control(n: int):
    """Same-size control: identical wire data with no scalar type predicate."""
    return _validation_list_harness(n, typed=False, response=False)


@probe(
    "validation-response-int-list", expect=1.0,
    sizes=(2000, 4000, 8000, 16000),
    axis="integers in one typed JSON response array",
    assumption="successful scalar validation and JSON emission are linear",
    stage="egress", group="web",
)
def _validation_response_int_list(n: int):
    """Typed response arrays do not build a duplicate Python object graph."""
    return _validation_list_harness(n, typed=True, response=True)


@probe(
    "validation-response-any-list-control", expect=1.0,
    sizes=(2000, 4000, 8000, 16000),
    axis="values in one untyped JSON response array",
    assumption="the same-size encode control is linear",
    stage="egress", group="web",
)
def _validation_response_any_list_control(n: int):
    """Same-size control: identical values with no scalar type predicate."""
    return _validation_list_harness(n, typed=False, response=True)


def _sync_state_harness(n: int, *, changed: bool) -> float:
    """Price native subscription-state reconciliation at one row bound."""
    from wreath._native import _core

    rows = tuple(
        {"key": f"row-{index}", "values": {"value": index, "active": True}}
        for index in range(n)
    )
    current = tuple(
        {
            "key": f"row-{index}",
            "values": {"value": index + int(changed), "active": True},
        }
        for index in range(n)
    )
    held = _core.sync_state(rows)
    loops = 10
    start = time.perf_counter()
    for _ in range(loops):
        state, upserted, removed = _core.sync_state_diff(held, current)
        expected = n if changed else 0
        if len(upserted) != expected or removed:
            raise RuntimeError("native sync diff returned the wrong delta")
        del state
    return (time.perf_counter() - start) / loops


@probe(
    "sync-state-all-changed", expect=1.0, sizes=(250, 500, 1000, 2000),
    axis="bounded rows whose values all changed",
    assumption="native digest comparison and upsert selection are linear",
    stage="sync", group="web",
)
def _sync_state_all_changed(n: int):
    """Every row changes, so the public Delta materializes n row references."""
    return _sync_state_harness(n, changed=True)


@probe(
    "sync-state-unchanged-control", expect=1.0,
    sizes=(250, 500, 1000, 2000),
    axis="bounded rows whose values are unchanged",
    assumption="the same-size no-delta control is linear",
    stage="sync", group="web",
)
def _sync_state_unchanged_control(n: int):
    """Same-size control: hashes and lookups remain, materialization does not."""
    return _sync_state_harness(n, changed=False)


def _trajectory_compile_harness(n: int, *, reversed_order: bool) -> float:
    """Price one-pass validation and packed trajectory construction."""
    import datetime

    from wreath.geospatial import Coordinate, Trajectory

    start_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    fixes = [
        (
            start_at + datetime.timedelta(seconds=index),
            Coordinate(lat=-27.5 + (index % 13) * 0.0001, lon=153.0),
        )
        for index in range(n)
    ]
    if reversed_order:
        fixes.reverse()
    loops = 3
    before = time.perf_counter()
    for _ in range(loops):
        trajectory = Trajectory(fixes)
        if len(trajectory) != n:
            raise RuntimeError("trajectory compiler dropped fixes")
    return (time.perf_counter() - before) / loops


@probe(
    "trajectory-compile-ordered", expect=1.0, tolerance=0.6,
    sizes=(1000, 2000, 4000, 8000),
    axis="ordered fixes copied into packed trajectory storage",
    assumption="boundary validation, sorting and distance accumulation are near-linear",
    stage="geospatial", group="web",
)
def _trajectory_compile_ordered(n: int):
    """The common ordered-fix construction path validates each pair once."""
    return _trajectory_compile_harness(n, reversed_order=False)


@probe(
    "trajectory-compile-reversed-control", expect=1.0, tolerance=0.6,
    sizes=(1000, 2000, 4000, 8000),
    axis="reverse-ordered fixes copied into packed trajectory storage",
    assumption="the same-size adversarial sort control remains near-linear",
    stage="geospatial", group="web",
)
def _trajectory_compile_reversed_control(n: int):
    """Same-size control: qsort receives reverse order before the same scan."""
    return _trajectory_compile_harness(n, reversed_order=True)


def _series_cell_rows_harness(n: int, *, populated: bool) -> float:
    """Price final heatmap cell materialization with a null-fill control."""
    from wreath._native import _core

    names = ("events", "errors", "latency", "load")
    fills = (0, 0, None, None)
    if populated:
        rows = tuple(
            (index // 100, index % 100, index, index & 3, index / 10, index / 100)
            for index in range(n)
        )
    else:
        rows = tuple((index // 100, index % 100, None, None, None, None)
                     for index in range(n))
    loops = 10
    before = time.perf_counter()
    for _ in range(loops):
        result = _core.series_cell_rows(rows, names, fills)
        if len(result) != n:
            raise RuntimeError("cell materializer changed cardinality")
    return (time.perf_counter() - before) / loops


@probe(
    "series-cell-rows-populated", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="dense heatmap cells with four populated measures",
    assumption="boundary materialization is linear in emitted cells",
    stage="series", group="web",
)
def _series_cell_rows_populated(n: int):
    """Every measure takes its database value at the result boundary."""
    return _series_cell_rows_harness(n, populated=True)


@probe(
    "series-cell-rows-empty-control", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="dense heatmap cells with four null measures",
    assumption="the same-size fill-value control is linear",
    stage="series", group="web",
)
def _series_cell_rows_empty_control(n: int):
    """Same-size control: identical output shape, every value takes its fill."""
    return _series_cell_rows_harness(n, populated=False)


def _scim_filter_harness(n: int, *, match: bool) -> float:
    """Price a compiled SCIM predicate over same-shaped resources."""
    from wreath._scim.filters import parse, select

    resources = tuple(
        {"id": str(index), "active": True, "userName": f"user-{index}"}
        for index in range(n)
    )
    node = parse('active eq true' if match else 'active eq false')
    loops = 20
    before = time.perf_counter()
    for _ in range(loops):
        selected = select(node, resources)
        if len(selected) != (n if match else 0):
            raise RuntimeError("SCIM selection returned the wrong cardinality")
    return (time.perf_counter() - before) / loops


@probe(
    "scim-filter-all-match", expect=1.0, sizes=(250, 500, 1000, 2000),
    axis="SCIM resources selected by one predicate",
    assumption="predicate evaluation and result collection are linear",
    stage="scim", group="web",
)
def _scim_filter_all_match(n: int):
    """Every resource matches and materializes at the response boundary."""
    return _scim_filter_harness(n, match=True)


@probe(
    "scim-filter-none-match-control", expect=1.0,
    sizes=(250, 500, 1000, 2000),
    axis="SCIM resources rejected by one predicate",
    assumption="the same-size no-result control is linear",
    stage="scim", group="web",
)
def _scim_filter_none_match_control(n: int):
    """Same-size control: every predicate runs, no result list entries survive."""
    return _scim_filter_harness(n, match=False)


def _scim_values_wide_harness(n: int, *, target_first: bool) -> float:
    """Price case-insensitive lookup across one wide resource mapping."""
    from wreath._scim.filters import values_at

    resource = {f"unused-{index}": index for index in range(n)}
    if target_first:
        resource = {"wanted": -1, **resource}
    else:
        resource["wanted"] = -1
    loops = 50
    before = time.perf_counter()
    for _ in range(loops):
        result = values_at(resource, "WANTED")
        if result != [-1]:
            raise RuntimeError("SCIM path lookup returned the wrong value")
    return (time.perf_counter() - before) / loops


@probe(
    "scim-values-wide-last-key", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="mapping keys visited before a case-insensitive SCIM path match",
    assumption="wide path lookup is one forward mapping pass",
    stage="scim", group="web",
)
def _scim_values_wide_last_key(n: int):
    """The wanted key follows n irrelevant keys in insertion order."""
    return _scim_values_wide_harness(n, target_first=False)


@probe(
    "scim-values-wide-first-key-control", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="same wide mapping with the SCIM path match first",
    assumption="the same-size mapping control stays bounded by one pass",
    stage="scim", group="web",
)
def _scim_values_wide_first_key_control(n: int):
    """Same-size control: target order changes without changing resource width."""
    return _scim_values_wide_harness(n, target_first=True)


def _template_loop_harness(n: int, *, escaped: bool) -> float:
    """Price a compiled template loop with and without HTML escaping."""
    from wreath.templates import Markup, Template

    template = Template.from_string(
        "{% for item in items %}{{ item }}{% endfor %}", "complexity-probe"
    )
    values: Any = tuple("<&value>" for _ in range(n))
    if not escaped:
        values = tuple(Markup("&lt;&amp;value&gt;") for _ in range(n))
    loops = 10
    before = time.perf_counter()
    for _ in range(loops):
        output = template.render_bytes({"items": values}, max_output=n * 32 + 1)
        if not output:
            raise RuntimeError("template loop emitted no output")
    return (time.perf_counter() - before) / loops


@probe(
    "template-loop-escaped", expect=1.0, sizes=(500, 1000, 2000, 4000),
    axis="escaped values emitted by one compiled template loop",
    assumption="loop execution and escaping are linear in emitted values",
    stage="egress", group="web",
)
def _template_loop_escaped(n: int):
    """Every value takes the ordinary HTML-escaping path."""
    return _template_loop_harness(n, escaped=True)


@probe(
    "template-loop-markup-control", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="trusted values emitted by one compiled template loop",
    assumption="the same-size already-safe control is linear",
    stage="egress", group="web",
)
def _template_loop_markup_control(n: int):
    """Same-size control: loop and output remain, character escaping does not."""
    return _template_loop_harness(n, escaped=False)


def _graphql_results_field_harness(n: int, *, distinct: bool) -> float:
    """Price native column-plan insertion beside an overwrite control."""
    from wreath._native import _core

    instances = (None,)
    loops = 20
    before = time.perf_counter()
    for _ in range(loops):
        results = _core.graphql_new_results(instances)
        for index in range(n):
            key = f"field-{index}" if distinct else "field"
            _core.graphql_project_constant(results, key, index)
        rows = _core.graphql_finish_results(results)
        if len(rows[0]) != (n if distinct else 1):
            raise RuntimeError("GraphQL result storage lost a field")
    return (time.perf_counter() - before) / loops


@probe(
    "graphql-results-distinct-fields", expect=1.0,
    sizes=(100, 200, 400, 800),
    axis="distinct fields accumulated in one native result plan",
    assumption="field-plan insertion is O(1) amortized per distinct key",
    stage="graphql-projection", group="web",
)
def _graphql_results_distinct_fields(n: int):
    """Distinct response keys must not linearly rescan all earlier columns."""
    return _graphql_results_field_harness(n, distinct=True)


@probe(
    "graphql-results-overwrite-control", expect=1.0,
    sizes=(100, 200, 400, 800),
    axis="writes to one repeated native result key",
    assumption="the same-size overwrite control is linear",
    stage="graphql-projection", group="web",
)
def _graphql_results_overwrite_control(n: int):
    """Same-size control: n stores resolve to one existing field slot."""
    return _graphql_results_field_harness(n, distinct=False)


def _graphql_projection_harness(n: int, *, attributes: bool) -> float:
    """Price row-wise native projection with a direct-value control."""
    from types import SimpleNamespace

    from wreath._native import _core

    values = tuple(range(n))
    instances = tuple(SimpleNamespace(value=value) for value in values)
    loops = 30
    before = time.perf_counter()
    for _ in range(loops):
        results = _core.graphql_new_results(instances)
        if attributes:
            _core.graphql_project_attribute(results, instances, "value", "value")
        else:
            _core.graphql_project_values(results, "value", values)
        rows = _core.graphql_finish_results(results)
        if len(rows) != n:
            raise RuntimeError("GraphQL projection changed row cardinality")
    return (time.perf_counter() - before) / loops


@probe(
    "graphql-project-attributes", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="object attributes projected into GraphQL result rows",
    assumption="attribute materialization is linear in response rows",
    stage="graphql-projection", group="web",
)
def _graphql_project_attributes(n: int):
    """The native result plan reads one boundary attribute per row."""
    return _graphql_projection_harness(n, attributes=True)


@probe(
    "graphql-project-values-control", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="already-resolved values projected into GraphQL result rows",
    assumption="the same-size direct-value control is linear",
    stage="graphql-projection", group="web",
)
def _graphql_project_values_control(n: int):
    """Same-size control: result ownership remains, attribute lookup does not."""
    return _graphql_projection_harness(n, attributes=False)


_PROTOBUF_COMPLEXITY_FIXTURE: Any = None


def _protobuf_complexity_fixture() -> Any:
    """One compiled declaration carrying packed and unpacked repeated ints."""
    global _PROTOBUF_COMPLEXITY_FIXTURE
    if _PROTOBUF_COMPLEXITY_FIXTURE is None:
        from wreath.protobuf import field, message

        @message
        class RepeatedProbe:
            packed: list[int] = field(1)
            unpacked: list[int] = field(2, packed=False)

        _PROTOBUF_COMPLEXITY_FIXTURE = RepeatedProbe
    return _PROTOBUF_COMPLEXITY_FIXTURE


def _protobuf_repeated_harness(n: int, *, packed: bool, decode: bool) -> float:
    """Price repeated-field wire work with the alternate wire form as control."""
    from wreath.protobuf import decode as protobuf_decode
    from wreath.protobuf import encode as protobuf_encode

    cls = _protobuf_complexity_fixture()
    values = list(range(n))
    instance = cls(packed=values) if packed else cls(unpacked=values)
    wire = protobuf_encode(instance)
    loops = 50
    before = time.perf_counter()
    for _ in range(loops):
        if decode:
            result = protobuf_decode(cls, wire)
            field = result.packed if packed else result.unpacked
            if len(field) != n:
                raise RuntimeError("protobuf decoder changed cardinality")
        elif not protobuf_encode(instance):
            raise RuntimeError("protobuf encoder emitted no repeated field")
    return (time.perf_counter() - before) / loops


@probe(
    "protobuf-encode-packed-repeated", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="varints in one packed repeated protobuf field",
    assumption="packed size and emission passes are linear in item count",
    stage="protobuf", group="web",
)
def _protobuf_encode_packed_repeated(n: int):
    """Packed values share one tag and length prefix."""
    return _protobuf_repeated_harness(n, packed=True, decode=False)


@probe(
    "protobuf-encode-unpacked-control", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="varints in one unpacked repeated protobuf field",
    assumption="the same-size per-item-tag control is linear",
    stage="protobuf", group="web",
)
def _protobuf_encode_unpacked_control(n: int):
    """Same-size control: each value carries its own field tag."""
    return _protobuf_repeated_harness(n, packed=False, decode=False)


@probe(
    "protobuf-decode-packed-repeated", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="varints decoded from one packed protobuf field",
    assumption="packed decoding visits each payload byte a bounded number of times",
    stage="protobuf", group="web",
)
def _protobuf_decode_packed_repeated(n: int):
    """One length-delimited packed field grows one native-owned decode array."""
    return _protobuf_repeated_harness(n, packed=True, decode=True)


@probe(
    "protobuf-decode-unpacked-control", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="varints decoded from repeated unpacked protobuf fields",
    assumption="the same-size tag-heavy control is linear",
    stage="protobuf", group="web",
)
def _protobuf_decode_unpacked_control(n: int):
    """Same-size control: each value re-enters field dispatch from its tag."""
    return _protobuf_repeated_harness(n, packed=False, decode=True)


def _protobuf_compile_harness(n: int, *, one_descriptor: bool) -> float:
    """Price descriptor compilation at the same total field cardinality."""
    from wreath._native import _core
    from wreath._protobuf_plan import KIND_INT64

    row = (1, KIND_INT64, 0, None)
    loops = 10
    before = time.perf_counter()
    for _ in range(loops):
        if one_descriptor:
            plan = tuple((index + 1, KIND_INT64, 0, None) for index in range(n))
            names = tuple(f"field_{index}" for index in range(n))
            result = _core.protobuf_compile(plan, names, (None,) * n, {})
            if result is None:
                raise RuntimeError("protobuf compiler produced no descriptor")
        else:
            for index in range(n):
                result = _core.protobuf_compile(
                    (row,), (f"field_{index}",), (None,), {},
                )
                if result is None:
                    raise RuntimeError("protobuf compiler produced no descriptor")
    return (time.perf_counter() - before) / loops


@probe(
    "protobuf-compile-wide-descriptor", expect=1.0,
    sizes=(100, 200, 400, 800),
    axis="fields compiled into one protobuf descriptor",
    assumption="field lookup and JSON-name uniqueness are O(n log n) or better",
    stage="protobuf-startup", group="web",
)
def _protobuf_compile_wide_descriptor(n: int):
    """Distinct camel names must not be compared with every prior field."""
    return _protobuf_compile_harness(n, one_descriptor=True)


@probe(
    "protobuf-compile-single-field-control", expect=1.0,
    sizes=(100, 200, 400, 800),
    axis="fields compiled across one-field protobuf descriptors",
    assumption="the same total-field control scales linearly",
    stage="protobuf-startup", group="web",
)
def _protobuf_compile_single_field_control(n: int):
    """Same-size control: compile n fields without an intra-plan uniqueness set."""
    return _protobuf_compile_harness(n, one_descriptor=False)


def _msgpack_collection_harness(n: int, *, mapping: bool) -> float:
    """Price MessagePack container traversal at equal element counts."""
    from wreath._native import _core

    value: Any = ({f"k{index}": index for index in range(n)} if mapping
                  else list(range(n)))
    loops = 30
    before = time.perf_counter()
    for _ in range(loops):
        encoded = _core.msgpack_dumps(value)
        if not encoded:
            raise RuntimeError("MessagePack encoder emitted no container")
    return (time.perf_counter() - before) / loops


@probe(
    "msgpack-map", expect=1.0, sizes=(1000, 2000, 4000, 8000),
    axis="entries in one MessagePack mapping",
    assumption="mapping encoding is linear in keys plus values",
    stage="msgpack", group="web",
)
def _msgpack_map(n: int):
    """Distinct keys exercise mapping iteration and string encoding."""
    return _msgpack_collection_harness(n, mapping=True)


@probe(
    "msgpack-array-control", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="items in one MessagePack array",
    assumption="the same-size scalar-array control is linear",
    stage="msgpack", group="web",
)
def _msgpack_array_control(n: int):
    """Same-size control: container growth remains, key encoding does not."""
    return _msgpack_collection_harness(n, mapping=False)


def _xml_wide_harness(n: int, *, attributes: bool, canonical: bool) -> float:
    """Price native XML parsing/canonicalization over a wide sibling set."""
    from wreath._native import _core

    child = b'<item a="v">text</item>' if attributes else b"<item>text-value</item>"
    wire = b"<root>" + child * n + b"</root>"
    limits = (len(wire) + 1, 16, n + 2, n + 2, max(64, n * 8))
    loops = 10
    before = time.perf_counter()
    for _ in range(loops):
        if canonical:
            result = _core.xml_c14n(wire, 0, len(wire), (), (), *limits)
        else:
            result = _core.xml_parse(wire, *limits)
        if result is None:
            raise RuntimeError("XML operation produced no result")
    return (time.perf_counter() - before) / loops


@probe(
    "xml-parse-wide-attributes", expect=1.0,
    sizes=(250, 500, 1000, 2000),
    axis="wide sibling elements each carrying one attribute",
    assumption="XML parsing is linear in elements and attributes",
    stage="xml", group="web",
)
def _xml_parse_wide_attributes(n: int):
    """Every sibling exercises attribute-name and value parsing."""
    return _xml_wide_harness(n, attributes=True, canonical=False)


@probe(
    "xml-parse-wide-text-control", expect=1.0,
    sizes=(250, 500, 1000, 2000),
    axis="wide sibling elements carrying text only",
    assumption="the same-size no-attribute control is linear",
    stage="xml", group="web",
)
def _xml_parse_wide_text_control(n: int):
    """Same-size control: element and text ownership remain, attributes do not."""
    return _xml_wide_harness(n, attributes=False, canonical=False)


@probe(
    "xml-c14n-wide-attributes", expect=1.0,
    sizes=(250, 500, 1000, 2000),
    axis="wide attributed siblings in exclusive canonicalization",
    assumption="namespace and attribute ordering stay linear for fixed width",
    stage="xml", group="web",
)
def _xml_c14n_wide_attributes(n: int):
    """Canonicalization repeats a fixed one-attribute scope n times."""
    return _xml_wide_harness(n, attributes=True, canonical=True)


@probe(
    "xml-c14n-wide-text-control", expect=1.0,
    sizes=(250, 500, 1000, 2000),
    axis="wide text-only siblings in exclusive canonicalization",
    assumption="the same-size no-sort control is linear",
    stage="xml", group="web",
)
def _xml_c14n_wide_text_control(n: int):
    """Same-size control: tag and text emission remain, attribute sorting does not."""
    return _xml_wide_harness(n, attributes=False, canonical=True)


def _sql_renumber_harness(n: int, *, parameters: bool) -> float:
    """Price SQL lexical renumbering beside inert dollar text."""
    from wreath._native import _core

    if parameters:
        sql = "SELECT " + ",".join(f"${index + 1}" for index in range(n))
    else:
        sql = "SELECT " + ",".join(f"'{index + 1}'" for index in range(n))
    loops = 50
    before = time.perf_counter()
    for _ in range(loops):
        result = _core.sql_renumber(sql, 10)
        if not result:
            raise RuntimeError("SQL renumbering emitted no statement")
    return (time.perf_counter() - before) / loops


@probe(
    "sql-renumber-parameters", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="positional parameters in one SQL fragment",
    assumption="parameter recognition and decimal rewriting are linear",
    stage="database", group="web",
)
def _sql_renumber_parameters(n: int):
    """Every token is an active placeholder whose number changes."""
    return _sql_renumber_harness(n, parameters=True)


@probe(
    "sql-renumber-quoted-control", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="quoted numeric tokens in one SQL fragment",
    assumption="the same-size quote-state control is linear",
    stage="database", group="web",
)
def _sql_renumber_quoted_control(n: int):
    """Same-size control: lexer state remains, no placeholder is rewritten."""
    return _sql_renumber_harness(n, parameters=False)


def _dkim_body_harness(n: int, *, whitespace: bool) -> float:
    """Price relaxed body canonicalization at equal line counts."""
    from wreath._native import _core

    line = b"alpha   beta \t gamma   \r\n" if whitespace else b"alpha beta gamma value\r\n"
    body = line * n
    loops = 30
    before = time.perf_counter()
    for _ in range(loops):
        canonical = _core.dkim_canonicalize_body(body)
        if not canonical:
            raise RuntimeError("DKIM canonicalizer emitted no body")
    return (time.perf_counter() - before) / loops


@probe(
    "dkim-relaxed-whitespace", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="body lines with repeated horizontal whitespace",
    assumption="relaxed whitespace folding is one forward pass",
    stage="egress", group="web",
)
def _dkim_relaxed_whitespace(n: int):
    """Every line exercises compression and trailing-space deletion."""
    return _dkim_body_harness(n, whitespace=True)


@probe(
    "dkim-relaxed-plain-control", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="body lines already in relaxed form",
    assumption="the same-size copy control is linear",
    stage="egress", group="web",
)
def _dkim_relaxed_plain_control(n: int):
    """Same-size control: line scanning remains, whitespace folding does not."""
    return _dkim_body_harness(n, whitespace=False)


def _sse_frame_harness(n: int, *, multiline: bool) -> float:
    """Price event-stream line splitting beside one-line output."""
    from wreath._native import _core

    data = ("x\n" * n) if multiline else ("xx" * n)
    loops = 100
    before = time.perf_counter()
    for _ in range(loops):
        frame = _core.sse_frame(None, None, None, None, data)
        if not frame:
            raise RuntimeError("SSE framer emitted no event")
    return (time.perf_counter() - before) / loops


@probe(
    "sse-multiline-data", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="newline-delimited values in one SSE data field",
    assumption="prefix insertion and newline normalization are linear",
    stage="egress", group="web",
)
def _sse_multiline_data(n: int):
    """Every two input bytes produce another data-field prefix."""
    return _sse_frame_harness(n, multiline=True)


@probe(
    "sse-single-line-control", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="same bytes in one SSE data line",
    assumption="the same-size no-split control is linear",
    stage="egress", group="web",
)
def _sse_single_line_control(n: int):
    """Same-size control: UTF-8 copying remains, line prefixes do not."""
    return _sse_frame_harness(n, multiline=False)


def _websocket_frame_harness(n: int, *, masked: bool) -> float:
    """Price native WebSocket framing and parsing at one payload size."""
    from wreath._native import _core

    payload = bytes((index * 17) & 255 for index in range(n))
    key = b"\x37\xfa\x21\x3d" if masked else None
    frame = _core.ws_build_frame(2, payload, key)
    loops = 100
    batch = 16
    before = time.perf_counter()
    for _ in range(loops):
        for _ in range(batch):
            parsed = _core.ws_parse_frame(frame)
            if parsed is None or len(parsed[2]) != n:
                raise RuntimeError("WebSocket parser changed payload length")
    return (time.perf_counter() - before) / loops


@probe(
    "websocket-parse-masked", expect=1.0,
    sizes=(2000, 4000, 8000, 16000),
    axis="masked WebSocket payload bytes",
    assumption="frame parsing and unmasking are linear in payload bytes",
    stage="websocket", group="web",
)
def _websocket_parse_masked(n: int):
    """The parser copies and unmasks the complete payload."""
    return _websocket_frame_harness(n, masked=True)


@probe(
    "websocket-parse-unmasked-control", expect=1.0,
    sizes=(2000, 4000, 8000, 16000),
    axis="unmasked WebSocket payload bytes",
    assumption="the same-size copy-only control is linear",
    stage="websocket", group="web",
)
def _websocket_parse_unmasked_control(n: int):
    """Same-size control: framing and payload ownership remain, XOR does not."""
    return _websocket_frame_harness(n, masked=False)


def _queue_drain_harness(n: int, *, priority: bool) -> float:
    """Price native bounded queue insertion and complete draining."""
    from wreath.queue import PriorityQueue, Queue

    queue: Any = (PriorityQueue(capacity=n + 1) if priority
                  else Queue(capacity=n + 1))
    before = time.perf_counter()
    if priority:
        for index in range(n):
            queue.offer(index, priority=(index * 7919) % max(n, 1))
    else:
        for index in range(n):
            queue.offer(index)
    for _ in range(n):
        queue.get_nowait()
    elapsed = time.perf_counter() - before
    if len(queue) != 0:
        raise RuntimeError("queue drain retained items")
    return elapsed


@probe(
    "priority-queue-offer-drain", expect=1.0, tolerance=0.65,
    sizes=(1000, 2000, 4000, 8000),
    axis="items offered to and drained from a native priority queue",
    assumption="heap maintenance is O(n log n), not quadratic",
    stage="queue", group="web",
)
def _priority_queue_offer_drain(n: int):
    """A varied-priority heap executes one logarithmic adjustment per item."""
    return _queue_drain_harness(n, priority=True)


@probe(
    "fifo-queue-offer-drain-control", expect=1.0,
    sizes=(1000, 2000, 4000, 8000),
    axis="items offered to and drained from a native FIFO queue",
    assumption="the same-size ring-buffer control is linear",
    stage="queue", group="web",
)
def _fifo_queue_offer_drain_control(n: int):
    """Same-size control: queue ownership remains, heap ordering does not."""
    return _queue_drain_harness(n, priority=False)


def _queue_snapshot_harness(n: int, *, priority: bool) -> float:
    """Price a non-destructive ordered snapshot at one held size."""
    from wreath.queue import PriorityQueue, Queue

    queue: Any = (PriorityQueue(capacity=n + 1) if priority
                  else Queue(capacity=n + 1))
    if priority:
        for index in range(n):
            queue.offer(index, priority=(index * 7919) % max(n, 1))
    else:
        for index in range(n):
            queue.offer(index)
    loops = 5
    before = time.perf_counter()
    for _ in range(loops):
        result = queue.snapshot()
        if len(result) != n:
            raise RuntimeError("queue snapshot changed cardinality")
    return (time.perf_counter() - before) / loops


@probe(
    "priority-queue-snapshot", expect=1.0, tolerance=0.65,
    sizes=(500, 1000, 2000, 4000),
    axis="held priority-queue entries copied in get order",
    assumption="snapshot ordering is O(n log n), not selection-sort quadratic",
    stage="queue", group="web",
)
def _priority_queue_snapshot(n: int):
    """The copied heap must be ordered without repeatedly scanning its tail."""
    return _queue_snapshot_harness(n, priority=True)


@probe(
    "fifo-queue-snapshot-control", expect=1.0,
    sizes=(500, 1000, 2000, 4000),
    axis="held FIFO entries copied in get order",
    assumption="the same-size ring snapshot control is linear",
    stage="queue", group="web",
)
def _fifo_queue_snapshot_control(n: int):
    """Same-size control: owned references are copied without ordering."""
    return _queue_snapshot_harness(n, priority=False)


def _rank_indices_harness(n: int, *, sorted_input: bool) -> float:
    """Price numeric ranking under ordered and disordered inputs."""
    from wreath._native import _core

    scores = ([float(index) for index in range(n)] if sorted_input else
              [float((index * 7919) % max(n, 1)) for index in range(n)])
    loops = 30
    before = time.perf_counter()
    for _ in range(loops):
        result = _core.rank_indices(scores, 0, n, False)
        if len(result) != n:
            raise RuntimeError("rank kernel changed cardinality")
    return (time.perf_counter() - before) / loops


@probe(
    "rank-indices-disordered", expect=1.0, tolerance=0.65,
    sizes=(1000, 2000, 4000, 8000),
    axis="disordered numeric scores ranked into indices",
    assumption="native ranking is O(n log n), not quadratic",
    stage="query", group="web",
)
def _rank_indices_disordered(n: int):
    """A deterministic permutation exercises the general qsort path."""
    return _rank_indices_harness(n, sorted_input=False)


@probe(
    "rank-indices-sorted-control", expect=1.0, tolerance=0.65,
    sizes=(1000, 2000, 4000, 8000),
    axis="already-sorted numeric scores ranked into indices",
    assumption="the same-size ordered control remains O(n log n) or better",
    stage="query", group="web",
)
def _rank_indices_sorted_control(n: int):
    """Same-size control: conversion and output remain, comparisons simplify."""
    return _rank_indices_harness(n, sorted_input=True)


def _fused_order_harness(n: int, *, overlap: bool) -> float:
    """Price reciprocal-rank fusion at equal ranking-cell counts."""
    from wreath._native import _core

    if overlap:
        base = tuple(f"item-{index}" for index in range(n))
        rankings = (base, base[::-1], base)
    else:
        rankings = tuple(
            tuple(f"arm-{arm}-item-{index}" for index in range(n))
            for arm in range(3)
        )
    loops = 10
    before = time.perf_counter()
    for _ in range(loops):
        result = _core.fused_order(rankings, 60)
        expected = n if overlap else n * 3
        if len(result) != expected:
            raise RuntimeError("rank fusion changed unique cardinality")
    return (time.perf_counter() - before) / loops


@probe(
    "rank-fusion-overlap", expect=1.0, tolerance=0.65,
    sizes=(500, 1000, 2000, 4000),
    axis="ranking cells resolving to an overlapping key set",
    assumption="hash accumulation plus final sort are O(n log n)",
    stage="query", group="web",
)
def _rank_fusion_overlap(n: int):
    """Three arms update the same n native hash slots."""
    return _fused_order_harness(n, overlap=True)


@probe(
    "rank-fusion-disjoint-control", expect=1.0, tolerance=0.65,
    sizes=(500, 1000, 2000, 4000),
    axis="same ranking cells resolving to disjoint key sets",
    assumption="the same-size maximum-output control remains O(n log n)",
    stage="query", group="web",
)
def _rank_fusion_disjoint_control(n: int):
    """Same-size input cell count: three times as many unique result slots."""
    return _fused_order_harness(n, overlap=False)


def _accept_sort_harness(n: int, *, ascending: bool) -> float:
    """Price Accept parsing under adverse and already-ranked quality order."""
    from wreath._native import _core

    indices = range(n) if ascending else range(n - 1, -1, -1)
    header = ",".join(
        f"application/x-{index};q={(index + 1) / (n + 1):.8f}"
        for index in indices
    )
    loops = 20
    before = time.perf_counter()
    for _ in range(loops):
        ranges = _core.parse_accept(header)
        if len(ranges) != n:
            raise RuntimeError("Accept parser changed range cardinality")
    return (time.perf_counter() - before) / loops


@probe(
    "accept-ranges-reverse-quality", expect=1.0, tolerance=0.45,
    sizes=(250, 500, 1000, 2000),
    axis="Accept ranges arriving opposite their negotiated quality order",
    assumption="range ordering is O(n log n), never insertion-sort quadratic",
    stage="ingress", group="web",
)
def _accept_ranges_reverse_quality(n: int):
    """Each later range sorts ahead of every earlier one."""
    return _accept_sort_harness(n, ascending=True)


@probe(
    "accept-ranges-ranked-control", expect=1.0, tolerance=0.65,
    sizes=(250, 500, 1000, 2000),
    axis="Accept ranges arriving in negotiated quality order",
    assumption="the same-size already-ranked control is O(n log n) or better",
    stage="ingress", group="web",
)
def _accept_ranges_ranked_control(n: int):
    """Same-size control: tokenization and output remain, ordering is pre-sorted."""
    return _accept_sort_harness(n, ascending=False)


def _media_negotiation_harness(n: int, *, adversarial: bool) -> float:
    """Price media selection with equally wide range and offer sets."""
    from wreath._native import _core

    offers = tuple(f"application/x-{index}" for index in range(n))
    if adversarial:
        accepted = ["application/*;q=0.5"] * n
        denied = [f"application/x-{index};q=0" for index in range(n)]
        header = ",".join((*accepted, *denied))
    else:
        header = ",".join(
            f"application/missing-{index};q=0" for index in range(n * 2)
        )
    loops = 5
    before = time.perf_counter()
    for _ in range(loops):
        selected = _core.negotiate_media(header, offers)
        if selected is not None:
            raise RuntimeError("excluded media negotiation unexpectedly matched")
    return (time.perf_counter() - before) / loops


@probe(
    "media-negotiation-excluded-wildcards", expect=1.0,
    sizes=(50, 100, 200, 400),
    axis="accepted wildcards, denied exact ranges, and offered media types",
    assumption="exclusions and candidates are indexed per call, not cross-scanned",
    stage="ingress", group="web",
)
def _media_negotiation_excluded_wildcards(n: int):
    """Every wildcard matches every offer, all of which exact ranges exclude."""
    return _media_negotiation_harness(n, adversarial=True)


@probe(
    "media-negotiation-denied-control", expect=1.0,
    sizes=(50, 100, 200, 400),
    axis="same count of denied ranges beside the same offered media set",
    assumption="the same-size parse and normalization control is linear",
    stage="ingress", group="web",
)
def _media_negotiation_denied_control(n: int):
    """Same-size control: no positive range enters candidate selection."""
    return _media_negotiation_harness(n, adversarial=False)


def _argument_normalization_harness(n: int, *, nested: bool) -> float:
    """Price bounded JSON normalization at equal container-edge counts."""
    from wreath._native import _core

    if nested:
        value: Any = 0
        for _ in range(n):
            value = [value]
    else:
        value = [0] * n
    loops = 20
    before = time.perf_counter()
    for _ in range(loops):
        result = _core.normalise_argument(value, n + 2, n + 2, n * 8 + 64)
        if not result:
            raise RuntimeError("argument normalizer emitted no JSON")
    return (time.perf_counter() - before) / loops


@probe(
    "argument-normalize-deep", expect=1.0,
    sizes=(100, 200, 400, 800),
    axis="nested container edges in one bounded policy argument",
    assumption="cycle detection uses an operation-owned active set, not ancestor rescans",
    stage="authorization", group="web",
)
def _argument_normalize_deep(n: int):
    """Acyclic singleton lists make depth equal the number of emitted edges."""
    return _argument_normalization_harness(n, nested=True)


@probe(
    "argument-normalize-flat-control", expect=1.0,
    sizes=(100, 200, 400, 800),
    axis="flat scalar items in one bounded policy argument",
    assumption="the same-size JSON-emission control is linear",
    stage="authorization", group="web",
)
def _argument_normalize_flat_control(n: int):
    """Same-size control: output and field accounting remain without deep ancestry."""
    return _argument_normalization_harness(n, nested=False)


def _sparsevector_compile_harness(n: int, *, ordered: bool) -> float:
    """Price conversion of a Python declaration into native sparse storage."""
    from wreath._native import _core

    indices = (range(1, n + 1) if ordered else
               sorted(range(1, n + 1), key=lambda index: (index * 7919) % n))
    elements = {index: float(index) for index in indices}
    loops = 20
    before = time.perf_counter()
    for _ in range(loops):
        data = _core.sparsevector_data(n + 1, elements, n + 1)
        if _core.sparsevector_len(data) != n:
            raise RuntimeError("sparse-vector compiler changed cardinality")
    return (time.perf_counter() - before) / loops


@probe(
    "sparsevector-compile-disordered", expect=1.0, tolerance=0.65,
    sizes=(500, 1000, 2000, 4000),
    axis="disordered sparse-vector declaration entries",
    assumption="native storage compilation is O(n log n), never quadratic",
    stage="database", group="web",
)
def _sparsevector_compile_disordered(n: int):
    """A deterministic key permutation exercises the general ordering path."""
    return _sparsevector_compile_harness(n, ordered=False)


@probe(
    "sparsevector-compile-ordered-control", expect=1.0, tolerance=0.65,
    sizes=(500, 1000, 2000, 4000),
    axis="already-ordered sparse-vector declaration entries",
    assumption="the same-size native-storage control is O(n log n) or better",
    stage="database", group="web",
)
def _sparsevector_compile_ordered_control(n: int):
    """Same-size control: conversion and native filling remain with one sorted run."""
    return _sparsevector_compile_harness(n, ordered=True)


def _prometheus_routes_harness(n: int, *, populated: bool) -> float:
    """Price native route-metric rendering at equal route cardinality."""
    from types import SimpleNamespace

    from wreath._native import _core

    buckets = tuple(1 if populated else 0 for _ in range(64))
    routes = tuple(
        SimpleNamespace(
            route_id=index, count=64, errors=0,
            duration_us_sum=4096.0, duration_us_max=128.0,
            buckets=buckets,
        )
        for index in range(n)
    )
    names = ("requests", "errors", "duration", "maximum")
    loops = 10
    before = time.perf_counter()
    for _ in range(loops):
        blocks = _core.prometheus_route_blocks(routes, names, None, False)
        if len(blocks) != 4:
            raise RuntimeError("Prometheus renderer changed family cardinality")
    return (time.perf_counter() - before) / loops


@probe(
    "prometheus-routes-full-histograms", expect=1.0,
    sizes=(50, 100, 200, 400),
    axis="route metric rows with every fixed histogram bucket populated",
    assumption="route planning and bounded histogram emission are linear in routes",
    stage="observability", group="web",
)
def _prometheus_routes_full_histograms(n: int):
    """Every route emits all 64 duration buckets plus scalar families."""
    return _prometheus_routes_harness(n, populated=True)


@probe(
    "prometheus-routes-empty-control", expect=1.0,
    sizes=(50, 100, 200, 400),
    axis="same route metric rows with empty fixed histograms",
    assumption="the same-size route-planning control is linear",
    stage="observability", group="web",
)
def _prometheus_routes_empty_control(n: int):
    """Same-size control: attributes and scalar families remain, buckets omit."""
    return _prometheus_routes_harness(n, populated=False)


def _flight_metadata_harness(n: int, *, ordered: bool) -> float:
    """Price canonical native metadata emission under row ordering changes."""
    from wreath._flight_schema import SCHEMA_VERSION, MetadataImage, NamedMeta

    indices = (range(n) if ordered else range(n - 1, -1, -1))
    dependencies = tuple(NamedMeta(index + 1, f"dependency-{index}")
                         for index in indices)
    image = MetadataImage(
        version=SCHEMA_VERSION,
        routes=(), plans=(), dependencies=dependencies, middleware=(),
        auth_policies=(), serializers=(), validators=(), limits=(),
        clients=(), databases=(), models=(),
    )
    loops = 20
    before = time.perf_counter()
    for _ in range(loops):
        wire = image.canonical_bytes()
        if not wire:
            raise RuntimeError("flight metadata encoder emitted no image")
    return (time.perf_counter() - before) / loops


@probe(
    "flight-metadata-reverse-rows", expect=1.0, tolerance=0.65,
    sizes=(250, 500, 1000, 2000),
    axis="reverse-ID native-flight metadata rows",
    assumption="canonical row ordering is O(n log n), never quadratic",
    stage="observability", group="web",
)
def _flight_metadata_reverse_rows(n: int):
    """Rows arrive opposite their canonical ID order."""
    return _flight_metadata_harness(n, ordered=False)


@probe(
    "flight-metadata-ordered-control", expect=1.0, tolerance=0.65,
    sizes=(250, 500, 1000, 2000),
    axis="already-ID-ordered native-flight metadata rows",
    assumption="the same-size canonical-emission control is O(n log n) or better",
    stage="observability", group="web",
)
def _flight_metadata_ordered_control(n: int):
    """Same-size control: field reads and wire output remain in canonical order."""
    return _flight_metadata_harness(n, ordered=True)


def _privacy_topology_harness(n: int, *, chain: bool) -> float:
    """Price the privacy planner's children-first graph calculation."""
    from wreath._privacy.graph import Graph, Node, order_children_first
    from wreath._privacy.model import Edge

    models: tuple[type, ...] = tuple(
        type(f"PrivacyProbe{index}", (), {}) for index in range(n)
    )
    nodes: dict[type, Node] = {
        model: Node(model, model.__name__, "public", f"t{index:06d}", (), {})
        for index, model in enumerate(models)
    }
    edge = Edge("child", "parent_id", "parent", "id", "r")
    outbound: dict[type, tuple[tuple[Edge, type], ...]] = (
        {
            models[index]: ((edge, models[index - 1]),)
            for index in range(1, n)
        }
        if chain
        else {}
    )
    graph = Graph(nodes, outbound, {})
    members: set[type] = set(models)
    loops = 10
    before = time.perf_counter()
    for _ in range(loops):
        ordered, cycles = order_children_first(graph, members)
        if len(ordered) != n or cycles:
            raise RuntimeError("privacy topology probe produced an invalid order")
    return (time.perf_counter() - before) / loops


@probe(
    "privacy-topology-chain", expect=1.0,
    sizes=(200, 400, 800, 1600),
    axis="models in one foreign-key chain",
    assumption="children-first ordering visits each model and edge once",
    stage="privacy", group="extended",
)
def _privacy_topology_chain(n: int):
    """A chain unlocks exactly one parent at each topological layer."""
    return _privacy_topology_harness(n, chain=True)


@probe(
    "privacy-topology-disconnected-control", expect=1.0,
    sizes=(200, 400, 800, 1600),
    axis="disconnected models in one privacy graph",
    assumption="the same-size deterministic-ordering control is linear",
    stage="privacy", group="extended",
)
def _privacy_topology_disconnected_control(n: int):
    """Same-size control: model ordering remains while dependency edges do not."""
    return _privacy_topology_harness(n, chain=False)


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
            "Checked complexity assumptions for native and request-hot paths. "
            "Timings are observations, not absolute performance gates; --check "
            "reruns each probe and enforces its declared global and tail exponent bound."
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
        if p.todo is None:
            raise RuntimeError(f"{p.name}: marked probe lost its defect contract")
        by_degree[p.todo.degree] = by_degree.get(p.todo.degree, 0) + 1
    tally = ", ".join(
        f"{count} {degree_name(degree)}"
        for degree, count in sorted(by_degree.items(), reverse=True)
    )
    print(f"\n=== fix-later marks: {len(marked)} ({tally}) ===")
    for p in sorted(marked, key=lambda q: (-(q.todo.degree if q.todo else 0), q.name)):
        if p.todo is None:
            raise RuntimeError(f"{p.name}: marked probe lost its defect contract")
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
