# Report: augmenting the native C pathway

Status: **report, not a prescriptive plan.** Nothing below is scheduled. Written
2026-08-06 alongside the change that taught `wreath-dup-scan` to read C.

Evidence: `uv run wreath-decomp` (11 rounds, 4,000 iterations, 2,000 warmup),
`uv run wreath-request-trace`, and `uv run wreath-dup-scan --lang native --near`.

## The framing has changed, and that is the main finding

"Move more of the request into C" is finished as a direction. One realistic
request makes **126 calls into C and runs 2 Python frames**, one of which is the
ASGI entry point itself:

| phase | into C | Python frames |
| --- | --- | --- |
| ingress | 30 | 1 (`app.py:__call__`) |
| routing | 4 | 0 |
| auth | 6 | 0 |
| handler | 58 | 1 (`orm/types.py:to_wire`) |
| egress | 28 | 0 |

Forty of those 126 are `dict.get`. There is no Python left in ingress, routing,
authorization or egress to migrate. So augmenting the native pathway now means
two different things: **making the C that already runs cheaper**, and **covering
subsystems the request path does not touch**.

## Where the cost actually is

```
route only                        3.48us      -
+ auth (roles)                    9.28us   +5.80us
+ auth + policy                  10.08us   +0.80us
+ auth + policy + ORM read       37.11us  +27.02us
```

Inside one ORM read (scripted database, no I/O; 14.90us total):

```
Session() + close()               0.91us
build Select + where              1.60us
shape_of() cache key              0.41us
compile_select (prebuilt)         1.48us
build + compile_select            3.58us   <- 24% of the read
```

Calibration: **89.8 ns per Python frame**; removing eleven frames is worth about
0.99us, which is below a single A/B's noise floor and still real.

### Two things these numbers do not say

**`pre_activation: python = 1` does not mean one Python frame runs before
activation.** `request_trace.py` records a Python entry only when the caller is
*not* traced Python — "a Python-to-Python call is not a native boundary
crossing", which is correct for what the tool measures and is not what the
column is usually read to mean. An entire authentication backend runs before
activation without moving that number off 1. Measured on the same app: requiring
auth adds ten Python frames and 2.80us, and the trace's Python column does not
change.

So the trace answers "how often does C hand control back to Python", not "how
much Python runs". For the second question, count frames (`sys.monitoring`'s
`PY_START`) or time an ablation. Both are used below.

**The ORM arm measures the fallback, not the native hydrator.** `_hydrate_plan`
returns a native plan only when the connection exposes `_decode_dest`;
`_ScriptedDatabase`, which every decomp and trace scenario uses, does not. So the
27.02us arm runs `Session._hydrate` in Python, one pass per column per row, while
a real native connection runs `_fetch_into` "without a Python frame per row".
**The 27.02us therefore overstates a production single-model read**, and any
ratio taken against it — including "3.58us of 14.90us" — has an inflated
denominator. The fallback is still live in production for joined loads,
non-native storage and the reference driver, which is where the finding below
applies.

## The auth stage, attributed

Ablation at the request level, holding the identity constant so nothing measures
the fixture. Baseline is route-only; each row adds one piece.

| piece | cost |
| --- | --- |
| wreath's auth pipeline (backend returns a prebuilt identity) | **+2.80us** |
| `BearerTokenBackend`'s duplicate-safe header scan | +0.23us at 2 headers, +0.49us at 12 |
| `decode` / `partition` / scheme fold | +0.34 to +0.42us |
| the *application's* own `Identity(...)` and two `frozenset`s | +1.39us |

So of the 5.80us the decomp charges: **roughly a quarter is the sample app's own
callback, not wreath**, and **the pipeline is 77% of what remains**. Two
hypotheses were tested and refuted, which is why they are written down:

- **REFUTED: the fast path's delegation is not a cost.** `_handle_http_plain`
  hands an authenticated request back to the general `_handle_http`, re-routing
  it, and its docstring prices that at "one extra table lookup". Measured by
  adding auth to an app that delegates and to one already on the general path
  (a global middleware forces it): 2.73us against 2.85us, so the delegation is
  **-0.12us** — free, as claimed. Worth knowing that the second routing pass is
  native and so adds no Python frame and no boundary crossing: only a clock can
  see it at all.
