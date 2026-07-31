# 0026 — The global middleware tape runs in C on the metal path

**Status:** planned, not implemented, and **not ready to start** — see "What
this is not". Metal tier only.
**Re-measured 2026-07-31, and the first version of this page was wrong.** Its
target survives — the tape is expensive and worth moving — but every causal
claim it made about *why* was an artifact of the instrument. The original
numbers are kept below, struck through, because the way they misled is the
useful part.

## The instrument was broken first

Nothing below can be read without this. `_devtools/measure.py` ran its arms
round-robin in a **fixed order every round**. Round-robin removes drift
*between* rounds; it does not remove drift *within* one, it converts it into a
per-arm constant. On a powersave governor the CPU ramps across a round, so arm 0
was measured cold and arm 7 hot on every single round, and averaging rounds
preserved the bias exactly rather than cancelling it.

On an 8-arm round that bias was **16% of the baseline** — larger than almost
everything these tools exist to measure, and stable enough across rounds to look
like a result. It ranked arms in an order that was purely their position:
middleware *reduced* request time, monotonically, in the order they were listed.

Alternating the direction of each round fixes it, and the A/A noise floor moved
from **5.61 µs to 0.18 µs — 31×**. Every number on this page has been re-taken
since. Two further defects fell out of the same investigation:

- **Cross-run comparison.** An identical no-op application measured 61.67 µs in
  a cold first block and 29.71 µs once the machine had ramped. Comparing arms
  timed in separate blocks inflated one CSRF figure by 2.4×.
- **The fixture is not the realistic case it claims to be.**
  `sample_app.py`'s header set carries no `Sec-Fetch-Site`, so CSRF takes its
  token-minting fallback on every request instead of the Fetch Metadata fast
  path every browser has used since 2023. That alone overstates CSRF by ~3.5×.

## The measurement

With the instrument fixed, and headers a modern browser would actually send, the
whole global stack costs **+16.5 µs on a ~30 µs request — 35%.** Under the
fixture's own headers it is ~24 µs, and the entire difference is that one
missing CSRF header.

Per middleware, measured in situ against an all-no-op tape of the same length,
so the fixed cost of having a tape and the cost of its length both cancel:

| middleware | run A | run B | `wreath-tape-decomp` |
| --- | --- | --- | --- |
| cors | 4.65 µs | 4.09 µs | 4.63 µs |
| timing | 4.54 µs | 4.28 µs | 3.92 µs |
| csrf | 4.39 µs | 4.12 µs | 3.74 µs |
| proxy | 4.19 µs | 5.43 µs | 3.14 µs |
| requestid | 3.03 µs | 2.40 µs | 2.05 µs |
| ratelimit | 1.40 µs | 1.04 µs | 1.74 µs |
| security | 1.80 µs | 0.72 µs | −0.24 µs (below floor) |
| **whole tape** | **16.62 µs** | **17.13 µs** | **16.51 µs** |

Two independent probes and the shipped tool agree on the total to within 0.6 µs.
Individual bodies wobble ±1 µs run to run, so they are a band, not a point —
quote the ordering, not the digits. The bodies sum to ~19–24 µs but the whole
tape is ~17 µs: it is **sub-additive by 5–7 µs**, because the first body to
touch the header index pays for it and the rest ride along.

### What the original page got wrong

~~"Roughly 14 µs is in neither the mechanism nor any body — it is the
per-request object graph that having a Python tape forces into existence."~~
**There is no such residual.** Measured directly:

| claim | original | measured |
| --- | --- | --- |
| having *any* global hook at all | 1.11 µs | 1.56 µs — and the tool's own estimate is 0.41 µs |
| each extra no-op `before`+`after` pair | 0.24 µs | free; six more pairs measured −0.49 µs total |
| `SecurityHeadersMiddleware` installed alone | +6.54 µs | −0.24 µs, below the floor |
| materializing the header index | (the 14 µs) | +0.75 µs |
| touching `request.state` | (the 14 µs) | +0.15 µs |

`SecurityHeadersMiddleware` was offered as the proof of the object-graph
hypothesis: 0.45 µs of work, +6.54 µs installed. It is the cheapest middleware
in the stack and its true marginal cost is *at* the noise floor. The 6.54 µs was
its position in the round.

~~"The tape and the dispatcher compose; that composition is most of the prize,
not the opcode bodies."~~ **Backwards.** The composition — everything an
application stops paying when `_has_global_http_hooks` goes false — is worth
**~1.5 µs**. The opcode bodies are worth **~17 µs**. It is entirely a bodies
story, which is a better position for this ADR than the one it argued for: the
prize does not depend on a second optimization also landing.

