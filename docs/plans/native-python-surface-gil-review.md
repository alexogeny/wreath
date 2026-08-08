# Native-path Python surface and GIL hotspot review

**Status:** measured review and investigation priorities; no implementation approved.

**Date:** 2026-07-18

Related:

- `docs/plans/native-performance-drain-audit.md`
- `docs/plans/native-gil-strategy.md`
- `docs/plans/worker-tape-architecture-baseline.md`
- `docs/plans/request-boundary-crossings.md`
- `src/wreath/_devtools/measure.py`

## Question

Where does a request running on Wreath's native server still execute substantial or avoidable Python before and after the user handler, and which native operations can hold the event-loop thread or GIL long enough to deserve separate investigation?

“Python surface” and “GIL hotspot” are not synonyms. A Python frame can be cheap and semantically necessary. Conversely, a single C call can monopolize the event-loop thread for milliseconds. This review uses exact frame/crossing counts plus decomposition measurements and does not infer elapsed cost from source size alone.

## Current measured shape

The current realistic request trace reports:

| Phase | Calls into C | Python frames |
| --- | ---: | ---: |
| Ingress | 2 | 6 |
| Middleware | 24 | 29 |
| Routing | 4 | 5 |
| Authentication/authorization | 7 | 10 |
| Handler | 62 | 62 |
| Egress | 32 | 25 |
| **Total** | **131** | **137** |

Before route activation, Wreath executes **50 Python frames and 37 calls into C**. The intended native shape—ingress, routing, authentication, and authorization staying native until route activation—is therefore not yet achieved structurally.

The current decomposition adds cost information:

- route-only request: **1.68 µs**;
- roles authentication: **+2.51 µs**;
- policy on top of roles: **+0.46 µs**;
- scripted ORM read on top: **+11.96 µs**;
- realistic bare application in the middleware harness: **16.56 µs**;
- full seven-middleware global tape: **31.21 µs**, or **+14.65 µs / 46.9%**;
- fixed cost of enabling any global hook: approximately **1.77 µs**;
- fourteen non-suspending before/after awaits account for only about **0.32 µs**.

The middleware result is important: async syntax is not the main cost. Request/state construction, internal bookkeeping, header/cookie work, policy glue, response mutation, and Python/native boundary traffic are.

## Validation pass and exact problem map

The three tools were rerun after the initial review with the same configured rounds/iterations. `wreath-request-trace` reproduced the exact same 131 C entries, 137 Python frames, and 50 pre-activation frames. The middleware tape result reproduced within normal run variation: +14.42 µs initially and +14.65 µs on validation; the estimated fixed-hook component moved from 2.04 to 1.77 µs and remains a grouped estimate, not an attribution to one line. Authentication reproduced at +2.50/+2.51 µs and policy at +0.42/+0.46 µs. ORM total time varied, but query construction plus shape derivation was stable at 2.55/2.54 µs. Findings below distinguish measured cost from structural evidence.

