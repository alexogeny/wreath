# What the middleware stack actually costs

Status: **measurement, no code change.** 2026-08-06. Third pass of the method in
[`hmac-key-schedule.md`](hmac-key-schedule.md) and
[`repeated-work-audit.md`](repeated-work-audit.md).

This round found no fix worth landing. It found numbers, four of which change
where it is worth looking next, and it leaves behind a way to re-measure them
with one command.

## Measuring in instructions, not time

Every earlier round timed things with a clock. On this machine that was a fight:
the A/A spread — the same code measured against itself — was 9.3%, and the
frequency governor moves under any run long enough to be interesting.

`wreath-cpu-probe` counts **CPU instructions** instead. A phase costing 40,000
instructions costs 40,000 at 4.5 GHz and at 400 MHz; only the time to retire
them changes. Measured across four runs of the same arms:

| | run 1 | run 2 | run 3 | run 4 | spread |
| --- | --- | --- | --- | --- | --- |
| no middleware | 33,348 | 33,332 | 33,418 | 33,364 | **0.13%** |
| one empty hook | 40,013 | 40,154 | 39,873 | 39,989 | **0.35%** |
| seven empty hooks | 48,475 | 48,558 | 48,588 | 48,329 | **0.27%** |

Instruction counts hold to a third of a percent where wall time wandered by
nine. **Cycles and IPC in the same table are unusable** — the cycle count for
identical work ranged from 4,369 to 24,914 — because reading those needs the
clock pinned, which needs root (`wreath-bench-quiet --tier 1`). The tool says so
in its own output; this is that warning being right.

## What the numbers say

New arms in `benchmarks/bench_clock_scaling.py` install hooks that do *nothing*,
so the cost of having a tape separates from the cost of what the hooks do:

```
uv run wreath-cpu-probe --arm hooks-0 --arm hooks-1 --arm hooks-7 \
                        --arm hooks-7-route --arm middleware
```

| arm | instructions | over no middleware |
| --- | --- | --- |
| no middleware | 28,652 | — |
| 1 empty global hook | 35,683 | +7,031 |
| 7 empty global hooks | 44,270 | +15,618 |
| 7 empty **route** hooks | 49,123 | +20,471 |
| the 7 shipped middlewares | 130,870 | +102,218 |

### 1. The hooks' own work dominates, not the machinery

Seven real middlewares cost 102,218 instructions; seven that do nothing cost
15,618. So **85% of the middleware bill is the hooks doing their jobs** —
parsing cookies, checking tokens, formatting headers. That is worth knowing
before anyone rewrites the dispatcher: the machinery is 15% of the problem.

### 2. The first hook costs five times what the next one does

Going from no middleware to one empty hook costs 7,031 instructions. Going from
one to seven costs 8,587 more — about 1,400 each. Nearly half the tape's total
overhead is paid the moment the *first* global hook is installed.

That is not the hooks. Counted in Python frames, adding a global hook adds only
five: the dispatcher, the two hook halves, and two route-outcome accessors. The
instructions are inside `_handle_http`, the general dispatcher, which the
application moves onto the moment it has any global hook.

`_select_dispatcher`'s own docstring describes exactly this cost and why
`_handle_http_plain` exists to avoid it — for applications with *no* hooks. An
application with hooks pays it in full. There is a third dispatcher,
`_handle_http_compartment`, but it serves a narrower case: it needs
`_route_programs`, which is only built when some route *excludes* some global
middleware. The realistic sample app has seven middlewares, no exclusions, and
takes the general path.

**This is the largest single number in this document and I have not acted on
it.** Adding a fourth dispatcher is a design decision, and the code argues
against it in writing ("two implementations of dispatch is already the most this
is worth" — written before the compartment one made three). The measurement is
here so the decision can be made against a number instead of an impression.

### 3. Route middleware costs more than global middleware

Seven route-scoped hooks: +20,471. Seven global ones: +15,618. Route middleware
is **31% dearer per hook**, which is the opposite of what the documentation
implies — it describes route middleware as the narrower thing that "runs only
once a route has matched and its authorization has passed". Narrower in *when it
runs*, but not cheaper when it does.

Worth a line in the guide, so nobody reaches for route scope expecting it to be
the lighter option.

### 4. Touching `request.state` switches on bookkeeping

A hook that writes one attribute to `request.state` costs 4,060 instructions
more than one that does nothing. In frames that is seven, not two: three
`__setattr__` calls, a `get`, a `cast`, the property, and the `State`
constructor.

This is the documented design working as intended — a request whose hooks never
touch state skips the allocation entirely, and the first touch materialises it
and copies the routing outcome in. The number is the price of that trade, and it
is higher than "one attribute write" suggests. Most shipped middleware touches
state.

## Places checked and left alone

- **The complexity probes all pass.** Every scaling assumption in
  `docs/agents/complexity-baseline.json` holds — timers, routing, header
  parsing, response emission, chunked bodies, egress backpressure. Whatever is
  left to win is a constant factor, not an algorithm.
- **`request.state`** is close to its floor: 20-45 ns per operation against
  19-28 ns for the plainest equivalent.
- **The proxy-header and CORS hooks** are already careful, with their reasoning
  written down in place.

## Two small things found, and not taken

Both are real, both are tiny, and both are recorded so they are not re-found.

- **`typing.cast` costs 37 ns and does nothing.** It exists for the type checker
  and is a no-op at runtime, but it is still a Python call. Two run per request
  on the hot path (`request.cookies`, `request.client`, `request.scheme`,
  `app._handle_http`). Removing them means satisfying `ty` another way, which is
  more disruption than ~75 ns justifies today.
- **`request.header("origin")` re-encodes and re-lowercases the name on every
  call**, though every caller passes a module-level constant: 171 ns for a `str`
  name against 136 ns for pre-lowercased `bytes`. Two calls per request, so
  ~70 ns.

## The honest summary

Two rounds of this method found two real defects worth about a microsecond each.
This round found none, and the reason is informative: the remaining cost is not
repeated work or a badly ordered guard. It is a general dispatcher doing general
things, and hooks doing what they were asked to do.

The one lever left with real weight — specialising dispatch for the common
"has middleware, excludes nothing" shape, worth up to 7,031 instructions per
request — is a structural change with an argument already written against it.
That is a decision, not a defect, and it belongs to whoever owns the design.
