"""Timer shootout: four O(1)/O(log n) timer designs across realistic workloads.

The reactor's per-connection deadlines (keep-alive, request timeout) are the
hottest churn in the loop. Which timer store actually wins depends entirely on
how the framework is *driven*, so this benchmarks four designs against workloads
shaped like real traffic -- fast APIs, idle connection pools, long-poll agents,
arbitrary app `call_later`, expiry storms, and a realistic blend.

Designs (all implemented here so the comparison is same-language and fair; the
absolute ns are Python-level, but the *relative* ordering is the algorithm):

  heap            binary heap + lazy cancel + compaction   (asyncio / uvloop / libuv)
  hashed_wheel    single-level hashed wheel + rounds        (Netty HashedWheelTimer)
  sized_wheel     single-level wheel sized so rounds==0     (bounded-range variant)
  hier_wheel      hierarchical cascading wheel              (Varghese-Lauck / old Linux)
  fifo_fixed      one FIFO list per fixed duration          (Redis / TCP keepalive)

References for constant factors:
  native_wheel    the C hashed wheel (wreath._native._reactor) -- native ceiling
  asyncio/uvloop  the real event-loop timer API             -- what we replace

The point is not "wheels beat heaps" (they do); it is *which design is lowest
CPU and memory pressure for the traffic our users and agents actually generate.*

REFERENCE ONLY -- not in the default `wreath-bench` battery. The production line is the
focused `benchmarks/bench_timing_wheel.py`. Run this by hand to
reproduce the shootout (`--lang native` for the fair field, `--lang python` for
the algorithm-only view).
"""

from __future__ import annotations

import argparse
import heapq
import json
import platform
import random
import statistics
import sys
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from time import perf_counter_ns
from typing import Any

try:
    from wreath._native import _reactor as _native
except ImportError:  # pragma: no cover
    _native = None
try:
    import uvloop
except ImportError:  # pragma: no cover
    uvloop = None


def _noop() -> None:
    return None


# 1. heap (asyncio's algorithm: heapq + lazy cancel + 50% compaction)
_MIN_SCHED, _CANCEL_FRAC = 100, 0.5


class _HeapNode:
    __slots__ = ("when", "seq", "cb", "cancelled")

    def __init__(self, when, seq):
        self.when = when
        self.seq = seq
        self.cb = _noop
        self.cancelled = False

    def __lt__(self, other):
        return (self.when, self.seq) < (other.when, other.seq)


class HeapTimers:
    name = "heap"

    def __init__(self):
        self._h: list[_HeapNode] = []
        self._cancelled = 0
        self._seq = 0
        self.now = 0.0

    def schedule(self, delay):
        self._seq += 1
        n = _HeapNode(self.now + delay, self._seq)
        heapq.heappush(self._h, n)
        return n

    def cancel(self, n):
        if not n.cancelled:
            n.cancelled = True
            self._cancelled += 1
            if len(self._h) > _MIN_SCHED and self._cancelled / len(self._h) > _CANCEL_FRAC:
                self._h = [x for x in self._h if not x.cancelled]
                heapq.heapify(self._h)
                self._cancelled = 0

    def advance(self, now):
        self.now = now
        fired = []
        h = self._h
        while h and h[0].when <= now:
            n = heapq.heappop(h)
            if n.cancelled:
                self._cancelled -= 1
            else:
                fired.append(n.cb)
        return fired


# 2/3. single-level hashed wheel (rounds) and sized wheel (no rounds)
class _WNode:
    __slots__ = ("cb", "rounds", "slot", "prev", "next", "cancelled")


class HashedWheel:
    name = "hashed_wheel"

    def __init__(self, resolution=0.001, slots=512):
        self.res = resolution
        self.n = slots
        self.slots: list[Any] = [None] * slots
        self.cursor = 0

    def schedule(self, delay):
        ticks = int(delay / self.res) or 1
        dl = self.cursor + ticks
        slot = dl % self.n
        node = _WNode()
        node.cb = _noop
        node.rounds = ticks // self.n
        node.slot = slot
        node.cancelled = False
        node.prev = None
        node.next = self.slots[slot]
        if node.next is not None:
            node.next.prev = node
        self.slots[slot] = node
        return node

    def cancel(self, node):
        if node.cancelled:
            return
        node.cancelled = True
        if node.prev is not None:
            node.prev.next = node.next
        else:
            self.slots[node.slot] = node.next
        if node.next is not None:
            node.next.prev = node.prev

    def advance(self, now):
        fired = []
        target = int(now / self.res)
        while self.cursor < target:
            self.cursor += 1
            slot = self.cursor % self.n
            node = self.slots[slot]
            while node is not None:
                nxt = node.next
                if not node.cancelled:
                    if node.rounds > 0:
                        node.rounds -= 1
                    else:
                        if node.prev is not None:
                            node.prev.next = node.next
                        else:
                            self.slots[slot] = node.next
                        if node.next is not None:
                            node.next.prev = node.prev
                        node.cancelled = True
                        fired.append(node.cb)
                node = nxt
        return fired