~~"CSRF is half the body cost."~~ Under realistic headers it is 3.7–4.4 µs, one
of *five* middlewares in a 3–4.7 µs band. There is no dominant item.

## The decision

Compile the configured global middleware stack into a **descriptor tape executed
in C**, on the metal path only, against the `_RequestContext` that already
exists there.

The policy every one of these middlewares applies is fixed when the application
boots: a CSP string, an allow-list of origins, a trusted-network set, a bucket
rate. What is paid per request is not deciding that policy — it is executing a
fixed program through a Python object graph rebuilt for every request. That is
the same defect as
[the response validator](../internals/compile-time.md), one layer out: work
settled at boot, re-interpreted per request.

Wreath already owns a native primitive for every hard part — `TokenBucket`,
`TrustedNetworks`, `csrf_sign`, `csrf_validate`, `csrf_new_token`,
`host_allowed`, `format_server_timing`, `request_id_valid`, `random_hex`. What
is Python is the glue between them and the per-request objects that glue needs.

### Why metal only

The metal tier is where a request already exists as a C object for its whole
life. On `native` (C extensions on stock asyncio) and on `pure`, the tape would
have to materialize the very objects it exists to avoid, and would be slower
than the Python it replaced. This is an experimental tier's change and it stays
there; `native` and `pure` keep the Python tape unchanged and remain the
reference for what the tape must produce.

### The dispatcher composes, and it is a rounding error

`_select_dispatch` (see `app.py`) picks `_handle_http_plain` only when
`_has_global_http_hooks` is false, so an application whose entire stack compiles
to a native tape becomes eligible for the specialized dispatcher too. That is
still true and still worth taking. It is worth **~1.5 µs of the ~17 µs**, not
most of it. Do not let it justify anything on its own.

## What this is not — and one rejection that does not stand

- **Deferring observing middleware past the response** ("reflex"). Re-measured
  at −1.72 µs, up from −0.82 µs but still small, and still for the reason
  originally given: the deferrable middleware are not the expensive ones.
  Rejection stands.

- **Per-route compartments** — "run only the middleware a route needs". **This
  rejection is withdrawn.** It was refused on the grounds that its −26.5 µs
  ceiling was "false, because skipping a hook saves 0.24 µs". That 0.24 µs was
  the cost of skipping a **no-op** hook — the dispatch and nothing else. It is
  the wrong quantity: skipping a *real* middleware saves its body, which the
  corrected table puts at 1–4.7 µs each.

  Re-measured with the instrument fixed, running 2 of 7 middlewares instead of
  7 is **−27.58 µs against a 34.88 µs request**, comfortably resolved. The
  ceiling was real all along.

  The `applies()` predicate implemented for `CORSMiddleware` measured −0.05 µs
  and was reverted. That number was taken with the broken harness and should not
  be trusted either way; CORS's body is 4.1–4.7 µs, so a gate that genuinely
  skips it cannot save 0.05 µs. Whether the predicate worked is now an open
  question, not a settled one.

  **Compartments were then built and measured, and they work.** A global
  middleware may expose `applies_to(method, path)`, consulted once per route at
  compile time; declining routes dispatch through a program compiled without it,
  via `_handle_http_compartment`. Measured by
  `uv run python benchmarks/bench_compartments.py`, over two runs:

  | | run A | run B |
  | --- | --- | --- |
  | ceiling available | 18.85 µs | 18.88 µs |
  | captured by the mechanism | 18.45 µs (98%) | 18.49 µs (98%) |
  | mechanism overhead | 0.40 µs | 0.39 µs |

  The benchmark asserts every arm answers 200, and that the compartment arm's
  status, headers and body match the truncated stack's, before and after timing.
  Both checks are load-bearing: an earlier run of this benchmark compared the
  two arms only against *each other* and reported 27.12 µs captured of a 27.39 µs
  ceiling. Its baseline was answering 500 on every request — the handler
  returned `Response(..., media_type="application/json")` with a `str` where the
  signature says `bytes`, which `CSRFMiddleware`'s egress rejects — and the
  error path is slower than a served one, so ~8 µs of the "saving" was the gap
  between a 500 and a 200. The mechanism's share was right; the magnitude was
  inflated by 47%.

  **This changes this ADR's standing.** Compartments are pure Python, work on
  all three tiers rather than metal alone, and needed no C, no opcode table and
  no differential parity suite. They are not a substitute — a route that needs
  all seven middlewares still pays for all seven, and misses and authenticated
  routes deliberately run the whole stack — but they take the common case, at
  ~1% of the cost of building this. What remains for a C tape is the routes that
  genuinely need every middleware, on metal only. Re-measure that residue before
  starting: the case for this ADR is now much narrower than when it was written.

