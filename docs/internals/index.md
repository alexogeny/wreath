---
description: The data structures behind Wreath's hot path, and how they differ from the conventional answer.
---

```hero
eyebrow: The request path
title: Most of a request never reaches Python.
lede: Ingress, middleware, routing, and authorization are native code. Python is entered once — when your handler is. This page walks that path and shows the three structures that make it short.
action: See the benchmarks -> ../perf/index.md
action: How we know -> #how-we-know
```

A framework is mostly bookkeeping: find the handler, check the caller, move the
bytes. None of that is your code, and all of it is on the path between a packet
arriving and your function running. Wreath's shape is that the bookkeeping stays
in C and Python is entered when a route is *activated* — not before.

That is a claim with a number attached. `wreath-request-trace` counts every
Python frame and every C call in one request against a realistic application,
and the result is checked into the repository as a baseline that CI refuses to
let drift. On that app, everything before the handler costs **one** Python
frame.

```figure
name: request-boundary
title: One request, two shapes
note: Both dots travel at the same speed; the difference is distance. Wreath's figure is drawn from docs/agents/request-boundary-baseline.json — 1 Python frame before the handler is activated. The upper track is illustrative: it shows the shape of a stack whose middleware, routing, and authorization are Python, not a measurement of any particular one.
```

The rest of this page is the machinery that keeps the lower track low, in the
order a request meets it.

## Timers: two per request, almost always cancelled

Every connection carries deadlines — a keep-alive and a request timeout — so a
server at load is creating and destroying two timers per request. Almost none of
them ever fire. They are cancelled, because the response went out first.

That workload is the whole design constraint, and it is not the one a general
timer queue is tuned for. asyncio keeps timers in a binary heap: inserting is
`O(log n)`, cancelling is `O(log n)`, and because cancelled entries are left in
place as tombstones, the loop periodically pays a compaction pass to sweep them
out. You are asking a structure optimised for *finding the earliest deadline* to
spend most of its time on deadlines that never mattered.

```figure
name: timing-wheel
title: Cancelling a timer
note: Both sides cancel the same timer, and the squares count what it costs. The heap walks one level per square and pays a compaction pass afterwards; the wheel unlinks the node from its bucket and closes the link behind it. The hand is the wheel's cursor, one slot per tick.
```

A hashed timing wheel inverts the trade. Slots are buckets of a fixed
resolution; a timer due in `d` ticks lands in bucket `(cursor + d) % slots` and
carries `rounds = d / slots`, decremented each time the cursor sweeps past.
Insert and cancel are pointer splices on an intrusive doubly linked list —
`O(1)`, no reallocation, no heapify, no compaction. Finding the next deadline,
the one thing a heap is actually good at, comes back from a segment tree over
the slot minima, and the metal loop blocks straight off it with
`io_uring_enter(EXT_ARG)`.

The full account — including the part where the first version was slower — is in
[The timer that wouldn't settle](../explorations/the-timer-that-wouldnt-settle.md).

## Routing: a parameter that doesn't multiply

A route table has to answer "which handler, and what were the path parameters"
for every request. The conventional answer is a tree: split the path into
segments and walk down, one node per segment.

Trees are excellent at literals and awkward about parameters. A route like
`/orders/{id}/items` matches *whatever* is in the second position, so a
decision tree has to make that route reachable from every literal branch at that
depth — and again from every branch below it. Add parameters and the compiled
form grows faster than the number of routes you wrote.

```figure
name: route-bitset
title: Matching GET /orders/42/items
note: Six routes in one (method, segment-count) group. Read a row to see when a route stopped matching, or a column to see how many were still in the running. The tree needs a copy of each parameter route under every literal branch; the bitset needs one bit.
```

Wreath's bitset table gives every route in a `(method, segment-count)` group a
single bit, ordered by specificity and then registration so bit order *is*
priority order. Matching intersects one mask per segment position:

```python title="the whole matching loop"
for position in group.order:
    survivors &= group.literal[position].get(segment, 0) | group.param[position]
    if survivors == 0 or survivors & (survivors - 1) == 0:
        break                       # none, or one: nothing further can narrow it
```

A parameter contributes its bit at *every* position, so it costs one bit rather
than a copy per branch, and the compiled table stays linear in the route count.
Positions are tested strongest-discriminator-first and the walk stops as soon as
one route can still match. Authorization is then one more AND against the
caller's capability mask, on a ticket the walk already produced — so a protected
route is never walked twice, and a public one never authenticates at all.

What that buys, measured on a 512-route table with half its segments
parameterised: resident size drops from about 20 MB to about 264 KB — **76×** —
at 14% to 37% fewer instructions per match. This is now the application router;
`routing="policy"` names it directly. It is the sole compiled table; the former
experimental bitset, decision-tree, and trie names and implementations were
removed so configuration cannot select a second request path.

## How we know

None of the above is a reason to believe a number. These are:

- **`uv run wreath-request-trace`** counts Python frames and C calls for one
  request through a realistic app, attributed by phase, and `--check` fails when
  a scenario grows. The baseline it compares against is
  `docs/agents/request-boundary-baseline.json`, and growth has to be justified in
  the commit that causes it.
- **[The performance page](../perf/index.md)** carries the throughput and latency
  numbers, each a median of three passes, with the methodology and the caveats
  up front. A single run is never allowed to support a claim here — that rule
  exists because a single run once did, and the optimization it justified
  evaporated on a second look.
- **`uv run wreath-decomp`** prices the stages against a measured A/A noise floor
  and refuses to attribute any difference that does not clear it. "Below noise"
  is reported as unresolved, not as zero.

If you want the shape of the whole server rather than these three pieces of it,
the [native server guide](../guides/server.md) is the map.