| Finding | Exact current spots | Validation | Confidence |
| --- | --- | --- | --- |
| Full native scope materialized by proxy middleware | `request.py:188-194` materializes `_asgi_scope()`; `middleware/proxy.py:62-66` reads client through `request.scope`; `proxy.py:68-101` obtains and mutates the dict | Trace reports three `Request.scope` frames; source proves materialization, but allocation/time has not been isolated | High structural; medium cost |
| Internal route bookkeeping forces public State | `app.py:474-475` constructs `Request`, writes `route_outcome`, then enters hooks; additional outcome writes occur around `app.py:508`, `554`, and `640-641`; `request.py:181-185` lazily creates `State`; `state.py:15-34` wraps a dict | Trace reports seven `Request.state`, six `State.__setattr__`, and one `State.__init__` frames; fixed-hook group is 1.77–2.04 µs | High structural; grouped cost only |
| Global ingress/egress loops stay Python | `app.py:475-482` awaits before-hooks; `app.py:683-720` reverses/awaits after-hooks, coerces, builds native response message, and dispatches background work | Full tape repeatedly costs 14.42–14.65 µs; await calibration prices only ~0.31–0.32 µs | High measured |
| Built-in middleware stores framework data in generic State | CSRF `middleware/csrf.py:192-215`; request ID `middleware/request_id.py:74-88`; timing `middleware/timing.py:56-69` | Exact trace catches request-ID/timing/CSRF State frames, but each storage component is not separately priced | High structural; unknown individual cost |
| Bearer/auth control is Python around one user verifier | `app.py:920-923`, `937-976`; `app.py:1070+`; `_auth/backends.py:37-51` converts header to str, partitions it, invokes verifier, and conditionally inspects awaitability | Roles +2.50/+2.51 µs; policy incremental +0.42/+0.46 µs | High measured group |
| Cached ORM plans still require rebuilt query/shape/binds | `orm/compiler.py:146-168` derives shape, looks up cache, recollects binds, and constructs `CompiledQuery`; native shape dispatch at `compiler.py:689-702`; `orm/session.py:299-303` compiles on every fetch | Build+shape stable at 2.55/2.54 µs; total scripted read varied from 6.58 to 8.14 µs | High for repeated cost; total noisy |
| Egress control remains Python | `app.py:683-720`, especially after-hook loop 693-699 and message construction 701-715 | Trace reports 25 egress frames/32 C entries; not separately timed from middleware finalizers | High structural; medium cost |
| Native large-input functions have no released region | JSON recursively touches Python values in `json.c:309-394` and parses/constructs strings, objects, and arrays at `665`, `766`, and `825` under entry points `399`/`977`; multipart's full-body parser begins at `multipart.c:66` and scans parts in the loop at `125+`; template execution enters at `templates.c:233` and interprets Python tuple instructions in the loop at `267+`; PostgreSQL allocates records and performs nested column/row Python decode at `postgres/decode.c:420-478`, entered through `482-505`; hydration creates/merges Python models in `postgres/hydrate.c:488+` and the row loop at `612+` | Static search finds no `Py_BEGIN_ALLOW_THREADS` in current native sources; elapsed same-loop cost is not yet decomposed | High structural; unmeasured hotspot |

Two qualifications matter. First, `SessionMiddleware` intentionally exposes `request.state.session` at `middleware/sessions.py:83-90`; moving it to a private scratch slot would change a public usage pattern unless replaced with a literal public API. Second, generic `request.scope` access must continue to materialize a conforming dictionary—the finding is specifically that Wreath's own proxy middleware takes that expensive generic path.

## Priority findings

### P0: built-in proxy handling materializes the full ASGI scope

`Request` is designed to retain the native `_RequestContext` and materialize a scope dictionary lazily. Direct `method`, `path`, `query_string`, and `headers` properties read native context attributes without creating the dictionary.

`ProxyPolicy._peer_trusted()` instead uses `request.scope.get("client")`, and its mutation path obtains `request.scope` before changing client/scheme. On the native path this calls `_RequestContext._asgi_scope()` and constructs the full ASGI dictionary even though the built-in middleware needs only client, scheme, and header mutation.

The trace shows three `Request.scope` frames before activation. Once materialized, the request loses much of the allocation benefit of the native context.

**Assessment:** credible unnecessary Python/object surface, likely the lowest-risk structural target.

**Investigation:** add ablations for trusted and untrusted peers that count whether `_asgi_scope()` is invoked and measure allocation/time. Compare the existing middleware with test-only direct native-context client/scheme access. Preserve generic dict-scope behavior on third-party servers.

**Possible direction:** add literal `Request.client` and `Request.scheme` accessors plus narrowly owned mutation methods that delegate to `_RequestContext` when native and update a dict when portable. Do not expose the native context publicly, and do not change arbitrary `request.scope` semantics.

### P0: global middleware is the largest measured pre-handler framework surface

Seven built-in middleware components add 14.42 µs to a 16.60 µs bare application. They account for 29 pre-activation Python frames and much of egress's 25 frames. Installed individually they appear to sum to 26.69 µs because each arm repays the roughly 2.04 µs fixed hook cost.