class SizedWheel(HashedWheel):
    name = "sized_wheel"
    # Enough slots that every timeout the workloads use lands in one rotation
    # (rounds==0): removes the per-rotation revisit cost of the hashed wheel.

    def __init__(self, resolution=0.001, max_delay=600.0):
        super().__init__(resolution=resolution, slots=int(max_delay / resolution) + 2)


# 4. hierarchical cascading wheel (Varghese-Lauck). S power of two.
class _HNode:
    __slots__ = ("cb", "deadline", "prev", "next", "head_list", "slot", "cancelled")


class HierWheel:
    name = "hier_wheel"
    S = 64
    SHIFT = 6
    LEVELS = 4  # 64^4 ticks ≈ 4.6 h at 1 ms

    def __init__(self, resolution=0.001):
        self.res = resolution
        self.cursor = 0
        self.levels = [[None] * self.S for _ in range(self.LEVELS)]
        self._spans = [self.S ** (i + 1) for i in range(self.LEVELS)]

    def _bucket(self, deadline):
        rem = deadline - self.cursor
        if rem < 0:
            rem = 0
        mask = self.S - 1
        for lvl in range(self.LEVELS):
            if rem < self._spans[lvl] or lvl == self.LEVELS - 1:
                return lvl, (deadline >> (lvl * self.SHIFT)) & mask
        return self.LEVELS - 1, (deadline >> ((self.LEVELS - 1) * self.SHIFT)) & mask

    def _insert(self, node):
        lvl, slot = self._bucket(node.deadline)
        lst = self.levels[lvl]
        node.head_list = lst
        node.slot = slot  # type: ignore[attr-defined]
        node.prev = None
        node.next = lst[slot]
        if node.next is not None:
            node.next.prev = node
        lst[slot] = node

    def schedule(self, delay):
        ticks = int(delay / self.res) or 1
        node = _HNode()
        node.cb = _noop
        node.deadline = self.cursor + ticks
        node.cancelled = False
        self._insert(node)
        return node

    def cancel(self, node):
        if node.cancelled:
            return
        node.cancelled = True
        lst = node.head_list
        slot = node.slot  # type: ignore[attr-defined]
        if node.prev is not None:
            node.prev.next = node.next
        else:
            lst[slot] = node.next
        if node.next is not None:
            node.next.prev = node.prev

    def _drain(self, lst, slot):
        node = lst[slot]
        lst[slot] = None
        return node

    def advance(self, now):
        fired = []
        target = int(now / self.res)
        while self.cursor < target:
            self.cursor += 1
            # cascade higher levels when a lower level wraps
            if (self.cursor & (self.S - 1)) == 0:
                lvl = 1
                while lvl < self.LEVELS:
                    idx = (self.cursor >> (lvl * self.SHIFT)) & (self.S - 1)
                    node = self._drain(self.levels[lvl], idx)
                    while node is not None:
                        nxt = node.next
                        if not node.cancelled:
                            node.prev = node.next = None
                            self._insert(node)
                        node = nxt
                    if idx != 0:
                        break
                    lvl += 1
            # fire level-0 slot
            slot = self.cursor & (self.S - 1)
            node = self._drain(self.levels[0], slot)
            while node is not None:
                nxt = node.next
                if not node.cancelled:
                    node.cancelled = True
                    fired.append(node.cb)
                node = nxt
        return fired


# 5. FIFO list per fixed duration (Redis / TCP keepalive)
class _FNode:
    __slots__ = ("cb", "deadline", "prev", "next", "bucket", "cancelled")


class _Bucket:
    __slots__ = ("head", "tail")

    def __init__(self):
        self.head = None
        self.tail = None


