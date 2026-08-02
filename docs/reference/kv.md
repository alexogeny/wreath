# `wreath.kv`

A bounded key/value table that lives in one worker's memory: values under a key,
a hard ceiling on how many, and a deadline. It is the small thing a response
cache, a session table, an idempotency ledger and a claim ledger all turned out
to need, and it is written here once so that each of them does not re-derive it.

Reach for it when you want a cache you can *size* — one that will not grow
without bound, that expires without a background thread, and that has no
external service to operate. Reach for
[`SnapshotCache`](cache.md) instead when the data is read-mostly reference data
you replace wholesale, and for
[`wreath.store.PostgresStore`](store.md) when the answer has to be the same on
every worker.

## What it costs

Measured 2026-08-01 on battery power, twenty-one interleaved rounds against an
A/A control that came in at 0.1% of the baseline. The implementations being
replaced are measured as arms in the *same* run rather than compared against
numbers recorded earlier, because two runs minutes apart on this machine are not
comparable.

| operation | before | after |
| --- | --- | --- |
| `BoundedCache.get` | 0.42µs | 0.23µs |
| `BoundedCache.set` | 0.44µs | 0.23µs |
| `MemoryStore.read` | 0.57µs | 0.22µs |
| `MemoryStore.claim` | 0.60µs | 0.22µs |
| `KV.get`, called directly | — | 0.16µs |

The bare `dict.get` these are measured against is 0.11µs on the same run, so the
table now costs roughly what one dictionary lookup and a recency update cost,
which is about the floor for something that also bounds and expires.

Two numbers in that table are worth more than the ratio. The first is that a
native table was never theoretical headroom: `_core.TokenBucket` — a table that
does strictly *more* per call — was already answering faster than the pure cache
next door, which is what reopened a decision `cache.py` had closed. The second
is that the first attempt at this made `BoundedCache` **21% slower** despite the
table underneath being twice as fast, because a keyword argument forces a C
method off the vectorcall path and into building an argument tuple and a keyword
dict per call. Both the wrapper and the C methods are written around that, and
the comments in `_native/kv.c` say so where somebody would otherwise undo it.

## Things worth knowing before you use it

**It is not synchronised.** Every caller is on one event loop, exactly as the
`BoundedCache` it replaces was, and a lock would charge every reader for a race
no reader has. Use [`wreath.queue`](queue.md) for the hand-offs that genuinely
cross threads.

**`len()` counts what the table will still return**, not what it happens to be
holding — an expired entry is one the table refuses, so counting it would make
`len()` a measure of debris. It reads the real clock, so a table written against
an injected test clock will report zero; ask `count(now=...)` instead, which
exists for precisely that.

**The clock is resolved in three steps**, and the reason there are three is
worth stating rather than leaving to be discovered. An explicit `now=` wins;
otherwise a `clock` injected at construction; otherwise the monotonic clock,
read in C.

The per-call `now=` is the fast path and exists because of a measurement: a
table that had to call a Python clock per operation paid about as much for the
time as for the lookup it was timing. The per-instance `clock=` is the *simple*
path and is what a test wants, because threading a time through every call is
noise in a test that is about expiry. Neither is redundant, and neither is
free -- so both are here, with the default being the one that costs nothing.

The consequence to remember: `len()` takes no argument, so it answers against
whichever clock the table holds. A table built with an injected test clock and
no `now` reports what that clock says; `count(now=...)` is how to ask at an
arbitrary time.

**Eviction is least recently used, and only `get` counts as a use.** `peek` is
the read that does not disturb what it is reading, which is what a membership
test or a diagnostic wants.

**Two bounds, not one.** `max_bytes` caps the summed `cost` the caller declares
per entry, alongside `max_entries`. The cost is the caller's number because only
the caller knows what a value really holds -- a query plan that references shared
registry metadata is not charged for it, and no generic sizing function could
know that. An entry whose cost alone exceeds the budget is evicted again
immediately, leaving the table empty rather than over its bound.

**`track_evictions=True`** records what was dropped, for `take_evicted()`. Only
*evictions* appear -- not expiries, not deletes -- because those are things the
caller already knows it caused, and an eviction is the one the table decided on
its own. It exists for caches whose entries own something outside the table: the
PostgreSQL driver's statement cache holds plans that still exist *on the
backend* until a `Close ('S')` goes out for them, so a cache that evicted
silently would leak one server-side prepared statement per eviction, per
connection, for the life of the process.

**`keep_deadline=True`** preserves the window a live key already has instead of
starting a fresh one. That is the rule a claim ledger needs — a holder that keeps
writing must not be able to hold its key forever — and it lives in the table so
that `wreath.store`'s two backends cannot drift apart on it.

## Where the budgets are

Every bounded table Wreath itself builds, and the knob that tunes it, is listed
in [what a worker holds in memory](memory-budgets.md) — kept true by a lint gate
rather than by anyone remembering to update it.

::: wreath.kv