The current execution model is split:

- `Wreath._handle_http()` manually loops global before-hooks;
- each built-in hook remains a Python coroutine;
- hooks call narrow C helpers for parsing, masks, signing, matching, or formatting;
- `_finish_http()` loops reversed after-hooks and coerces each result;
- request and response control remain Python throughout.

**Assessment:** confirmed material cost, but not all of it is unnecessary. CORS, CSRF, proxy trust, rate limiting, request IDs, timing, and security headers perform real work. The opportunity is cumulative fusion and reduced object traffic, not deleting hooks or merely making them synchronous.

**Investigation:** extend `wreath-tape-decomp` with grouped ablations:

1. fixed hook activation with no policy work;
2. request construction only;
3. internal state writes only;
4. shared header-index build;
5. all ingress policies without after-hooks;
6. all after-hooks without ingress;
7. one fused test-only built-in policy runner with identical results.

Require exact response/header/short-circuit parity for hit, miss, static, denial, preflight, and exception paths.

**Possible direction:** compile built-in global policies at startup into one immutable descriptor and execute one native ingress operation plus one native egress operation. Custom Python hooks remain ordered fallback instructions. This is justified only if the cumulative ablation clears noise; translating one hook at a time will repeatedly repay the same fixed cost.

### P0: public `State` is used for private framework bookkeeping

The first global hook eagerly creates `Request` and writes `request.state.route_outcome = "ingress"`. The trace attributes seven frames to `Request.state` and six to `State.__setattr__`. `State` itself wraps a Python dictionary and generic attribute dispatch.

Built-in CSRF, request-ID, timing, and session middleware also store private keys in public request state. Some values are intentionally user-observable through helper APIs, but the storage mechanism need not be the generic public namespace.

The measured fixed hook cost explicitly includes eager `Request`/`State` construction and `route_outcome` bookkeeping.

**Assessment:** confirmed avoidable mechanism for internal control state. Public request state remains necessary; forcing it for framework internals is not.

**Investigation:** ablate only `route_outcome`, then all built-in private state accesses, while keeping policy work identical. Count State construction and dict operations. Do not infer the whole 2.04 µs fixed cost belongs to State.

**Possible direction:** private fixed request slots or a compiled middleware scratch array for route outcome, timing start, CSRF token/issue flag, request ID, and session ownership. `Request.state` stays lazy for application use. Public helpers such as `csrf_token()` and `request_id()` read the private slot through a literal method rather than a themed or opaque API.

### P1: authentication's built-in path remains Python-controlled

Roles authentication adds 2.50 µs; policy evaluation adds only another 0.42 µs. The trace includes Python frames for backend `authenticate`, request header conversion, verifier callback, identity assignment, capability-mask construction, and authorization dispatch.

The verifier is user Python and is the legitimate activation boundary. Header parsing, bearer syntax, identity-mask extraction, and built-in set requirements do not necessarily need separate Python control frames.

**Assessment:** material but semantics-sensitive. Custom backends and policy callbacks must stay Python. The opportunity is to enter Python once for verification, not to pretend authentication can be wholly native.

**Investigation:** decompose bearer extraction, verifier call, identity construction, role/permission mask, and requirement resolution. Compare byte-token delivery against current string conversion. Test challenge handling, malformed headers, absent credentials, verifier exceptions, denial finalizers, and identity visibility.

**Possible direction:** a compiled built-in-auth instruction extracts bearer bytes from native headers, activates the Python verifier once, then resumes native mask/requirement resolution. Any custom backend remains a Python instruction.

### P1: ORM query construction is a larger Python surface than cache lookup

On the validation run, building `Select+where` costs 1.18 µs, native shape-key derivation costs 0.25 µs, prebuilt compilation costs 1.36 µs, and build plus compilation costs 4.22 µs. The more stable grouped observation is query construction plus shape derivation: 2.55 µs initially and 2.54 µs on validation. Total scripted `fetch_one` varied from 6.58 to 8.14 µs, so its percentage moved from 39% to 31%; the report does not treat either total as a fixed constant.