class FixedList:
    name = "fifo_fixed"

    def __init__(self, quantum=0.001):
        self.q = quantum
        self.buckets: dict[int, _Bucket] = {}
        self.now = 0.0

    def schedule(self, delay):
        key = int(delay / self.q)
        b = self.buckets.get(key)
        if b is None:
            b = _Bucket()
            self.buckets[key] = b
        node = _FNode()
        node.cb = _noop
        node.deadline = self.now + delay
        node.bucket = b
        node.cancelled = False
        node.prev = b.tail
        node.next = None
        if b.tail is not None:
            b.tail.next = node
        else:
            b.head = node
        b.tail = node
        return node

    def cancel(self, node):
        if node.cancelled:
            return
        node.cancelled = True
        b = node.bucket
        if node.prev is not None:
            node.prev.next = node.next
        else:
            b.head = node.next
        if node.next is not None:
            node.next.prev = node.prev
        else:
            b.tail = node.prev

    def advance(self, now):
        self.now = now
        fired = []
        for b in self.buckets.values():
            node = b.head
            while node is not None and node.deadline <= now:
                nxt = node.next
                fired.append(node.cb)
                node.cancelled = True
                node = nxt
            b.head = node
            if node is not None:
                node.prev = None
            else:
                b.tail = None
        return fired


# Native + real-loop adapters
class _WheelStore:
    """Adapter for TimingWheel (handle.cancel())."""

    def __init__(self, store):
        self.s = store

    def schedule(self, delay):
        return self.s.schedule(delay, _noop)

    @staticmethod
    def cancel(h):
        h.cancel()

    def advance(self, now):
        return self.s.advance(now)


class _NativeStore:
    """Adapter for Heap/Hier/Fifo stores (store.cancel(handle))."""

    def __init__(self, store):
        self.s = store

    def schedule(self, delay):
        return self.s.schedule(delay, _noop)

    def cancel(self, h):
        self.s.cancel(h)

    def advance(self, now):
        return self.s.advance(now)


def _wheel(slots):
    return _WheelStore(_native.TimingWheel(resolution=0.001, slots=slots, base=0.0))


def native_arms():
    """All five designs, implemented natively in C (apples-to-apples)."""
    return {
        "n_heap": lambda: _NativeStore(_native.HeapStore()),
        "n_hashed": lambda: _wheel(4096),  # the ship default: sized for the timeout range
        "n_sized": lambda: _wheel(600_002),
        "n_hier": lambda: _NativeStore(_native.HierStore(resolution=0.001)),
        "n_fifo": lambda: _NativeStore(_native.FifoStore(quantum=0.001)),
    }


def python_arms():
    return {
        "heap": HeapTimers,
        "hashed_wheel": HashedWheel,
        "sized_wheel": SizedWheel,
        "hier_wheel": HierWheel,
        "fifo_fixed": FixedList,
    }


class LoopArm:
    def __init__(self, name, loop):
        self.name = name
        self.loop = loop

    def schedule(self, delay):
        return self.loop.call_later(delay, _noop)

    @staticmethod
    def cancel(h):
        h.cancel()

    def close(self):
        self.loop.close()


# Workloads. Each returns median ns per unit of work for one arm factory.
def _median_ns(fn: Callable[[], int], work_units: int, trials: int) -> float:
    samples = []
    for _ in range(trials):
        started = perf_counter_ns()
        fn()
        samples.append((perf_counter_ns() - started) / work_units)
    return statistics.median(samples)


def w_fast_api(make, iters, trials):
    """Fast API: each request resets keep-alive(5s) + arms request(30s), then
    both are cancelled on completion. 2 durations, tiny pending set."""

    def run():
        arm = make()
        for _ in range(iters):
            ka = arm.schedule(5.0)
            rq = arm.schedule(30.0)
            arm.cancel(rq)
            arm.cancel(ka)

    return _median_ns(run, iters, trials)  # ns per request (2 sched + 2 cancel)


def w_idle_pool(make, k, iters, trials):
    """Idle connection pool: K conns each hold a keep-alive(5s) timer; each op a
    request lands on a random conn -> cancel+reschedule its timer (reset)."""

    def run():
        arm = make()
        pinned = [arm.schedule(5.0) for _ in range(k)]
        rng = random.Random(1)
        for _ in range(iters):
            i = rng.randrange(k)
            arm.cancel(pinned[i])
            pinned[i] = arm.schedule(5.0)
        del pinned

    return _median_ns(run, iters, trials)