## Design

### Compilation, at application boot

A new pure-Python compiler, `src/wreath/_middleware_tape.py`, turns the ordered
global middleware list into a tuple of descriptors. Each descriptor is
`(opcode, frozen_parameters)` where the parameters are already the bytes and
native objects the executor needs — never a Python callable.

A middleware compiles when its exact type is known to the compiler and its
configuration is expressible. It does **not** compile when it is a user's own
middleware, or a shipped one configured in a way no opcode covers.

**All-or-nothing, per application.** If any registered global middleware fails
to compile, the tape is discarded and the application keeps the Python tape
entirely. Partial native interpretation is forbidden — the same rule the body
validator already follows, and for the same reason: a half-native tape has two
orderings and no single place that defines the semantics.

### Execution, per request

Two entry points in C, because the tape has two halves and they run at different
moments:

- **ingress** — runs in `server_http1.c` immediately before the request would
  cross into Python, against the `_RequestContext`. An opcode may mutate the
  context (proxy rewrite, request id) or **refuse**, in which case C emits the
  complete response and the request never enters Python at all.
- **egress** — runs at `_wreath_response`, where C already owns the status,
  header list and body. Opcodes append headers and read the ingress state the
  tape recorded on the context.

Ingress runs in registration order. Egress runs in reverse. When an ingress
opcode refuses at index *i*, egress runs only the opcodes for middlewares whose
ingress completed — exactly the `error_afters` / `success_afters` prefix
semantics `_handle_http` implements today.

### Opcodes

Ordered by risk, which is also the implementation order.

| opcode | half | can refuse | frozen parameters |
| --- | --- | --- | --- |
| `SECURITY_HEADERS` | egress | no | two header blocks (plain, https) |
| `SERVER_TIMING` | both | no | header name |
| `REQUEST_ID` | both | no | header name, trust flag, echo flag |
| `PROXY_HEADERS` | ingress | no | `TrustedNetworks` |
| `CORS` | both | yes (preflight) | origin set, method set, header set, max-age |
| `RATE_LIMIT` | ingress | yes (429) | `TokenBucket`, key selector |
| `CSRF` | both | yes (403) | secret, cookie/header names, max-age, flags |

## Implementation stages

Each stage is independently shippable, independently measurable, and leaves the
tree green. **Do not start stage N+1 until stage N's verification passes.**

### Stage 0 — the seam, with no opcodes

- `_middleware_tape.py` with the compiler returning `None` for everything.
- `tape.c` / `tape.h` with the executor, ingress and egress entry points, and an
  empty opcode table.
- Wire both halves into `server_http1.c`, gated on the app exposing a compiled
  tape.
- `_compile_routes_locked` builds the tape and, when it is complete, clears
  `_has_global_http_hooks` for dispatch purposes.

**Verification:** no application compiles a tape yet, so behaviour is unchanged.
`uv run wreath-check` green; `wreath-request-trace` unchanged. This stage proves
the plumbing without changing an answer.

### Stage 1 — one egress opcode, as the load-bearing experiment

`SERVER_TIMING` egress. **Not `SECURITY_HEADERS`**, which the original page put
first on the reasoning that it was safest: it is also the *cheapest* opcode in
the stack, 0.7 µs on a good run and below the floor on a normal one. A stage
that cannot move the needle cannot test the hypothesis, and this ADR's own kill
condition would then have fired on it and closed a sound plan.

`SERVER_TIMING` is the cheapest opcode that is still comfortably measurable
(3.9–4.5 µs, against a floor of 0.06–0.5 µs) and it cannot refuse or mutate the
request, so the blast radius is a header list C already owns. It is the whole
point of the staging: prove a C opcode recovers its body's cost, on one opcode,
before six more are written.

### Stage 2 — the rest of egress

`SECURITY_HEADERS` and `REQUEST_ID` echo. Cheap, safe, and now being added to a
seam that has already demonstrated its payoff.

### Stage 3 — ingress opcodes that cannot refuse

`REQUEST_ID` mint and `PROXY_HEADERS`. `PROXY_HEADERS` mutates client, scheme
and host on the context; every later reader of those must be checked, including
the auth backend and CSRF's origin check.

### Stage 4 — opcodes that refuse

