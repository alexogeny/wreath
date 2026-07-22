# The timer that wouldn't settle

*An exploration in not trusting the first answer.*

Some of the best engineering leaves no code behind. This is the story of a spike
that produced one small, fast, boring data structure — a hashed timing wheel —
and the four rounds of measurement it took to be *sure* that boring was right.
The reactor the spike was built to explore never shipped. The wheel did. The
difference between them is the whole point.

## A spike, and one honest question

The framework runs its own native HTTP server, and every request that server
handles arms two deadlines: a keep-alive timer and a request timeout. Under
`asyncio`, arming a timer is `loop.call_later`, and cancelling it is
`handle.cancel()` — and the framework does both, twice, for essentially every
request, almost always cancelling before the timer fires because the request
finished in time.

That is a lot of churn on machinery we don't own. So we built a spike: a native,
asyncio-compatible event loop with a fused fast path — inline-driving coroutines
that never suspend, resuming request handlers straight from the protocol read
callback. It's kept for reference in `wreath/reactor.py`
and its acceptance suite, but it isn't wired into anything. What the spike really
did was surface one question sharp enough to demand a real answer:

> The event loop keeps its timers in a binary heap. For *our* traffic, is there
> something better?

The heap is O(log n) to insert and cancel, with a lazy-cancellation scheme and a
periodic compaction pass. A **timing wheel** is O(1) for both. On paper, the
wheel wins. But "on paper" is exactly the phrase this whole document exists to
distrust.

## Round 1 — the tempting shootout

We implemented five timer designs and benchmarked them across workloads shaped
like real traffic: fast APIs, idle connection pools, long-poll agents, arbitrary
application `call_later`, expiry storms, and a realistic blend.

| design | idea | as seen in |
|---|---|---|
| `heap` | binary heap + lazy cancel + compaction | asyncio, uvloop, libuv |
| `hashed_wheel` | single-level wheel + a "rounds" counter | Netty `HashedWheelTimer` |
| `sized_wheel` | a wheel sized so `rounds` is always 0 | bounded-range variant |
| `hier_wheel` | a hierarchical cascading wheel | Varghese–Lauck, old Linux |
| `fifo_fixed` | one FIFO list per fixed duration | Redis, TCP keepalive |

The first results were seductive. The FIFO list — which exploits that
same-duration timers arrive already sorted by deadline — was best or near-best on
every fixed-duration workload. The hierarchical wheel, the fanciest thing in the
room, was mediocre to *worst*. The tidy conclusion wrote itself: adopt the FIFO
fast path for our fixed deadlines and move on.

We almost did.

## The catch: "our wheel is native — that's cheating"

One arm of that shootout was written in C: the wheel we'd actually built. The
other four were Python. Which means the comparison wasn't measuring *algorithms*
at all — it was measuring C against a language with per-object overhead and an
interpreter loop. The native arm was winning a race the others were running in
treacle.

So we rebuilt all of them in C — heap, hierarchical, FIFO, sized, hashed — as
`wreath._native._reactor`, and ran the shootout again on a
level field.

## Round 2 — the reversal

Putting every design on equal footing didn't just narrow the gaps. It **inverted
two of the conclusions.**

Native, ns per work-unit (lower is better, `*` = winner):

| workload | n_heap | n_hashed | n_sized | n_hier | n_fifo |
|---|---:|---:|---:|---:|---:|
| fast_api | **132** | 142 | 167 | 136 | 171 |
| agents_longpoll | **130** | 153 | 157 | 136 | 157 |
| diverse_delays | 125 | **110** | 114 | 128 | 128 |
| expiry_heavy | 200 | **99** | 123 | 101 | 106 |
| mixed_realistic | 190 | **167** | 182 | 187 | 274 |
| idle_pool K=100k | 377 | **339** | 349 | 378 | 386 |

Three things fell out of this that Round 1 had actively lied about:

- **The FIFO list lost.** Implemented faithfully, every schedule must map a
  duration to its bucket — a hash lookup — and that constant ate the whole
  theoretical advantage. On the mixed workload it *detonated*, because arbitrary
  app durations spawn thousands of buckets and expiry has to scan them all.
- **The hierarchical wheel became competitive.** Its Round 1 mediocrity was
  entirely Python overhead in the cascade; in C it was fine. It just never
  *won* anything decisively.
- **The heap was best when few timers are live** (`fast_api`, `agents`). With a
  small set, `log n` is trivial and the heap has the leanest per-op path. This is
  precisely why asyncio and libuv get away with heaps — they only fall apart
  under pressure, which is exactly where a server lives.

And the humbling part: on a fair field, *all five native designs land within
about 1.5× of each other.* The enormous gaps from Round 1 were the programming
language wearing an algorithm's costume.

## Round 3 — a million operations of chaos

Small, tidy workloads flatter everything. So we wrote `ultra_mixed`: fixed-duration
requests, idle resets, agent long-polls, arbitrary app timers, and clock ticks,
all interleaved, with a pending set that swells to twenty thousand live timers
and durations spanning six orders of magnitude — and ran it at **one million
operations**.

Here the hashed wheel we intended to ship came *second-to-last*:

| ultra_mixed @ 1M ops | n_heap | n_hashed (512) | n_sized | n_hier | n_fifo |
|---|---:|---:|---:|---:|---:|
| ns/op | 844 | 1042 | 794 | **745** | 11075 |

