# Memory that stays where you put it

Every server accumulates small piles of state that belong to *this* process and
this moment: the session someone is in the middle of, the idempotency key of a
payment that is still settling, a rate-limit counter, a batch of log records on
their way to a writer. None of it is worth a round trip to another service, and
all of it is dangerous if nobody bounds it — an in-memory store with no ceiling
is a memory leak that an unauthenticated caller gets to control.

Wreath ships two primitives for that, and only two.

- **[`wreath.kv`](../reference/kv.md)** — values under a key, a hard ceiling on
  how many, and a deadline.
- **[`wreath.queue`](../reference/queue.md)** — a hand-off between two pieces of
  code that must not wait for each other.

They exist as their own modules because the alternative had already happened:
the same three decisions — bound it, expire it, evict the right one — were
written out separately in the session table, the idempotency ledger, the JWKS
cache, the login-attempt counter, the log sink and the trace exporter. Written
six times, they were subtly different six times.

## A bounded table

```python
from wreath.kv import KV

sessions = KV(max_entries=10_000, ttl=1800.0)

sessions.set(session_id, payload)
payload = sessions.get(session_id)      # None once the deadline passes
```

That is the whole of it for a cache. Nothing runs in the background: entries
expire when they are next read, and the ceiling is enforced by dropping the
least recently *used* entry — not the oldest, which would throw away the hot key
and keep the cold one.

When the table is a ledger rather than a cache, the interesting operation is
`claim`:

```python
ledger = KV(max_entries=4096, ttl=86400.0)

if not ledger.claim(idempotency_key):
    raise Conflict("this request is already in flight")
```

`claim` is atomic against every other task on the loop, and it is atomic for a
boring reason: there is no `await` between the lookup and the write, so nothing
can interleave. It is the in-process counterpart of the single
`INSERT ... ON CONFLICT` that [`wreath.store.PostgresStore`](../reference/store.md)
claims with, and the two agree on the semantics that matter — including that
writing to a claimed key does not extend its window, so a slow holder cannot
hold its key forever.

**This is one worker's memory.** Behind a single worker or a sticky load
balancer that is the whole answer. Behind anything else it is a fast path in
front of a shared store, not a substitute for one, because a second worker's
memory knows none of it.

## A bounded hand-off

```python
from wreath.queue import Queue

records = Queue(capacity=4096)

if not records.offer(record):           # False: the queue was full
    ...                                  # and `records.dropped` went up

batch = records.drain(limit=256)         # one call, not a pop per item
```

The thing to understand about this queue is that it *drops*, and that this is
the feature. A queue in front of a slow consumer has three options — grow
without bound, make the producer wait, or lose something — and for a producer
that is in the middle of serving a request, the first two are worse. So it loses
the item and increments `dropped`, where an operator can see it.

If you would rather be told than lose the item, `put_nowait` raises `QueueFull`.
If you would rather keep the newest, `drop_oldest=True` evicts the front. What
the queue will not do is block, and if you need a producer to wait then you want
backpressure and a different primitive.

### When first-in-first-out is the wrong order

`Queue` is FIFO because that is what a hand-off usually wants. Three other
orderings ship for when it is not:

```python
from wreath.queue import PriorityQueue, Queue, RoundRobin

frames = Queue(capacity=64, lifo=True)      # newest first; stale frames are junk
work = PriorityQueue(capacity=4096)         # lowest number first, stable in ties
work.offer(job, priority=1)

tenants = RoundRobin(capacity=1024, max_lanes=256)
tenants.offer("tenant-a", job)              # a lane each, and everyone gets a turn
```

The one worth pausing on is `RoundRobin`, because it fixes a fairness bug that
a single queue does not look like it has. First-come-first-served *is* a
fairness rule — it is just the wrong one when the producers are tenants, since
whoever offers fastest gets the most service and a busy tenant starves the rest.
A lane each converts that into a turn each, and because `capacity` is per lane,
one tenant filling up drops that tenant's work and nobody else's.

`lifo=True` is the opposite: deliberately unfair. It serves the newest item, so
under a backlog what it serves is the freshest and the oldest may never be
served at all. Reach for it only where a stale item is worthless anyway.

### The consumer side

The consumer side can be synchronous or awaited:

```python
item = await records.get()
```

An item that is already queued resolves without suspending the caller at all —
no Future is built, and the event loop is not re-entered. Only an actually empty
queue parks a waiter. Offering is safe from any thread, which is the difference
between this and `wreath.kv`: the queue exists for hand-offs that cross threads,
and is locked accordingly, while the table is deliberately unsynchronised
because every one of its callers is on one loop.

### One surface, learned once

The three queues carry the same fourteen names, and the table shares the four
verbs that mean the same thing on it — `clear`, `get`, `peek`, `snapshot`. They
also construct the same way, as ordinary Python classes, so subclassing any of
them is unremarkable.

None of that was true at first. `clear` returned nothing on the table and a
count on the queue; `peek` existed on two of the four with different
signatures; the round-robin scheduler was missing half the surface; and the
queues configured themselves in `__new__`, which meant a subclass had to
override `__new__` and must never call `super().__init__` — a rule that lived
only in a comment, and one the table next door did not share. Each of those is
a thing a reader would have had to intern twice.

## Both are accelerated, and neither has to be

Both ship a C implementation and a pure-Python twin with identical behaviour,
selected automatically; `WREATH_PURE=1` forces the twin. The parity suite drives
the same operation sequences at both and compares after each step, which is how
a counter that disagreed on nine of thirty randomised trials was found while
every operation result matched.

The native table is a SwissTable — one byte of metadata per slot, scanned
thirty-two lanes at a time, with the scan itself living in `_native/simd.h`
beside the other dispatched kernels and cross-checked against a plain byte loop.

What the acceleration is worth is in each reference page, measured rather than
claimed, including the attempt that made one caller **slower** and what that
turned out to be.

Reference: [`wreath.kv`](../reference/kv.md), [`wreath.queue`](../reference/queue.md)
