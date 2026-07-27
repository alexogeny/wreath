# Python/native boundary crossings in the request lifecycle

**Status:** analysis, 2026-07-16. No code changes proposed here have been made.

## The question

Neo's intended shape is that a request stays in C through ingress, routing,
authentication, and authorization, and enters Python when a route handler is
*activated*. This document reports where that holds and where it does not, and
which departures are reducible without giving up the ergonomics that make the
framework worth using.

## Method

`uv run neo-request-trace` counts every crossing for one request against a
realistic app (proxy headers, rate limiting, CORS, CSRF, security headers,
request IDs, timing, bearer authn, role + policy authz, a parametrized route
table, an ORM read). Counts are exact rather than sampled: `sys.setprofile`
reports `c_call` on entry to a C function and `call` on entry to a Python
frame. The baseline lives in `docs/agents/request-boundary-baseline.json`.

**This report counts crossings. It does not measure time.** Every performance
statement below is a hypothesis that needs a benchmark before it is a claim.

## Finding: the invariant does not hold today

| | Python frames | calls into C |
| --- | --- | --- |
| **Before handler activation** | **54** | **49** |
| Handler | 72 | 91 |
| Egress | 37 | 33 |

The same app with the middleware and auth removed enters Python **3** times
before activation. So the stack, not the framework skeleton, is what pulls the
request into Python early.

Python is entered *once*, very early, and never really leaves. The native
server parses the head in C and then calls
`Neo._neo_http` (`src/neo/_native/server_http1.c:1840`, via `spawn_app_task`)
— a Python coroutine — **before any middleware, routing, authentication, or
authorization has run**. Everything after that point is Python control flow
that dips into C for individual predicates and returns.

## Root cause: C owns leaves, Python owns sequencing

Every symbol `_coremodule.c` exports is a *leaf predicate*:
`find_header`, `origin_matches`, `host_allowed`, `build_capability_mask`,
`normalize_authorization_decision`, `format_server_timing`, `request_id_valid`,
`cache_control_flags`. Not one of them owns a *phase*.

Neo already has native implementations of nearly every ingress concern —
`authz.c`, `ratelimit.c`, `proxy.c`, `webpolicy.c`, `security.c`, `router.c`,
`dtbitset.c`. What is not native is the **sequencing**. That lives in
`app.py:_handle_http`, a ~200-line Python coroutine that decides what runs next.

Because the sequencer is Python, C can only ever be leaves, and each leaf costs
a round trip. This is why the boundary linter cannot see the problem: every
individual C function is fine. The cost is in who calls whom, across modules.

## Reducible crossings, ranked

### 1. The framework's own middleware are Python hooks (~32 of the 54 frames)

Seven middleware run before activation, each an `await before(request)` into
Python, and each Python frame doing a handful of C leaf calls. But **these are
Neo's own middleware, fully specified at startup** — not user code.
`compile_middleware` already fuses them into a linear tape (`middleware/base.py`)
because the pipeline is static and known. That is precisely the precondition for
emitting it as a native program instead of a Python one.

This is the single largest block of pre-activation Python, and none of it is
user code.

### 2. `request.scope` defeats `_RequestContext` (gratuitously, in one case)

`_RequestContext` (`server_request.c`) exists so the native path never builds an
ASGI scope dict: it holds `method`, `path`, `headers`, `client`, `scheme` as
ready-made Python objects. `Request.scope` lazily calls `context._asgi_scope()`,
which materializes a 13-key dict.

Seven middleware read `request.scope`, four of them before activation:

| Site | Reads | Fix |
| --- | --- | --- |
| `cors.py:75` | `scope["method"]` | `request.method` — **already exists; pure win** |
| `security.py:108`, `csrf.py:73,184` | `scope.get("scheme")` | needs `Request.scheme` |
| `proxy.py:64`, `ratelimit.py:197` | `scope.get("client")` | needs `Request.client` |

The CORS one is gratuitous: `request.method` is a C getter on the context, and
the line reads the dict purely out of habit. Installing CORS currently forces a
full scope-dict build on every request.