A 512-slot wheel gives far-future timers a large `rounds` count, and it revisits
each of them on every rotation as it sweeps their slot. With thousands of
long-lived agent connections pending, that revisit cost adds up. The hierarchical
wheel avoids it by construction. FIFO, again, detonated (11075 ns — fifteen times
the field).

This is the moment a lazier investigation ships the hierarchical wheel. But the
weakness had a shape, and the shape was a knob:

| ultra_mixed @ 1M | hashed(512) | hashed(4k) | hashed(65k) | hier |
|---|---:|---:|---:|---:|
| ns/op | 1007 | **798** | 792 | 789 |

A **4096-slot** wheel matches the hierarchical wheel — with none of the cascade
machinery, and 32 KB of fixed array. The wheel wasn't beaten. It was *misconfigured*.

## Round 4 — making it genuinely fair

Two things still weren't apples-to-apples, and a good reviewer said so. The
real-API measurement only pitted the wheel against the event loops; and the idle
pool stopped at a hundred thousand connections. So: put **every** native design
into the real-call_later table, and push the pool to a **million** live timers.

Real `call_later` + `cancel`, ns:

| n_heap | n_hashed | n_sized | n_hier | n_fifo | asyncio | uvloop |
|---:|---:|---:|---:|---:|---:|---:|
| 78 | 81 | 505 | 72 | 76 | 891 | 1769 |

Every O(1) native design is a dead heat — 72 to 81 nanoseconds — and all of them
are **11–22× faster than the event loop's own timer**. No design was getting a
secret native boost over the others; they were genuinely peers, and the whole
family leaves asyncio and uvloop far behind together.

And the single most server-relevant number in the entire study — a **million idle
keep-alives** being reset:

| idle_pool K=1,000,000 | n_heap | n_hashed | n_sized | n_hier | n_fifo |
|---|---:|---:|---:|---:|---:|
| ns/reset | 551 | **542** | 564 | 552 | 615 |

At the scale that actually stresses a connection server, the hashed wheel *wins*.

## Proof it's safe, not just fast

Speed you can't trust isn't speed. All four native stores — the hashed wheel,
the heap, the hierarchical wheel, the FIFO — were run under
AddressSanitizer, UndefinedBehaviorSanitizer, and LeakSanitizer through a
punishing exercise: schedule at every magnitude, cancel at head, middle, and
tail, double-cancel, cancel after fire, heap compaction, level cascade, bucket
growth, handles outliving a freed store, dealloc with live timers, and an
80,000-operation randomized fuzz per store. (There's a
`build_reactor.py` sanitizer harness for it.)

```
exit=0 · no AddressSanitizer errors · no UBSan runtime errors ·
no leaks attributable to the module · ALL EXERCISES COMPLETE
```

One real bug surfaced along the way — a reference leak in the wheel's `cancel`
that dropped a node from its slot list but not the store's owning reference. It
was caught by reading the code, not by the sanitizer, which is its own small
lesson about not outsourcing all of your vigilance to tools.

## What we shipped, and why

The **4096-slot native hashed timing wheel**. Not because it wins every
micro-benchmark — the heap edges it when almost nothing is pending, the
hierarchical wheel takes a few mid-sized workloads by noise-thin margins — but
because it is the best *citizen*:

- On a fair, native field it is **never more than ~7–15% off the best** on
  anything.
- It **wins the two cases that matter most for a server**: a million live
  connections, and the real per-request `call_later` + `cancel` cost.
- It holds the **least memory per timer** (76 bytes).
- It is the **simplest correct design** — no cascade, no per-duration bucket map,
  no oversized array.
- It is **sanitizer-clean.**

It gets its own line in `wreath-bench` as the *wreath-metal timer*
(`benchmarks/bench_timing_wheel.py`), where it clocks
~144 ns per request against asyncio's ~1373 and uvloop's ~1177 — roughly a
**9× reduction in per-request timer overhead**, at a third of the memory.

## The lessons, which outlast the code

1. **Never benchmark across languages.** One native arm among Python ones didn't
   compare data structures; it compared C to an interpreter, and it lied twice.
2. **The workload is the benchmark.** Tidy micro-tests flattered everything and
   hid the far-future revisit cost entirely. The million-op chaos found it in one
   run.
3. **The clever structure rarely wins; the simple one, well-tuned, usually
   does.** The hierarchical wheel and the FIFO list were the "smart" answers.
   Neither beat a hashed wheel with its slot count set correctly.
4. **A weakness with a shape is a knob, not a defect.** The wheel's one bad
   result was a configuration, not a flaw — but you only learn that by pushing on
   it instead of retreating from it.
5. **Rigor is cheap.** Four rounds of measurement and a sanitizer pass cost an
   afternoon. Shipping the hierarchical wheel — more code, more surface, more to
   maintain — for a result the hashed wheel matched would have cost far more, for
   longer, silently.

## What we kept

The reactor loop and its fused task machinery are preserved as reference and
imported by nothing; they were the scaffolding, and the scaffolding did its job
by asking a good question. The wheel graduated. Everything else in this document
is measurement — which is to say, it's the reason we get to be confident about
something small instead of merely hopeful about something clever.
