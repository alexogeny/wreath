# Single-pass request pipeline plan

## Goal

Refactor Neo's HTTP request control flow so every request is classified by the route tree once, misses and denials terminate at the cheapest valid stage, public and missing routes never authenticate, protected routes authenticate at most once, and explicitly global ingress/finalization hooks cover early responses without adding work to applications that do not configure them. Prove control-flow behavior with red tests and performance with retained, repeated before/after benchmarks.

## Repository constraints

- Target CPython 3.14 and preserve ASGI semantics.
- Keep `src/neo` dependency-free and application-owned state explicit.
- Preserve pure-Python/native C and decision/trie observable parity.
- Preserve route precedence, overlapping literal/parameter behavior, `HEAD` fallback, exception handlers, static files, and native response emission.
- Keep existing route-fused middleware for matched-route work.
- Treat CSP and browser security headers as response finalization, not request scanning.
- Treat CORS preflight as a classification branch; ordinary CORS remains response handling.
- Do not introduce a generic XSS request scanner.
- Add rate-limit hook positions and recording test doubles in this change; a distributed fingerprint limiter is separate work.
- Never encode benchmark timing thresholds in pytest or claim a win from one run.

## Progress checklist

### Recorded baseline and red tests

- [x] Record environment metadata and repeated baseline results for public, protected-allow, protected-deny, middleware, and router-pruning scenarios. Baseline artifact: `benchmark-results-pipeline/baseline.json/20260715T061829Z.json`; dedicated miss/preflight baselines will be added with the focused benchmark.
- [x] Add red request-pipeline tests for exact stage ordering and early termination (`tests/test_request_pipeline.py`; initial run fails at the intended missing global seam/order assertions).
- [x] Add red tests proving public and missing routes skip authentication.
- [x] Add red tests proving protected authentication/policy checks run at most once and policy authorization precedes action/route hooks.
- [x] Add red tests proving auth failures still pass through configured global finalizers.
- [x] Add initial red tests for 404 behavior; dedicated CORS preflight and static finalizer cases remain pending.
- [x] Extend decision/trie parity tests for protected routes, absent auth backends, overlap precedence, and middleware ordering.
- [x] Add pure/native tests proving classification returns an opaque ticket whose resolution does not restart search.

### Router classification contract

- [x] Add an internal route outcome/resolution contract in `src/neo/routing.py`.
- [x] Replace pure decision-router `probe()` composition with one-pass classification in `src/neo/_pure/dtrouter.py`.
- [x] Add cheap ticket resolution against a caller capability mask without rewalking method/path decision nodes.
- [x] Implement equivalent one-pass classification and ticket resolution in `src/neo/_native/dtrouter.c` with correct ownership and cleanup.
- [x] Provide the same application-facing classification contract for pure/native trie routers without changing route precedence.
- [x] Keep the existing `match()` API for compatibility and focused routing benchmarks.
- [x] Preserve static-route fast paths, dynamic parameter extraction, overlapping literal/parameter behavior, and `HEAD` fallback.

### Global and route-local execution seams

- [x] Add a small typed global hook contract for ingress before-hooks and response after-hooks.
- [x] Keep `add_middleware()` and `MiddlewareTape` route-fused semantics unchanged.
- [x] Compile global hooks once and skip stage coroutine dispatch when none are configured.
- [x] Make global before-hooks capable of short-circuiting before classification.
- [x] Unwind applicable global after-hooks exactly once for route, static, miss, auth-denial, exception, and ingress-short-circuit responses.
- [x] Expose explicit request-owned route outcome metadata to global hooks without hidden global state.
- [x] Add typed ingress, miss, pre-auth, identity, and action rate-limit hook positions using `PipelineHooks` and recording tests.

### Application dispatcher

- [x] Refactor `Neo._handle_http()` to finalize responses centrally and classify decision-routed requests once.
- [x] Preserve lazy `Request` construction when no global hook and no selected branch requires it.
- [x] Dispatch definite misses directly to preflight, static mount, miss handling, or 404 without auth/binding/route middleware.
- [x] Dispatch public candidates without calling the auth backend even when credentials are present.
- [x] Dispatch protected candidates through pre-auth hooks, one authentication, identity hooks, local capability resolution, external policy authorization, action hooks, route middleware, and endpoint.
- [x] Ensure every denial skips all later request stages.
- [x] Preserve exception coercion, custom exception/status handlers, `HEAD`, native `neo.response`, streaming/file responses, background work, and websocket/lifespan behavior.

### First-party security middleware

- [x] Run `TrustedHostPolicy` in the global ingress seam before authentication.
- [x] Run CORS preflight from the global ingress branch before endpoint authentication.
- [x] Run ordinary CORS response handling as a global finalizer so configured 401/403/static responses can receive headers.
- [x] Run `SecurityHeadersPolicy` as a global finalizer (including 404s when configured).
- [x] Keep `SessionPolicy` route-local.
- [x] Reject multiple CORS preflight registrations instead of silently replacing one.

### Benchmarks and documentation

- [x] Add `benchmarks/bench_request_pipeline.py` for public, miss, and protected allow/deny across small and large route sets; CORS preflight remains covered by correctness tests rather than the router microbenchmark.
- [x] Measure routing with no-op/cached authentication overhead separately from end-to-end async authentication scenarios.
- [x] Extend `benchmarks/scenarios.py` with missing and auth-missing alongside the existing allow/denial scenarios; a dedicated preflight load scenario remains optional follow-up.
- [x] Retain repeated post-change raw results with the same environment and settings as the baseline.
- [x] Compare repeated structural and end-to-end results; current evidence shows large classifier wins but mixed end-to-end throughput, so no blanket speedup is claimed.
- [x] Update lifecycle, middleware/security, routing, benchmark, reference, and agent-facing documentation.