def w_agents_longpoll(make, iters, trials):
    """Agents / SSE / long-poll: far-future request timeouts (300s) + keepalive
    (60s); schedule then cancel. Stresses far-future handling (wheel rounds)."""

    def run():
        arm = make()
        for _ in range(iters):
            t = arm.schedule(300.0)
            k = arm.schedule(60.0)
            arm.cancel(t)
            arm.cancel(k)

    return _median_ns(run, iters, trials)


def w_diverse_delays(make, iters, trials):
    """Arbitrary app call_later: retries/debounce/rate-limit windows -> a wide
    spread of distinct durations. Stresses fifo_fixed's per-duration buckets."""
    rng = random.Random(7)
    delays = [rng.uniform(0.05, 120.0) for _ in range(4096)]

    def run():
        arm = make()
        j = 0
        for _ in range(iters):
            h = arm.schedule(delays[j % len(delays)])
            arm.cancel(h)
            j += 1

    return _median_ns(run, iters, trials)


def w_expiry_heavy(make, n, trials):
    """Timeouts actually fire: schedule N over a horizon, advance to expire all."""

    def run():
        arm = make()
        for i in range(n):
            arm.schedule((i % 500 + 1) * 0.001)
        arm.advance(1.0)

    return _median_ns(run, n, trials)


def w_mixed_realistic(make, iters, trials):
    """A realistic blend: 78% fixed-duration keep-alive/request reset+cancel,
    17% arbitrary app call_later, 5% timers that actually fire."""
    rng = random.Random(11)
    app_delays = [rng.uniform(0.1, 90.0) for _ in range(2048)]

    def run():
        arm = make()
        pending = []
        now = 0.0
        for i in range(iters):
            r = rng.random()
            if r < 0.78:
                ka = arm.schedule(5.0)
                rq = arm.schedule(30.0)
                arm.cancel(rq)
                arm.cancel(ka)
            elif r < 0.95:
                h = arm.schedule(app_delays[i % len(app_delays)])
                pending.append(h)
                if len(pending) > 64:
                    arm.cancel(pending.pop(0))
            else:
                arm.schedule(0.002)
                now += 0.001
                arm.advance(now)

    return _median_ns(run, iters, trials)


