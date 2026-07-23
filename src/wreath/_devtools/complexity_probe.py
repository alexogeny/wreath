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

Registered probes pin the complexity contracts of hot-path data structures
(the timing wheel's batch fire/cancel and parked-timer costs, first). Each
probe declares the exponent it expects; the run fails (exit 1) when the fitted
exponent drifts a whole class away, so a regression to a rescan-per-node shape
shows up as a reviewable number rather than as drift nobody noticed.

Adding a probe: decorate a callable taking a size and returning either the
elapsed-seconds float for that size, or a (seconds, {counter: value}) pair --
counters are printed alongside and often identify the mechanism (e.g. the
wheel's `slot_rescans` going k -> 1 is the tie-count fix, visible).

Timings run with GC disabled, best-of-`repeats` per size, warmed up at a small
size first. Sizes double so the fitted exponent has three or more steps to
settle; keep a probe's largest size small enough to finish in tens of
milliseconds -- this is a shape check, not a throughput benchmark.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ProbeFn = Callable[[int], "float | tuple[float, dict[str, int]]"]

#: exponent -> printable class, in fit order
_CLASSES = ((0.0, "O(1)"), (1.0, "O(n)"), (2.0, "O(n^2)"), (3.0, "O(n^3)"))


@dataclass
class Probe:
    name: str
    fn: ProbeFn
    expect: float          # expected growth exponent (0 constant, 1 linear, ...)
    sizes: tuple[int, ...]
    doc: str = ""
    repeats: int = 3
    #: exponent drift that still counts as the expected class; a whole class
    #: away (>= 0.5 toward the next integer) fails the probe.
    tolerance: float = 0.5
    #: when every size finishes under this, growth is unobservable and the
    #: probe passes -- fitting an exponent to scheduler jitter proves nothing,
    #: and the regressions probes guard against sit orders of magnitude above.
    noise_floor: float = 100e-6
    #: name of a returned counter to fit the exponent on instead of wall time.
    #: Counters are deterministic work measures (rescans, verifications), so a
    #: shape proof on one is immune to timer jitter and the noise floor.
    metric: str | None = None


_REGISTRY: dict[str, Probe] = {}


def probe(name: str, *, expect: float, sizes: tuple[int, ...],
          repeats: int = 3, tolerance: float = 0.5,
          metric: str | None = None,
          noise_floor: float = 100e-6) -> Callable[[ProbeFn], ProbeFn]:
    """Register `fn(size)` as a named complexity probe."""
    def register(fn: ProbeFn) -> ProbeFn:
        _REGISTRY[name] = Probe(name, fn, expect, sizes,
                                (fn.__doc__ or "").strip(), repeats, tolerance,
                                noise_floor=noise_floor, metric=metric)
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
    ok: bool = field(init=False)

    def __post_init__(self) -> None:
        p = self.probe
        if p.metric is not None:
            values = [float(max(c.get(p.metric, 0), 1)) for c in self.counters]
            self.exponent = _fit_exponent(p.sizes, values)
            self.below_floor = False   # counters are jitter-free: always fit
        else:
            self.exponent = _fit_exponent(p.sizes, self.times)
            self.below_floor = max(self.times) <= p.noise_floor
        self.ok = (self.below_floor or
                   abs(self.exponent - p.expect) < p.tolerance)


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
    verdict = "ok" if r.ok else "FAIL"
    on = f" on {p.metric}" if p.metric else ""
    fitted = ("below noise floor" if r.below_floor
              else f"fitted n^{r.exponent:.2f}{on} ({_classify(r.exponent)})")
    print(f"\n== {p.name} — expect {_classify(p.expect)}, {fitted} [{verdict}]")
    if p.doc:
        print(f"   {p.doc.splitlines()[0]}")
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
    from wreath._native import _reactor
    return _reactor.TimingWheel(
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

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._extra.get(name, default)

    def write(self, data: Any) -> None: ...
    def writelines(self, chunks: Any) -> None: ...
    def pause_reading(self) -> None: ...
    def resume_reading(self) -> None: ...
    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.closed = True


@probe("http1-receive-queue-lockstep", expect=0.0,
       sizes=(16384, 32768, 65536, 131072), noise_floor=1e-6)
def _http1_receive_queue_lockstep(cap: int):
    """Chunk ingest with the receive queue in pop/push lockstep at capacity:
    amortized O(1) per message.

    An app consuming exactly one queued message per arriving chunk while the
    queue array sits at capacity must not pay a whole-array compaction per
    push (head == 1 reclaims one slot for cap-1 pointer moves)."""
    import asyncio

    from wreath._native import _server
    from wreath.server import ServerConfig

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
        return ingest / iterations

    return asyncio.run(run())


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


@probe("bitset-router-static-scale", expect=0.0,
       sizes=(1000, 4000, 16000, 64000), repeats=1)
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


@probe("bitset-router-same-group-scale", expect=1.0,
       sizes=(8000, 16000, 32000, 64000), repeats=1)
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


@probe("http-parse-request-headers", expect=1.0, sizes=(500, 1000, 2000, 4000))
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


@probe("middleware-tape-fused-dispatch", expect=1.0,
       sizes=(8, 16, 32, 64))
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
        return (time.perf_counter() - start) / loops

    return asyncio.run(run())


# --- baseline probe: response coercion fast path --------------------------


@probe("response-coerce-text", expect=1.0,
       sizes=(20_000, 40_000, 80_000, 160_000))
def _response_coerce_text(n: int):
    """Coercing an n-byte string handler return into a Response is O(n): the
    single-frame fast path does one utf-8 encode plus O(1) header assembly, so
    cost scales with body size, not worse."""
    from wreath.response import coerce_text

    body = "x" * n
    start = time.perf_counter()
    coerce_text(body)
    return time.perf_counter() - start


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
    parser.add_argument("--sizes", type=str, default=None,
                        help="comma-separated size override for every probe")
    parser.add_argument("--repeats", type=int, default=None,
                        help="best-of repeats per size (default: per probe)")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    options = parser.parse_args(argv)

    if options.list_probes:
        for p in _REGISTRY.values():
            summary = p.doc.splitlines()[0] if p.doc else ""
            print(f"{p.name:<24} expect {_classify(p.expect):<7} {summary}")
        return 0

    names = options.probes or list(_REGISTRY)
    unknown = [n for n in names if n not in _REGISTRY]
    if unknown:
        parser.error(f"unknown probe(s): {', '.join(unknown)} "
                     f"(--list shows registered names)")

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
            documents.append({
                "probe": p.name,
                "expect_exponent": p.expect,
                "fitted_exponent": round(result.exponent, 3),
                "class": _classify(result.exponent),
                "below_noise_floor": result.below_floor,
                "ok": result.ok,
                "sizes": list(p.sizes),
                "seconds": result.times,
                "counters": result.counters,
            })
        failures += 0 if result.ok else 1

    if options.format == "json":
        print(json.dumps(documents, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