- **REFUTED: the async verifier's coroutine is not a cost.** A *synchronous*
  verifier measured **slower** than an async one. `BearerTokenBackend` computes
  `_verifier_is_async` once "so the per-request path skips `inspect.isawaitable`
  for plain async verifiers" — and the sync path was left paying that
  `inspect.isawaitable` on every request. If the sync verifier is meant to be
  the cheap option, it currently is not.

The pipeline's 2.80us is ten Python frames, or ~280 ns each against the 89.8 ns
trivial-frame slope: these are large async bodies (`_authorize_request`,
`_handle_http`), not thin wrappers. **Frame count is as poor a proxy for cost
here as crossing count** — the same warning, one level down.

## Candidates, ranked by measured cost

1. ~~**`Session._hydrate` reads two forwarding properties per column per row.**~~
   **Done.** `_RowPlan` now resolves `(storage cell, decoder)` once per *shape*
   and the row loop reads no properties. Measured against a real PostgreSQL 17,
   10,000 rows: the Record path went from 387,983 to 530,053 rows/s, and the
   gap to the native hydrator narrowed from 4.70x to 3.67x.

   **Once per shape, not once per query, and that distinction was the whole
   finding.** Hoisting the plan into the query built it per `fetch`, which on a
   small result costs more than the per-row work it saves — the exact failure
   AGENTS.md describes, a fixed cost added to a loop that runs once. Measured
   across row counts, per-query hoisting ran **0.90x at one row and 0.85x at
   two**, breaking even near five. `fetch_one` is one row. Caching on the
   shape's existing `_CachedPlan` line instead turned every size into a win:

   | rows | 1 | 2 | 5 | 10 | 50 | 200 | 1000 |
   | --- | --- | --- | --- | --- | --- | --- | --- |
   | speedup | 1.22x | 1.25x | 1.28x | 1.31x | 1.41x | 1.40x | 1.42x |

   Eleven interleaved rounds per cell, 10–11 of 11 favouring the change at every
   size. Over a real database the same query at 10,000 rows measured 1.29x with
   15/15 rounds favouring it; at 100 rows the network round-trip swamps the
   saving entirely, which is the honest scope of the win.

   The cost is three extra calls into C per Record-path query — the plan cache's
   lock and lookup — recorded in the request-boundary baseline. They land in
   `handler`, not `pre_activation`, and a native connection never reaches this
   code at all, so a production single-model read pays none of it.

2. **A duplicate-safe native header lookup.** `BearerTokenBackend` scans
   `request.headers` in Python because it must *refuse* a repeated
   `Authorization` — a deliberate smuggling defence — and `find_header` returns
   the first match, so it cannot see the duplicate. A native
   lookup that answers "the value, or 'more than one'" measured a ceiling of
   **0.36us at 12 headers** (~9% of the stage), and nothing at 2. Real, small,
   and it scales with header count rather than being fixed.

3. **Subsystems with a pure implementation and no native twin.** Only two are
   real candidates: `_pure/snapshot.py` (`SnapshotCache`, a read-mostly
   generation cache — `kv.c` is the model to copy) and `_pure/response.py`
   (`PreparedResponse`). Two others look like gaps and are not:
   `_pure/compression.py` is a facade over CPython's own C `zlib`/`zstd`, where a
   hand-written twin would be strictly worse, and `_pure/typegen.py` is
   code generation on a cold path.

4. **The raw growable buffer is still six near-copies.** The `PyBytes`-backed
   writer is now shared (`bytes_writer.h`), but the `PyMem`-backed one is not:
   `server_http1.c`'s `buf_reserve` and `out_reserve`, `server_http2.c`'s
   `buf_reserve`, `postgres/buffer.c`'s `wreath_pg_buffer_reserve`,
   `orm_shape.c`'s `buf_ensure`, `templates.c`'s `outbuf_reserve`, and
   `edge_serve.c`'s `ebuf_add` — pairing at 0.81 to 0.94 similarity. They are
   *not* a mechanical collapse: they disagree about growth constants and about
   what happens on failure. Sharing them means deciding which policy wins, and
   that decision needs a measurement per call site, not a header.

## What the C-aware scan says about the tree's shape

`wreath-dup-scan --lang native` reports 23 exact groups and 381 redundant lines;
`--near` adds 49 pairs. Reading them:

- **`queue.c` carries one data structure twice.** `queue_new`/`heap_new`,
  `queue_init`/`heap_init`, `queue_drain`/`heap_drain`, `queue_close`/
  `heap_close`, `queue_clear`/`heap_clear`, `queue_set_waiting`/
  `heap_set_waiting` — six pairs, roughly 85 redundant lines, several exact.
  This is the largest remaining native de-duplication and the only one with a
  real design question behind it (one queue with a pluggable ordering, versus
  two types that happen to agree).
- **Reader/writer twins are noise.** `rp_add_reader`/`rp_add_writer`,
  `rp_remove_reader`/`rp_remove_writer`, `st_remove_reader_cb`/
  `st_remove_writer_cb`, the `reactor_ring` accept/poll/receive/cancel family.
  Each pair is 10 to 30 lines and reads better as siblings than as one function
  with a direction flag. Leave them.
- **`tp_traverse`/`tp_clear` boilerplate is identical across `client_http1.c`,
  `reactor_transport.c`, `server_http2.c`, `http3_connection.c` and
  `postgres/*.c`** — several pairs at 1.00 similarity. An X-macro would collapse
  it, and should not: `wreath-native-memory-lint` reads one C function at a time
  and would stop seeing reference handling that is exactly what it exists to
  check.
- **`simd.h`'s per-arch arms group as clones by construction** (sse2/avx2/neon/
  swar for `json_run`, `html_run`, `value_run`, `find`, `xor_mask`). These must
  stay written out: `AGENTS.md` requires per-architecture code be readable from
  the wrong machine, and `tests/test_native_simd.py` reads the header as *text*
  to check declaration order for arms it cannot compile. Do not macro them.

## Two failures a real database made visible

Both were found by running the suite with `WREATH_TEST_POSTGRES_DSN` set, which
this tree had not done in a long time. Neither is caused by the changes above --
each was checked -- and neither is fixed here, because both are decisions rather
than repairs.

**`tests/http3/test_adversarial.py::test_header_flood_is_bounded_not_fatal`
fails: `curl failed rc=95 (server crashed or hung?)`.** The healthy baseline
beside it passes, so HTTP/3 serves; the flood case does not. It was invisible
because `_http3` had gone unbuilt since the commits that last touched its
sources, so the tests were exercising a binary those changes never reached.
Attributed by compiling `http3_asgi.c` from `HEAD` into a variant extension and
running the same test against it: **it fails identically without the change in
this branch**, so it belongs to whatever last edited that module.

**`tests/test_log_buffer_live.py` is not parallel-safe.** Twenty-four tests,
three serial runs, 24/24 green every time; three runs at `-n 8`, a *different*
test failing each time (`test_two_buffers_on_one_stream_interleave...`,
`test_the_buffer_comes_due_on_the_time_threshold`,
`test_a_batched_append_lands_every_row_in_offer_order`). The schema is named per
xdist worker correctly, so this is not the `pg_namespace` race in AGENTS.md; it
is state shared between tests that xdist is free to split across workers.
`wreath test` runs `-n 8` by default, so anyone who sets the DSN meets this.

## A trap, for whoever counts frames next

**Count a warm request, not the first one.** An unwarmed single-request frame
count of the ORM read reported 203 added frames, of which 80 — 39% — were in
CPython's `_parser.py` and `_compiler.py`: the regex *compiler*, apparently
running inside a request. Warmed with fifty requests first, the same read adds
**69** frames and touches no regex at all. The whole regex signal was lazy
initialisation being charged to the request that happened to trigger it, and it
was the most eye-catching thing in the profile in both directions.

The same applies to the plan cache, which fills on first use and would otherwise
make `compile_select` look enormous.

## Do not

- Do not migrate `ReverseProxy` leaf by leaf. It was tried; moving the header
  transform into C measured 113.2 against a 110.1-117.2 baseline.
- Do not use cProfile to choose a target. Its per-call overhead is larger than
  most of these hot paths, and it has already caused one accepted-then-worthless
  change. Ablate: remove a piece, time the whole request.
- Do not give `wreath.edge` a pure twin.

## Keeping the shared primitives shared

`byteorder.h`, `ascii.h` and `bytes_writer.h` are reachable from every
extension. Before writing a fixed-width load, an ASCII fold, or a growable
`PyBytes`, take the one that exists — and run `uv run wreath-dup-scan --lang
native --near` before adding a helper. It is what found the four `*_grow`, the
five `*_write`, and the five hand-rolled `read_u32_*` sitting beside
`wreath_load_u32_le`.