def w_ultra_mixed(make, iters, trials):
    """Maximal diversity in one stream: fixed-duration requests (arm+cancel),
    idle keep-alive resets, agent long-polls (far future), arbitrary app
    call_later, and expiry ticks -- interleaved with a fluctuating pending set
    and durations spanning ms → minutes (6 orders of magnitude)."""
    rng = random.Random(2027)
    app_delays = [rng.uniform(0.05, 120.0) for _ in range(8192)]

    def run():
        arm = make()
        live: list = []
        now = 0.0
        j = 0
        for _ in range(iters):
            r = rng.random()
            if r < 0.40:  # fast request: arm + cancel both
                ka = arm.schedule(5.0)
                rq = arm.schedule(30.0)
                arm.cancel(rq)
                arm.cancel(ka)
            elif r < 0.60:  # idle keep-alive reset
                if live:
                    idx = rng.randrange(len(live))
                    arm.cancel(live[idx])
                    live[idx] = arm.schedule(5.0)
                else:
                    live.append(arm.schedule(5.0))
            elif r < 0.75:  # agent long-poll (far future)
                live.append(arm.schedule(rng.uniform(60.0, 600.0)))
            elif r < 0.90:  # arbitrary app call_later
                live.append(arm.schedule(app_delays[j % len(app_delays)]))
                j += 1
            else:  # clock tick: advance + fire
                now += rng.uniform(0.001, 0.05)
                arm.advance(now)
            if len(live) > 20_000:  # bound the working set
                arm.cancel(live.pop(0))

    return _median_ns(run, iters, trials)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=60_000)
    parser.add_argument("--trials", type=int, default=7)
    parser.add_argument("--label", default="unlabelled")
    parser.add_argument(
        "--lang",
        choices=["native", "python"],
        default="native",
        help="native = all five designs in C (fair); python = pure algorithms",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if _native is None:
        raise SystemExit("native wreath._native._reactor is required")

    import asyncio

    it, tr = args.iterations, args.trials

    all_arms = native_arms() if args.lang == "native" else python_arms()
    results: dict[str, dict[str, float]] = {"_lang": args.lang}

    def measure(workload_name, run_for_arm, arms):
        row = {}
        for name, make in arms.items():
            row[name] = run_for_arm(make)
        results[workload_name] = row

    measure("fast_api", lambda make: w_fast_api(make, it, tr), all_arms)
    measure("agents_longpoll", lambda make: w_agents_longpoll(make, it, tr), all_arms)
    measure("diverse_delays", lambda make: w_diverse_delays(make, it, tr), all_arms)
    measure("expiry_heavy", lambda make: w_expiry_heavy(make, it, tr), all_arms)
    measure("mixed_realistic", lambda make: w_mixed_realistic(make, it, tr), all_arms)
    measure("ultra_mixed", lambda make: w_ultra_mixed(make, it, tr), all_arms)

    # idle_pool across pool sizes, up to a million live connections.
    idle = {}
    for k in (1_000, 10_000, 100_000, 1_000_000):
        idle[k] = {name: w_idle_pool(make, k, it // 2, tr) for name, make in all_arms.items()}
    results["idle_pool"] = idle

    # Real event-loop API (bounded set, per-call overhead incl. allocation).
    def _asyncio():
        return LoopArm("asyncio", asyncio.new_event_loop())

    def _uvloop():
        return LoopArm("uvloop", uvloop.new_event_loop())

    # Real call_later+cancel overhead: every native design vs the real loops,
    # all on a bounded pending set so an un-drained loop heap can't distort it.
    api = {}
    api_arms = dict(all_arms)
    api_arms["asyncio"] = _asyncio
    if uvloop is not None:
        api_arms["uvloop"] = _uvloop
    for name, make in api_arms.items():

        def run(make=make):
            done = 0
            while done < it // 2:
                s = make()
                pinned = [s.schedule(3600.0) for _ in range(64)]
                n = min(512, it // 2 - done)
                for _ in range(n):
                    s.cancel(s.schedule(0.010))
                done += n
                if hasattr(s, "close"):
                    s.close()
                del pinned

        api[name] = _median_ns(run, it // 2, tr)
    results["api_overhead"] = api

    # Memory: peak bytes to hold K live timers.
    memory = {}
    for name, make in all_arms.items():
        tracemalloc.start()
        tracemalloc.reset_peak()
        arm = make()
        pinned = [arm.schedule(300.0) for _ in range(10_000)]
        _c, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        memory[name] = peak / 10_000
        del pinned, arm

    document = {
        "tool": "benchmarks.bench_timer_shootout",
        "schema_version": 1,
        "label": args.label,
        "python": sys.version,
        "platform": platform.platform(),
        "uvloop": getattr(uvloop, "__version__", None),
        "iterations": it,
        "trials": tr,
        "results": results,
        "memory_bytes_per_timer": memory,
    }
    text = json.dumps(document, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
        print(f"wrote {args.output}")
    else:
        _print_summary(results, memory)


def _print_summary(results, memory):
    arms = list(results["fast_api"].keys())
    simple = [
        "fast_api",
        "agents_longpoll",
        "diverse_delays",
        "expiry_heavy",
        "mixed_realistic",
        "ultra_mixed",
    ]
    lang = results.get("_lang", "?")
    print(f"\n[{lang}] ns per work-unit (median; lower is better). winner starred.")
    print("  workload".ljust(20) + "".join(a.ljust(12) for a in arms))
    for w in simple:
        row = results[w]
        best = min((row[a] for a in arms if a in row), default=None)
        cells = f"  {w}".ljust(20)
        for a in arms:
            v = row.get(a)
            mark = "*" if v == best else " "
            cells += ((f"{v:.0f}{mark}") if v is not None else "-").ljust(14)
        print(cells)
    print("\nidle_pool (ns per keep-alive reset with K live conns)")
    for k, row in results["idle_pool"].items():
        best = min(row[a] for a in arms if a in row)
        cells = f"  K={k}".ljust(20)
        for a in arms:
            v = row.get(a)
            mark = "*" if v == best else " "
            cells += (f"{v:.0f}{mark}").ljust(14)
        print(cells)
    print("\nreal event-loop API (ns per call_later+cancel):")
    for name, v in results["api_overhead"].items():
        print(f"  {name.ljust(16)} {v:.0f}")
    print("\nmemory (bytes per live timer):")
    for name in arms:
        if name in memory:
            print(f"  {name.ljust(16)} {memory[name]:.0f}")


if __name__ == "__main__":
    main()