The others are not gratuitous, and the reason is worth stating: **ProxyHeaders
writes** `scope["client"]` and `scope["scheme"]` (`proxy.py:80,89`) so
downstream middleware observe the corrected values. The scope dict is currently
the only mutable shared surface, so the fix is not just "add getters" — the
context needs mutable `client`/`scheme` with `Request` accessors, and
ProxyHeaders needs to write through them. `_asgi_scope()` would then read
current values, and only when something genuinely needs the dict (a third-party
ASGI app). The fields are already stored in the context; they are simply
immutable and unexposed (`context_getset` omits `client` and `server`).

### 3. Header lookups rescan instead of indexing

Six `find_header` calls before activation, each preceded by a Python
`request.headers` property access. `Request.header()` scans on the first lookup
and only builds a map on the second (`request.py`), which is a good heuristic
for one reader and the wrong one for a stack where proxy, CSRF, request-ID, and
auth each look up different headers independently.

The parser already has every header in C
(`neo_http_parse_request_parts`, `http.c`) and eagerly materializes a Python
list of `(bytes, bytes)` tuples for all of them — including the ones nobody
reads. A native header index built once at parse time would make these lookups
zero-crossing, and would let the tuple list be materialized lazily, only for
ASGI compatibility.

### 4. Bearer authn crosses ~8 times to reach one line of user code

`BearerTokenBackend.authenticate` (`auth/backends.py`) finds the authorization
header, decodes it, splits the scheme, compares it to `Bearer`, and then calls
the user's `verify(token)`. Everything but the last step is framework code
running in Python. Native scheme extraction entering Python once, for the
verifier, would collapse this.

### 5. Routing is interleaved with authn, so it cannot stay native

`classify()` returns a ticket, Python authenticates, `_identity_mask()` builds a
capability mask, then `resolve(ticket, mask)` finishes the match. The trace
shows the phase oscillating `routing → auth → routing → auth`. The lazy ticket
is a *good* design and worth keeping. But it means routing cannot complete
without re-entering Python for authentication — so making authn native (4) is a
precondition for keeping routing native, not an independent win.

### 6. `Request` and `State` are built at ingress, before anything needs them

`Request(...)` plus `State()` are constructed as soon as any global hook exists,
and `request.state` is touched 7 times before activation (7 Python property
calls, 6 `__setattr__`). `state.route_outcome` is bookkeeping the framework
writes for its own hooks.

## What must stay in Python

This is the ergonomics boundary, and it should not move:

- **User middleware hooks.** A `before`/`after` hook is user Python by
  definition.
- **User auth verifiers and policy providers.** `verify(token)`,
  `authorizer.authorize(...)`.
- **Route handlers.** The point of the framework.
- **The ASGI scope dict**, for third-party ASGI servers and apps — but it is
  already lazy, and finding 2 is about keeping it that way.

Nothing in the ranked list above requires moving user code into C. That is the
whole point of the next section.

## Relationship to the single-pass pipeline plan

[`single-pass-request-pipeline.md`](single-pass-request-pipeline.md) is
complete, and it is why `_handle_http` looks the way it does: one
classification, opaque tickets, denials terminating early, no authentication on
public or missing routes. It made the Python sequencer **do less work**.

This document is about **who sequences at all**, which that plan deliberately
left alone — it preserved the Python dispatcher and optimized its ordering. The
two do not conflict, and the earlier work is a precondition for this one: a
pipeline that has already been reduced to a fixed, compiled sequence of stages
is exactly what can be emitted as a native program. The lazy ticket in
particular should survive any such change.

## Feasibility: is a native sequencer possible?

Two things decide this, and both were measured rather than assumed.

### Nothing before activation actually suspends

A C sequencer would need coroutine machinery only for steps that genuinely
yield to the loop. Disassembling every framework hook for suspension opcodes
(`SEND`, `YIELD_VALUE`, `GET_AWAITABLE`, `END_SEND`):

