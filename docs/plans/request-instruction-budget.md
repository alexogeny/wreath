# Where a request's instructions actually go

Status: **historical measurement, superseded.** 2026-08-06. Fourth pass of the method in
[`hmac-key-schedule.md`](hmac-key-schedule.md),
[`repeated-work-audit.md`](repeated-work-audit.md) and
[`middleware-tape-cost.md`](middleware-tape-cost.md).

The map below records the pre-policy architecture. As of 2026-08-08, the native
server stores validated request headers as one raw block plus spans and
materializes Python pairs only when observed, while first-class HTTP policy has
replaced the shipped middleware tape. Do not use the header or middleware rows
below as current budgets. The current A/A and cumulative policy map is captured
in
[`native_overhead_hunt_2026-08-08.json`](../../benchmarks/results/native_overhead_hunt_2026-08-08.json).

The first three passes each went at one subsystem. This one steps back and maps
the whole request, so the next piece of work can be chosen by size rather than
by whatever was last looked at.

Everything below is **CPU instructions**, not time. On this machine instruction
counts reproduce to about a third of a percent while wall time wanders by nine;
cycles and IPC in the same table are unusable without pinning the clock, which
needs root. Reproduce any row with:

    uv run wreath-cpu-probe --arm <name>

## The map

```
raw-asgi (a bare ASGI app, no framework)    20,075
static   (Wreath, fixed route, prebuilt Response)  28,704   +8,629
```

Wreath's floor is **8,629 instructions** over a bare ASGI callable. Everything
else is a feature, and each costs roughly this much on top:

| feature | instructions added | per what |
| --- | --- | --- |
| **8 request headers** | **+8,898** | ~1,110 each |
| the seven shipped middlewares | +101,066 | see below |
| response validation + JSON | +14,696 | per response |
| binding one typed parameter | +7,982 | per handler signature |
| 8 response headers | +3,409 | ~426 each |
| returning a dict instead of a Response | +710 | per response |

## The largest single finding: headers cost objects, not bytes

Request headers are the most expensive per-unit item in the framework, and the
cost is nearly all *object construction*, not parsing:

| arm | instructions | per header |
| --- | --- | --- |
| 1 header (baseline) | 27,479 | — |
| 8 headers, **1-byte** values | 34,537 | **882** |
| 8 headers, 24-byte values | 36,392 | 1,114 |
| 8 headers, 200-byte values | 37,082 | 1,200 |

A header costs about **880 instructions before its value contributes a single
byte**. Growing the value from 1 byte to 200 adds only ~320 more — about 1.6
instructions per byte. The parser's scanning and copying are cheap; what is
expensive is that every header becomes a `bytes` name, a `bytes` value, a
two-tuple and a list slot.

**A real request carries eight to fifteen headers and a handler reads one or
two.** At ~880 instructions each that is 7,000-13,000 instructions per request
spent building Python objects nobody looks at — comparable to Wreath's entire
8,629-instruction floor, and larger than the whole middleware tape's machinery.

This is not a new idea in the codebase. `http.c` says so directly, above the
function that builds the list:

> Header construction is a replaceable parser sink. Keeping allocation outside
> the request-line/state machine lets the server substitute **a lazy raw-header
> sink** without duplicating syntax validation.

The seam exists; nothing has been put through it. The measurement above is what
that would be worth.

The hard part is not the list — it is that the server itself reads several
headers while parsing (`content-length`, `transfer-encoding`, `host`,
`connection`, `upgrade`) and the smuggling defences depend on that happening
eagerly. So a lazy sink means: validate and scan every header as now, allocate
nothing, answer the framing questions straight from the buffer, and materialise
Python objects only when the application asks for them. That is a real design
change touching security-critical code, which is why this is a measurement and
not a patch.

Two smaller things were considered and sized while in there, both rejected as
not worth their disruption:

- `header_name_object` linear-scans a 16-name table for every header. It looks
  wasteful, but the interned-versus-novel arms differ by only ~134 instructions
  per header, and that number includes the allocation a novel name needs. The
  scan itself is not the cost.
- The header list is `PyList_New(0)` and appended to. Presizing would remove a
  handful of reallocations, worth perhaps 50-100 instructions for a whole
  request.

## The middleware stack

Covered in [`middleware-tape-cost.md`](middleware-tape-cost.md); repeated here
so the map is complete.

| arm | instructions | over none |
| --- | --- | --- |
| no middleware | 28,851 | — |
| 1 empty global hook | 35,703 | +6,852 |
| 7 empty global hooks | 44,206 | +15,355 |
| 7 empty **route** hooks | 49,232 | +20,381 |
| the 7 shipped middlewares | 129,770 | +100,919 |

**85% of the middleware bill is the hooks doing their jobs**, not the machinery.
Of the 15% that is machinery, nearly half is paid by the *first* hook, because
installing one moves the application off `_handle_http_plain` onto the general
dispatcher. Route-scoped middleware costs ~31% more per hook than global, which
is the opposite of what the guide implies.

## Binding and response handling

Binding one `int` path parameter costs **+7,982 instructions**, but only four
Python frames — so it is the binder machinery, not the conversion. Two pieces of
that are avoidable and were sized:

- `_convert_scalar` dispatches on the annotation at *request* time, through a
  chain of identity comparisons, though the annotation is fixed when the route
  compiles. Resolving the converter once would take a conversion from 112 ns to
  65 ns.
- The `("path", alias)` location tuple is allocated per request per parameter and
  is only ever read when a conversion *fails*.

Together those are worth roughly 50 ns per parameter — real, safe, and an order
of magnitude smaller than the two defects already fixed. Recorded rather than
taken, so the next person can decide with the number in hand.

Response validation and JSON is +14,696, already down from +22,580 by the
serialisation fix in [`repeated-work-audit.md`](repeated-work-audit.md).

## What this map says to do next

Ranked by size, with what each would take:

1. **Lazy request headers** — 7,000-13,000 instructions per realistic request.
   A design change through a seam the code already anticipates, touching
   request-smuggling defences. The biggest thing on the board by a wide margin.
2. **A dispatcher for "has middleware, excludes nothing"** — up to ~6,900 per
   request for any application with global middleware. A design decision with an
   argument already written against it in `_select_dispatcher`.
3. **Compiled scalar converters** — ~50 ns per bound parameter. Small, safe,
   uncontroversial.

The first two are both structural and both belong to whoever owns the design.
What the last four passes have shown is that the easy defects — work repeated
that only ever produced one answer — are now largely gone from the hot path;
what remains is the cost of the framework being general.