`RATE_LIMIT`, then `CORS` preflight. A refusal must emit **byte-identical**
output to the Python path, RFC 9457 problem document included, and must run the
completed prefix of egress opcodes over it.

### Stage 5 — CSRF

Last for its security risk, which is the half of the original argument that
holds. It is *not* half the body cost. Both branches need opcodes — the
`Sec-Fetch-Site` fast path that modern browsers take, and the token fallback for
everything else — and getting only the fast path into C would leave the tape
declining exactly the requests the fixture measures.

## Correctness constraints

These are not suggestions; a stage that violates one is not finished.

1. **The Python tape stays the reference.** For every request shape, the native
   tape must produce what the Python tape produces. It is not a second
   implementation with its own opinion.
2. **All-or-nothing per application**, as above.
3. **No process-global mutable state in the native tape** — ADR 0007. The
   descriptors and any counters belong to the application object, not to the
   module. A rate-limit bucket is already a Python-owned native object; it stays
   that way.
4. **Ordering and partial unwind are preserved exactly**, including which egress
   opcodes run when an ingress opcode refuses.
5. **A refusal is byte-identical** to the Python refusal, `Vary` headers and
   all.
6. **Observability does not regress.** The recorder's phase marks, the log
   scope, and `route_outcome` must be set for a tape-answered request as they
   are for a Python-answered one, or the tape must decline requests where a
   recorder is armed. Declining is acceptable; silently losing a record is not.
7. **A shape the tape does not understand falls back**, request by request, to
   the Python tape — HTTP/2 and HTTP/3 dict scopes have no `_RequestContext` and
   must never reach the executor.

## Verification

Per stage, all of it:

- **Differential test** (`tests/test_middleware_tape_parity.py`, new): drive a
  matrix of request shapes through one application with the tape forced on and
  forced off, and assert the responses are identical after normalizing `date`
  and any minted id. This is the gate that matters — everything else supports
  it.
- `uv run wreath-check --docs` green.
- `uv run wreath-sanitize core --leaks` and `wreath-sanitize server` for the new
  C.
- `uv run wreath-native-lint` clean; waive in place with a reason or fix.
- `uv run wreath mutant` over `_middleware_tape.py` **in both execution modes**
  (`WREATH_PURE=1` for the second), scored by the survived-wins rule in
  `AGENTS.md`.
- `uv run wreath-tape-decomp` before and after, recorded in the stage's note.
- `uv run wreath-request-trace --check`; crossings should fall, and the baseline
  is re-recorded with a reason.

## Expected result, and the honest bound

An application on metal whose whole stack compiles should lose most of the
**16.5 µs** the tape costs, and gain the specialized dispatcher's ~1.5 µs as a
side effect. On a ~30 µs request that is worth having, and it is smaller than
the 26.8 µs the first version of this page promised.

Three things temper it, and they are the reason this stays a plan:

- **Sub-additivity.** The bodies sum to ~19–24 µs but the tape costs ~17 µs.
  Whatever the opcodes give back, expect 20–30% less than the per-body table.
- **All-or-nothing means no partial credit.** With a flat cost distribution and
  no dominant item, six of seven opcodes ship *zero* improvement — the tape is
  discarded and the Python path runs. Every stage before the last is
  infrastructure, and the ADR banks nothing until stage 5 lands.
- **The bound is a projection.** That a C opcode recovers its body's cost is
  precisely what stage 1 exists to test.

**Kill condition.** If stage 1's differential does not show `SERVER_TIMING` in C
recovering a clear majority of its 3.9–4.5 µs, the premise is wrong and this ADR
should be closed rather than continued. Measure it with alternating round
direction and against an arm in the same interleaved run — the first version of
this page was killed by neither of those, and the plan it produced would have
spent five stages of C on a 14 µs cost that did not exist.

## Files

| path | role |
| --- | --- |
| `src/wreath/_middleware_tape.py` | new — compiles middleware to descriptors |
| `src/wreath/_native/tape.c`, `tape.h` | new — the executor and opcode table |
| `src/wreath/_native/server_http1.c` | ingress and egress call sites |
| `src/wreath/app.py` | build the tape in `_compile_routes_locked`; teach `_select_dispatch` that a complete tape means no Python hooks |
| `tests/test_middleware_tape_parity.py` | new — the differential gate |
| `benchmarks/bench_biomimetic_paths.py` | existing — the compartments and reflex ceilings |
| `wreath-tape-decomp --header sec-fetch-site:same-origin` | the per-middleware table above |
| `docs/agents/manifest.json` | new sources and tests, same change |