The query object is rebuilt and its shape re-derived per request to find an already compiled statement.

**Assessment:** confirmed avoidable repeated Python construction for stable query shapes. This is not primarily a GIL-release problem and should not trigger a native worker/tape architecture.

**Investigation:** compare current rebuild, retained immutable `Select`, startup-compiled query template with bind slots, and direct cached `CompiledQuery` execution. Include dynamic predicates, optional clauses, includes, limits, and registry invalidation.

**Possible direction:** public or internal prepared query templates compiled at registry/application startup, with request-time bind extraction only. Keep unrestricted dynamic query construction available. Prefer this explicit cache shape over translating the whole Python ORM session to C.

### P1: egress still has a large Python control surface despite native emission

The trace records 25 egress Python frames and 32 calls into C. `_finish_http()` unwinds each global after-hook in Python, repeatedly coerces responses, checks HEAD/native-extension conditions, constructs a `wreath.response` message dictionary, and awaits send. The native server then emits the bytes efficiently.

Most measured middleware cost includes these finalizers. Native one-shot emission does not remove Python response-policy work before the send.

**Assessment:** structurally large and probably material as part of the full tape, but response-message construction itself is not separately priced.

**Investigation:** split after-hook unwind, response coercion, native-message construction, native send, HEAD wrapper, and background dispatch. Measure ordinary `Response`, prepared response, streaming, file, exception, and HEAD independently.

**Possible direction:** include built-in response finalizers in the compiled global policy descriptor. Consider a private direct native-response call only if message-dictionary construction independently clears noise; generic ASGI send remains authoritative.

## GIL-held and event-loop-blocking native candidates

### Native JSON

`json.c` scans and emits large buffers, recursively traverses Python containers, constructs Python objects during decode, and contains no released region. Requests may buffer up to 16 MiB, so pathological or legitimately large JSON can hold the event-loop thread materially longer than route/middleware work.

The encoder cannot generally release the GIL because it continuously touches Python values. Decoder byte scanning could theoretically become a native token tape, but Python materialization remains and peak memory may double.

**Priority:** measure high; redesign only with evidence. Add size/depth/string-escape/number-density sweeps with same-loop heartbeat and total request tails. Compare stdlib and Wreath native behavior for equivalent semantics. A worker/tape proposal must price materialization and peak RSS, not just parse time.

### Multipart parsing

`multipart.c` scans boundaries and headers while constructing Python part/header objects. Aggregate request/form limits bound memory but still permit multi-megabyte GIL-held parsing. It is a likely event-loop stall for large forms, yet its Python-object interleaving prevents a trivial release region.

**Priority:** measure high for upload workloads. Sweep body size, part count, boundary density, header bytes, and retained-file bytes. Distinguish raw scanning from object creation. A native span tape is plausible only if it avoids copying and preserves all current limits/errors.

### Template rendering

`templates.c` executes a compiled opcode tape but repeatedly accesses Python tuples, mappings, iterators, values, and configured types. Large loops can hold the event-loop thread. Flattening values into a worker-safe tape may cost as much as rendering and change dynamic behavior.

**Priority:** workload-specific. Extend template benchmarks with same-loop heartbeat and large repeated rows. Prefer chunked/streaming rendering or explicit offload experiments before a second tape representation.

### PostgreSQL decode/hydration

Wire parsing and storage are broadly native, but scalar decode, records, identity maps, relationship assembly, and model hydration construct Python objects. Large result batches can monopolize the loop. General GIL release is not safe; isolated byte kernels such as large hex `bytea` are the only simple candidates.

**Priority:** measure by phase. Extend decode benchmarks with heartbeat and rows/columns/null-density/fan-out sweeps. Prefer batching/yield boundaries or query limits over native workers unless one isolated kernel dominates.

### Compression

Production compression uses CPython's maintained native `zlib`; Wreath's direct-zlib extension was rejected by its retention gate. Even if zlib releases the GIL internally, a synchronous call still occupies the event-loop thread.

