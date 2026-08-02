# `wreath.queue`

A bounded in-process queue that cannot grow without limit and says out loud when
it has dropped something. A log record going from the projector thread to the
writer, a finished trace going to the exporter, a notification waiting for a
client's stream: all of them want the same queue, and this is it.

Reach for it whenever a producer must not be made to wait on a consumer — which
is most hand-offs inside a server, because the producer is usually serving a
request. Reach for `asyncio.Queue` instead when you want the opposite: a producer
that *should* block until the consumer catches up.

## What it costs

Measured 2026-08-01 on battery power, twenty-one interleaved rounds against an
A/A control at 0.1% of baseline, with the implementation being replaced measured
as an arm in the same run.

| operation | before | after |
| --- | --- | --- |
| `BoundedLogQueue.offer` → `Queue.offer` | 0.36µs | 0.08µs |
| hand-rolled bounded heap → `PriorityQueue.offer`+`get` | 0.62µs | 0.15µs |

Bare `heapq.heappush`/`heappop` with a stability tiebreak — what anybody would
otherwise write — is 0.23µs on the same run, so the priority queue is faster
than the unbounded, uncounted, unlocked thing it replaces, not merely faster
than the wrapper. It never builds the `(priority, sequence, item)` tuple that a
Python heap needs.

The ring was never the problem. `collections.deque(maxlen=n).append` measures
0.02µs and nothing here beats it — what cost was everything wrapped *around* it,
which is to say a lock acquire, two counter increments, a length test and a
Python method call for each item. There were two copies of that wrapper in the
tree, in `_logsink` and `_otlp`, near-identical to each other. They are one
primitive now, and the bookkeeping is one C call.

## Things worth knowing before you use it

**Loss is counted, never silent.** `offer` returns `False` and increments
`dropped` when the queue is full. That is the only policy compatible with the
promise a bounded hand-off makes: bounded memory, bounded latency, and a number
an operator can look at when a consumer falls behind. `drop_oldest=True` evicts
the front instead, for a consumer that would rather have the newest — and still
counts, because something was lost either way.

**`put_nowait` is the other posture**: raise `QueueFull` rather than lose the
item, for a producer that would rather be told.

**There is no blocking `put`.** This queue is deliberately lossy under pressure;
if you need a producer to wait, you need backpressure, and that is a different
primitive. It is why the ASGI receive queues in `server.py` and `testing.py` are
still `asyncio.Queue`.

**It is safe to offer to from any thread**, unlike [`wreath.kv`](kv.md). `await
get()` may only be used from one event loop — the loop the first waiter parks on
— and offering from another thread wakes that loop through
`call_soon_threadsafe`.

**`await get()` does not suspend when an item is already there.** It hands back
an awaitable that is already resolved, so no Future is built and the event loop
is not re-entered; only a genuinely empty queue parks a waiter. The waiting half
is written in Python on purpose, because parking a waiter, waking it from
another thread and surviving cancellation is delicate enough to belong where it
can be read.

**`snapshot()` reads the backlog without consuming it**, which is what a
diagnostic wants; `drain()` answers the same question by destroying it.

## Choosing a discipline

Four orderings ship, and they differ in who waits rather than in how fast they
are.

**FIFO** — `Queue(capacity)`. The default, and right for almost every hand-off:
the oldest item has been waiting longest, so serving it first is what bounds the
worst case.

**LIFO** — `Queue(capacity, lifo=True)`. A stack. Under a backlog it serves the
*newest* item, so what it serves is the freshest — at the cost of the oldest
item possibly never being served at all. That trade is right where a stale item
is worthless anyway (a live metric, a frame of a video feed) and wrong wherever
every item must eventually be handled. It is a latency choice, not a fairness
one; a LIFO is deliberately unfair.

**Priority** — `PriorityQueue(capacity)`. Lowest number first, and items sharing
a priority come out in the order they went in. That stability is worth more than
it sounds: most items in a real workload share a priority, so without it the
common case is unordered and a test that pins ordering is flaky rather than
wrong. The payload is never compared, so anything is queueable — including
objects with no ordering, and objects whose `__lt__` would run arbitrary Python
in the middle of a sift. `drop_lowest=True` lets an urgent arrival displace a
queued low-priority item instead of being refused behind it.

All four share one surface: `offer`, `get`, `get_nowait`, `put_nowait`, `peek`,
`drain`, `snapshot`, `clear`, `close`, `closed`, `capacity`, `offered`,
`dropped`, `len()`. Learn it once. What differs between them is only what
genuinely differs — `lifo` and `drop_oldest` on the plain queue, `drop_lowest`
on the priority one, `lanes` on the scheduler. `wreath.kv` shares the four verbs
that mean the same thing on a table: `clear`, `get`, `peek`, `snapshot`.

The one place a shared name means something different is `RoundRobin.capacity`,
which is **per lane** rather than in total. That is the point of it, so it is
called out here and in the tests rather than smoothed over.

**Round robin** — `RoundRobin(capacity, max_lanes=...)`. A lane per producer and
a rotating cursor. One shared queue is fair only in the first-come-first-served
sense, which is exactly the wrong fairness when the producers are tenants:
whoever offers fastest gets the most service. `capacity` is **per lane**, so one
lane filling up drops that lane's work and nobody else's, and `max_lanes` is
required because lanes are created on demand — a lane name that arrives from a
request would otherwise be an unbounded allocation a caller controls. The
rotation itself is plain Python; it moves a cursor over a list once per item,
which is nowhere near the cost of the queue operation it wraps. The waiting half
is not rewritten either — it mixes in the same `Awaiting` the other two use, so
cancellation and cross-thread wake-ups are solved once for all three.

::: wreath.queue