### Verification

- [x] Run focused request-pipeline, routing parity, auth, middleware, CORS, security, static, and native parity tests.
- [x] Run `uv run pytest` (960 passed, 41 skipped, 21 deselected at the recorded full run).
- [x] Run `uv run ruff check .`.
- [x] Run `uv run ty check`.
- [x] Run `uv run --group docs mkdocs build --strict`.
- [x] Review changed APIs/docs and explicitly record mixed benchmark evidence without a blanket performance claim.

## Recorded benchmark evidence

Artifacts:

- baseline end-to-end: `benchmark-results-pipeline/baseline.json/20260715T061829Z.json`
- focused large-tree comparison: `benchmark-results-pipeline/classifier-after.json`
- focused small-tree comparison: `benchmark-results-pipeline/classifier-small-after.json`
- isolated post-change end-to-end: `benchmark-results-pipeline/after-optimized.json/20260715T065849Z.json`
- new miss/auth-missing scenarios: `benchmark-results-pipeline/new-scenarios-after.json/20260715T065700Z.json`

The focused 385-route median changed from roughly 3,363 ns to 576 ns for protected allow and 5,535 ns to 567 ns for protected deny. Public classification stayed approximately flat (261 ns versus 269 ns), while definite misses improved from 269 ns to 185 ns. The one-route benchmark showed the same direction with smaller protected gains.

The short end-to-end runs are mixed and noisy: public throughput stayed close, while protected scenarios varied and some post-change runs regressed. One post-change run was accidentally launched concurrently with another load run and is retained but must not be used for comparison. No blanket end-to-end performance win is claimed; longer isolated trials and profiling should precede further hot-path optimization.

## Intended control flow

```text
minimal global before-hooks
  telemetry start / trusted host / cheap ingress limit

single route classification
  MISS       -> miss limit -> CORS preflight or static or 404
  PUBLIC     -> route/action limit -> route middleware -> endpoint
  PROTECTED  -> pre-auth limit -> authenticate once
             -> identity limit -> local capability authorization
             -> external policy authorization -> action limit
             -> route middleware -> endpoint

global response finalizers
  CORS / CSP and browser headers / telemetry completion

response emission and background work
```

CSP and ordinary CORS handling are not pre-routing blockers. A full client fingerprint must be computed lazily only for a configured route/stage; the cheapest ingress limiter should use already-available transport/header facts.

## Router resolution requirements

A classification result must contain enough opaque compiled information to resolve the request after authentication without searching the tree again. It may contain one candidate or an ordered candidate set when route overlap requires it. Resolution must preserve current semantics for cases such as:

```text
/users/me      protected literal
/users/{id}    public parameter
```

The implementation must not expose or invoke a protected handler before authorization. For the common one-candidate leaf, the native representation should avoid allocating a general candidate list.

Expected structural result:

```text
public request       one classification traversal, no auth
missing request      one classification traversal, no auth
protected allow      one classification traversal, one auth
protected deny       one classification traversal, one auth
```

## Correctness rules

- Missing routes never invoke authentication, authorization, binding, route middleware, or handlers.
- Public routes never authenticate solely because credentials were supplied.
- Protected routes authenticate at most once.
- Classification walks method/path routing structures at most once.
- Ticket resolution does not restart route search.
- Every rejection prevents every later request stage.
- Configured global finalizers run exactly once for their selected response classes.
- CORS preflight does not require endpoint authentication.
- Decision and trie routing produce the same status, handler choice, and observable stage ordering.
- Existing route precedence cannot change accidentally.
- Global hook/limiter state is owned by the application or hook instance.
- The no-global-hooks public hot path remains allocation-light.

## Planned files

```text
src/neo/app.py
src/neo/routing.py
src/neo/_pure/dtrouter.py
src/neo/_pure/router.py
src/neo/_native/dtrouter.c
src/neo/_native/router.c
src/neo/middleware/base.py
src/neo/middleware/cors.py
src/neo/middleware/security.py
src/neo/middleware/__init__.py

tests/test_request_pipeline.py
tests/test_routing_modes.py
tests/test_auth_pipeline.py
tests/test_auth_verifier_call_count.py
tests/test_security_middleware.py
tests/test_framework_features.py

benchmarks/bench_request_pipeline.py
benchmarks/apps.py
benchmarks/scenarios.py
benchmarks/README.md

docs/concepts/request-lifecycle.md
docs/guides/middleware-errors.md
docs/guides/routing.md
docs/reference/services.md
docs/agents/index.md
docs/agents/manifest.json
```

## Acceptance checks

- The new tests demonstrably fail against the old dispatcher for ordering/traversal reasons and pass after implementation.
- Public, miss, protected-allow, and protected-deny requests classify once.
- Auth verifier and policy provider call counts remain exactly one where applicable.
- Trusted-host and ingress-limit rejection happen before authentication.
- 404 requests skip fingerprinting, auth, route middleware, binding, and handlers.
- Configured CORS/security finalizers can cover protected denials and static responses.
- Pure/native decision and trie implementations pass parity tests.
- Repeated before/after benchmark artifacts contain enough metadata to reproduce the comparison.
- All project tests, lint, type checks, and strict docs build pass.