**Priority:** follow `worker-tape-architecture-baseline.md`: first compare direct compression with bounded application-owned thread offload. Do not reopen native zlib glue without new evidence.

### WebSocket masking

A controlled A/B/A experiment found that release enabled another Python thread but caused request-thread reattachment to wait for the competing thread's scheduling quantum. The experiment was reverted. Large frames still block the same event loop during copy/XOR.

**Priority:** closed for inline GIL release. Revisit only as explicit worker offload for unusually large frames, including protocol ordering and backpressure costs.

### HPACK Huffman decode

Whole-protocol measurements place the bounded 16 KiB case around 31.3 µs and common cases around 2–5 µs.

**Priority:** closed. No release or worker architecture is justified.

### Routing and small policy primitives

Routers, capability masks, header lookup, origin matching, CSRF signing, rate-limit arithmetic, and response-header helpers are short operations. Their issue is repeated orchestration/boundary traffic, not long GIL-held kernels.

**Priority:** do not add GIL release. Fuse cumulative built-in policy work only after grouped ablation.

## Surfaces that are large but not native-path problems

Source size alone should not drive C work:

- CLI, dev server, OpenAPI, type generation, migration/introspection, testing, and benchmark reporting are cold/control-plane code.
- Registry/model/constraint compilation is predominantly startup work and benefits from explicit compilation rather than worker execution.
- `webhooks.py` is large but workload-specific; delivery/outbox policy belongs in Python unless a measured byte kernel emerges.
- pure server/PostgreSQL modules are parity references and inactive when their native backends are selected.
- response-bound background tasks are deliberately Python and occur after response emission; they are not pre-activation native-path leakage.

## Recommended order

1. **Remove native-scope materialization from built-in proxy handling**, after an allocation/time ablation confirms it.
2. **Price and remove private framework use of public `State`**, beginning with `route_outcome`.
3. **Add grouped global-middleware decomposition** and test one fused built-in ingress/egress descriptor.
4. **Add prepared ORM query templates** if direct cached execution confirms the measured 2.55 µs construction budget is removable.
5. **Decompose built-in auth around the one unavoidable Python verifier activation.**
6. **Split egress measurements** before proposing a direct response ABI.
7. **Add same-loop large-input harnesses** for JSON, multipart, templates, and PostgreSQL batches.
8. Keep WebSocket inline release and HPACK work closed unless workload or limits change.

## Acceptance rules

- Preserve generic ASGI behavior and third-party server support.
- Preserve custom middleware/auth/exception hooks as explicit Python activation points.
- Do not equate removed frames with elapsed wins; use exact trace counts and cumulative decomposition.
- Do not call GIL release a same-loop responsiveness fix.
- Retain A/A floors and every before/after trial.
- Measure allocation/RSS and tail latency together with throughput.
- Keep pure/native semantics identical where a pure twin exists.
- Any private native fast path must fall back explicitly rather than silently changing ordering, cancellation, backpressure, or finalizer behavior.

## Bottom line

The largest confirmed unnecessary native-path Python mechanisms are **full scope materialization by built-in proxy middleware** and **generic public-State bookkeeping for private route control**. Scope materialization is source-confirmed but still needs an isolated timing; State contributes to a measured 1.77–2.04 µs fixed-hook group but is not solely responsible for it. The largest confirmed material surface is the **global middleware ingress/egress tape**, reproduced at 14.42–14.65 µs on the current realistic app; its policy work is real, so only cumulative fusion is credible. ORM query construction plus shape derivation is the clearest stable handler-side repeated Python cost at 2.54–2.55 µs.

The most plausible large-input GIL/event-loop stalls are JSON, multipart, template loops, and PostgreSQL batch materialization, but none yet has the decomposition needed to justify workers or tapes. WebSocket release was measured and rejected; HPACK is too small. The next work should reduce avoidable Python object/control surfaces first, then measure large-input kernels rather than adding blanket GIL-release regions.