| step | suspends? |
| --- | --- |
| ProxyHeaders / RateLimit(local) / CORS / CSRF / SecurityHeaders / RequestID / ServerTiming — `before` **and** `after` | **no** — none |
| `BearerTokenBackend.authenticate` | yes (awaits the user's verifier) |
| `Neo._handle_http` / `_authorize_request` / `_run_stage` / `_finish_http` | yes |

**Every framework middleware hook is `async def` purely to satisfy the hook
contract. Not one of them ever yields.** The tape is synchronous work wearing a
coroutine costume, so a native driver would not need to be a coroutine to run
it.

The codebase already knows this. `RateLimitMiddleware.__init__` binds
`self.before` to `_before_local` or `_before_remote` at construction, commenting
that "a synchronous store also skips a coroutine on the hot path". That is this
idea, applied once, by hand.

### Almost none of the pre-activation Python is user code

Of the 54 pre-activation Python frames, **52 are framework code**. The two that
are not are the user's `verify(token)` and policy `authorize(...)` — irreducible,
because they are the user's.

### What it would be worth

`uv run neo-tape-decomp`, medians of 11 interleaved rounds x 4000 requests,
A/A noise floor 0.76us (2.1%), raw results in `benchmark-results-tape/`:

| | us/request |
| --- | --- |
| bare app (auth + routing + ORM handler, no middleware) | 35.90 |
| full realistic stack | 68.92 |
| **the tape** | **+33.02 (47.9% of the request)** |

Measured two independent ways — installing all seven at once, and adding them
one at a time — which agree within noise (+33.02 vs +34.24).

Two cautions on that number:

* It is an **in-process measurement of the ASGI callable**, not a server
  benchmark. It isolates the framework layer, which is the layer in question,
  but a real request also pays socket, parse, and emit costs, so 47.9% is not
  47.9% of a wire request.
* It is a **ceiling, not a saving**. A native driver does not make `urandom`,
  HMAC, or base64 cheaper. Notably, though, CSRF's own crypto costs ~2.5us
  against its ~15.6us arm, so the tape is dominated by dispatch, not by work.
  The sample also mints a CSRF token on every request because it sends no
  cookie; a cookie-bearing client would be cheaper.

Separately, `sum(alone) = +60.55us` exceeds the whole tape's +33.02us, because
each arm re-pays a fixed **~4.6us** for having any global hook at all: the eager
`Request`+`State` construction, `route_outcome` bookkeeping, and the after-hook
unwind that `_handle_http` performs the moment `_global_hooks` is non-empty.
That is finding 6, priced.

### Verdict on feasibility

Feasible, and the practical portion is most of it. The tape needs no coroutine
support in C; the only pre-activation suspension is authentication, and only
because the user's verifier is awaited — `BearerTokenBackend` already detects a
synchronous verifier (`_verifier_is_async`), so that case can complete in C too.

**But feasible is not the same as worth it. See the next section, which is the
one that matters.**

## What a crossing is actually worth

Counting crossings only guides work if a crossing has a price. It does, and it
is small but real — which has consequences in both directions.

`cors.py` was the cheapest fix on the list: it hand-rolled a Python genexpr over
the response headers where `webpolicy.find_response_header` — the C helper its
sibling middleware already use — does the same scan, and it read
`request.scope["method"]` where `request.method` exists. Applying both removed
**11 Python frames** per request (`neo-request-trace` totals 163 → 152).

A/B of the two implementations, same process, interleaved, 11 rounds x 4000
requests, could **not resolve it**: −0.19us against a 0.78us noise floor.

That is a fact about the instrument, not about the fix. Dividing an unresolvable
delta by 11 to get a per-frame cost — an earlier draft of this document did
exactly that, and concluded 18ns — is not a measurement. The question is
answerable directly: add frames in bulk, where the signal dwarfs the floor, and
take the slope (`uv run neo-tape-decomp --calibrate`).

| extra Python frames | us/request |
| --- | --- |
| 0 | 65.98 |
| 200 | 72.85 |
| 400 | 99.44 |
| 800 | 128.92 |

**Slope: ~70–85 ns per Python frame**, from two independent calibrations, with
the largest arm running +63us — far above any noise floor. So:

* The CORS fix is worth **~0.9us**. It lands almost exactly *on* a single A/B's
  noise floor, which is precisely why one A/B cannot see it. It is still real.
* **Roughly two such fixes become measurable; ten are worth ~8us.** Small
  frame-removing changes accumulate, and dismissing each one as "below noise"
  would be a methodological error — the floor bounds what a single comparison
  resolves, not what the change does.
* All **52 pre-activation framework frames are worth ~3.7–4.3us**, about 6% of
  this request.

### Consequences

**For incremental fixes:** they are worth doing, and the right instrument is the
frame count, not a stopwatch. `neo-request-trace` counts exactly and
deterministically, so it resolves a single fix that timing cannot; the
calibration above converts its counts to microseconds. Track frames per change,
re-measure time once several have landed.

**For the structural change:** a native ingress sequencer removes those 52
frames and is therefore worth **~4us of the 33us tape**, not 33us. That is a
real number and a poor return for a C reimplementation of the dispatcher, with
the risks listed below. The remaining ~29us is not dispatch at all.

### So where does the other ~29us go?

Into the hooks' own work, not into dispatch. Marginal cost per hook, from
`neo-tape-decomp --mode alone` with the ~4.6us fixed toll subtracted:

| hook | marginal |
| --- | --- |
| CSRF | ~11.0us |
| CORS | ~5.6us |
| RequestID | ~3.4us |
| ProxyHeaders | ~2.9us |
| ServerTiming | ~2.8us |
| SecurityHeaders | ~2.0us |
| RateLimit (memory store) | ~0.6us |

CSRF is not paying for cryptography (its urandom + HMAC + base64 measure ~2.5us)
and it is not an artifact of the sample sending no cookie: an arm that *does*
send a valid cookie, taking the validate path instead of the mint path, costs
the same within noise (+15.01us vs +13.84us, floor 2.86us).

### Ablation: the machinery costs as much as the work

`cProfile` attributed CSRF's cost to its token glue. That was wrong, and
expensively so: the profiler adds ~1-2us *per call*, and `_new_token -> _sign ->
2x _b64encode` is five calls, so most of the "glue cost" was the profiler
measuring itself. **Do not size a hook with cProfile.** Ablate instead --
remove one piece at a time and time the whole app with no profiler attached:

| arm (CSRF only, over a bare app at 35.93us) | vs bare |
| --- | --- |
| full CSRF hook | +12.52us |
| ... with mint/validate removed | +7.83us |
| ... with `after()` removed | +8.67us |
| ... with `before()` removed | +8.87us |
| **inert hooks (`before`/`after` return immediately)** | **+4.59us** |

A/A floor 1.61us, so every row resolves.

**An inert global middleware -- two hooks that do nothing at all -- costs
4.59us**, roughly a third of CSRF's total. That independently reproduces the
~4.6us fixed toll derived from `sum(alone)` vs the full tape, by a completely
different method. Minting and validating the token is only 4.69us, of which
~2.5us is the irreducible urandom+HMAC+base64.

So the largest single item in the tape is **not any hook. It is the cost of
having hooks**: the eager `Request`+`State` construction and `route_outcome`
write that `_handle_http` performs as soon as `_global_hooks` is non-empty, the
enumerate/reversed dispatch loops, `_coerce_response` per after-hook, and two
coroutines created and immediately unwound per middleware. Recall from the
feasibility section that *none of those coroutines ever suspends*.

### It is a fixed toll, and synchronous hooks are not the fix

The obvious reading of that table is "the coroutines". It is wrong, and the
measurements say so before any code was written:

**A non-suspending `await` costs 37.9ns more than a guarded synchronous call**
(75.3ns vs 37.4ns, A/A floor 0.4ns). Across all 14 hook calls a request makes
(7 middleware x before+after) that is **0.53us** — worth having, but not the
4.59us, and not worth widening the public middleware contract on its own.

Inert middleware, added one at a time (A/A floor 0.26us):

| arm | vs no middleware |
| --- | --- |
| 1 inert (before **and** after) | +3.37us |
| 1 inert, **before only** | +3.31us |
| 1 inert, **after only** | +3.39us |
| each additional inert middleware | +0.80us |

A middleware with only a `before` hook costs the same as one with both. **The
price is not per hook. It is a fixed toll, paid once, the moment
`_global_hooks` stops being empty** — and `neo-tape-decomp`'s `sum(alone)` vs
full-tape estimate independently put it at ~4.6us.

### The toll's source is not yet isolated

Honest status. Timing the operations the toll performs, standalone:

| operation | cost |
| --- | --- |
| `Request(...)` | 288ns |
| `+ request.state.route_outcome = "ingress"` | 666ns |
| `+ the `get`/set at route time` | 850ns |
| `reversed(hooks[:active_global])` unwind loop | 150ns |
| **state bookkeeping + unwind, together** | **~0.71us** |

That accounts for 0.71us of 3.37us. **2.66us is unexplained**, and three
hypotheses have already failed:

* *cProfile's attribution* — invalid. It adds ~1-2us per call and was measuring
  itself.
* *Coroutine creation* — measured at 0.08us for the two hooks.
* *GC pressure from the extra `State`+dict* — the control is invalid: disabling
  the collector makes the toll **larger** (+6.77us vs +3.41us), because
  uncollected cycles grow the heap and cost more in locality than the collector
  does in cycles.

So: the toll is real, reproducible, and the single largest item in the tape, but
nobody should write code against it until its source is found. The next step is
isolation — bisect `_handle_http`'s `if global_hooks:` path directly — not
another guess.

That reframes the native-op idea in finding 1, and improves it. Native ops are
worth building not to skip crossings (~4us) but because the hooks' *work* —
header scans, cookie parsing, string assembly — is what C makes faster, and that
is where the other ~29us lives. Aim it with `neo-tape-decomp`, hook by hook,
starting with CSRF.

The two efforts are complementary and independently justified: frame removal is
cheap, cumulative, and tracked by `neo-request-trace`; native ops are the larger
win and tracked by `neo-tape-decomp`. Neither requires moving the *sequencer* to
C, which is the one thing the numbers do not support.

## The structural change

Don't move user code to C. **Invert who sequences.**

Today: Python sequences, and calls C leaves.
Proposed: C sequences a compiled pipeline, and calls Python only for
user-supplied steps.

Neo already compiles routes (`_compile_routes`) and fuses middleware
(`compile_middleware`) at startup. The pipeline is static by the time a request
arrives. So it can be emitted as a native ingress program: an array of ops
(proxy, ratelimit, cors, csrf, security-headers, request-id, timing, classify,
authn, resolve, authz), each either a native op or a `PY_CALL` escape for a
user-supplied hook. `_handle_http`'s branching becomes a small C interpreter
over that array.

For an app whose middleware are all framework-provided, Python would then be
entered on the **first `PY_CALL`, which is the handler** — the stated invariant,
delivered without taking anything away from the user. An app with user hooks
pays one crossing per hook, which is the irreducible cost of the hook existing.

Crucially, this keeps the sequencer **in the framework, not the server**. A
native ingress program driven from `spawn_app_task` would be faster still, but
it would strand every app running on Uvicorn and force a second sequencer.
`_handle_http` calling one C entry point keeps a single implementation, works on
any ASGI server, and preserves the `_pure` fallback the optional extension
already requires.

This is a large change and is not proposed as a single step. Findings 2 and 3
are independently worthwhile, much cheaper, and do not depend on it.

### Risks worth naming

* **Two sequencers, forever.** `_core` is optional, so the Python path stays and
  needs parity tests. That is the established pattern here (`_pure` vs
  `_native`), not a new burden, but it is real.
* **Exception semantics are where the bugs will be.** `_handle_http` coerces
  errors to responses at every stage, and `_finish_http` must unwind exactly the
  after-hooks that ran. Getting that wrong in C is silent.
* **Debuggability regresses.** A C sequencer erases the Python frames that
  tracebacks and profilers rely on — including `neo-request-trace` itself, which
  would go blind on the native path and need the pure path to keep tracing.

## What has been done

Measured by `neo-request-trace`, which resolves a single change that timing
cannot. Pre-activation went **54 Python frames / 49 C calls → 50 / 37**.

1. **`cors.py` uses `webpolicy.find_response_header` and `request.method`**
   (−11 Python frames, +1 C call). CORS was the only middleware hand-rolling a
   header scan its siblings already got from C. Also a correctness fix: the
   match is now case-insensitive, so a handler's own
   `Access-Control-Allow-Origin` is honored rather than duplicated.
2. **CSRF token minting and validation moved into `security.c`** (−3 Python
   frames, −11 C calls), with pure twins in `_pure/security.py` and 43 parity
   tests. The HMAC still comes from `hmac.digest`; only the glue is native.
   Two bugs the parity tests caught, both invisible to the timing A/B:
   * A malformed `PyArg_ParseTuple` format string (`"S Ls#"`).
   * `int()` is arbitrary precision and `strtoll` is not, so a token claiming a
     26-digit issue time was rejected by both twins but with different `issued`
     values. The pure twin now rejects anything outside int64 explicitly.
   **Its timing win did not resolve** (−0.04us against a 2.98us floor). It was
   kept for the crossing reduction, not for a measured speedup, and the ~8us
   this was predicted to save was an artifact of reading cProfile.
3. **`ProxyHeadersMiddleware` shares one header index** instead of running three
   scans, and `Request._set_header` now *updates* the index rather than dropping
   it — otherwise ProxyHeaders' own write forced every later consumer to rebuild
   it. (−2 Python frames, −3 C calls; timing unresolved in both directions,
   at 8 and at 20 headers.)

## Outside the tape: the ORM is bigger than all of it

The tape is ~33us of a ~69us request. Decomposing the other half
(`uv run neo-decomp --suite request`, A/A floor 0.18us, no global middleware in
any arm):

| stage | cumulative | step |
| --- | --- | --- |
| route only (no auth, no ORM) | 2.98us | — |
| + auth (roles) | 7.89us | **+4.91us** |
| + policy check | 8.78us | +0.89us |
| + **one ORM read** | 35.21us | **+26.43us** |

**The framework core is not the problem: a route with no auth and no ORM costs
2.98us.** One ORM read costs ~26us — against a *scripted in-memory database*, so
that is pure CPU with no I/O, and it is comparable to the entire middleware tape.

Inside one read (`--suite orm`, timed outside the pipeline):

| stage | cost |
| --- | --- |
| `Session()` + `close()` | 0.61us |
| build `Select` + `.where()` | 2.11us |
| `shape_of()` — derive the plan cache key | 1.67us |
| `compile_select` (prebuilt query) | 4.17us |
| build + `compile_select` | 6.91us |
| **full `fetch_one`** | **14.32us** |

**Building the query and finding its compiled plan is 44% of a read.** Note what
this is *not*: `compile_select` already consults `registry.cached_plan`, so the
SQL is not rebuilt. The cost is deriving the key that finds it — `shape_of`
walks the projection, predicates, orderings, and loads and assembles bytes,
per request, because the idiomatic
`User.select().where(User.id == request_value)` rebuilds the query object per
request.

That points at a design question rather than a micro-fix: a prepared-query API
that binds values to an already-shaped query would skip both the rebuild and the
re-derivation. It is the largest single item measured anywhere in this document
and has not been attempted.

## Suggested order from here

1. **The ORM read: ~26us, the biggest item measured anywhere here.** Query build
   (2.11us) plus cache-key derivation (`shape_of`, 1.67us within
   `compile_select`'s 4.17us) is 44% of a read, repeated per request for a query
   whose shape never changes. Wants a prepared-query design, not a micro-fix.
2. **Isolate the fixed hook toll.** 3.37us, the largest item *in the tape*,
   source unknown, 0.71us accounted for. Bisect the `if global_hooks:` path in
   `_handle_http` rather than theorize; three theories have already failed.
3. **Mutable `client`/`scheme` on the context, `Request` accessors, ProxyHeaders
   writes through them.** Removes the scope-dict build from the native path.
   **Unmeasured**: every number here is the in-process dict-scope path, and this
   only pays on the native server, which needs a server benchmark to size.
4. **Native ops for the remaining hook work**, aimed by `neo-tape-decomp`.
5. **Synchronous hooks: 0.53us**, and it widens the public middleware contract.
   Worth folding into (1) if the toll turns out to live nearby; not worth doing
   on its own.
6. **Not the native ingress sequencer**, on current evidence: ~4us for a C
   reimplementation of the dispatcher.

## Method, since three predictions failed here

Every hypothesis in this document that was not measured turned out wrong, and
each would have been built if it had not been checked first:

* cProfile said CSRF's cost was token glue. Moving the glue to C changed
  nothing; the profiler was timing its own per-call overhead.
* "The tape is 47.9%, so a native sequencer recovers 47.9%." It recovers ~4us.
* "An inert hook costs 4.59us, so it is the coroutines." It is 0.08us of it.

**Measure the thing before building the fix for it**, and prefer ablation
(remove a piece, time the whole app) over a profiler on a path this small.

Track each change with `neo-request-trace` (exact, deterministic, resolves a
single fix); re-measure time with `neo-tape-decomp` once several have landed,
because individually they sit at the noise floor. Re-record the baseline with
`--update-baseline` and say why in the commit.
