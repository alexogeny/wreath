# Wreath native static performance investigation

## Scope and method

This is a source-level forensic profile, not a timing result. Per request, no benchmark was run and no existing benchmark output, decomposition output, trace baseline, or profiler artifact was inspected. The investigation followed the HTTP/1 static-route path, the default decision router, request construction, response emission, and CPython calls in native loops. It also ran the static native complexity and boundary linters.

## Step 1 — findings

### 1. Default decision-router compilation contains quadratic and super-linear work — high confidence

`dnode_build()` in `src/wreath/_native/dtrouter.c:361` scores each unused segment position. For every candidate at a position, lines 377–389 scan earlier candidates to count distinct literals. That is O(segments × candidates²) before recursion. Lines 439–447 then build every literal branch from that literal's routes plus every parameter route, recursively duplicating parameter-route work across branches. The source comment at the top of the file explicitly describes this folding.

This is primarily startup cost, but `Wreath.__call__()` and `_wreath_http()` compile dirty routes on entry (`src/wreath/app.py:420` and `src/wreath/app.py:437`). If a benchmark starts sampling before compilation is forced, the first request pays it. Large mixed literal/parameter tables can also increase instruction-cache and memory pressure after compilation. The bitset backend avoids branch folding but scans route bitset words during matching (`src/wreath/_native/dtbitset.c:1170`).

### 2. Every complete HTTP/1 head is searched twice for its terminator — high confidence

`drive_head()` performs a resumable `find_sub_from(..., "\r\n\r\n")` at `src/wreath/_native/server_http1.c:2766`. Once complete, it calls `wreath_http_parse_request_parts()` at line 2796. That parser immediately calls `wreath_memmem(..., "\r\n\r\n")` again at `src/wreath/_native/http.c:91`. This is linear rather than quadratic, but it repeats a full request-head pass on every request and is likely visible for header-heavy traffic.

### 3. Parsing constructs many Python objects before route activation — medium-high confidence

The parser allocates a Python list for headers, then lowercased name bytes, value bytes, a tuple, and a list append per header (`src/wreath/_native/http.c:136–205`). It also allocates the target as bytes. `begin_request()` then copies the target into `raw_path` and `query_string`, decodes another Unicode path, allocates a fresh receive queue, and creates a request-context GC object (`src/wreath/_native/server_http1.c:2188–2254`). Common header names are cached, but values and pairs are not.

The boundary linter confirms Python object traffic inside the parse loop (NB001). This is expected for ASGI compatibility, but it means ingress is native in control flow rather than allocation-free. A future native-app path could retain slices/native header storage until `Request.headers` or `Request.scope` is requested.

### 4. The eager static-response path still pays Task construction and a Python task-state probe — medium-high confidence

`spawn_app_task()` crosses into the Python app, constructs an `asyncio.Task` with `eager_start=True`, then invokes the cached `Task.done` descriptor and converts its result (`src/wreath/_native/server_http1.c:1918–1959`). Suspended requests additionally call `Task.add_done_callback` at line 1968. Thus even a handler whose receive/send awaits complete synchronously pays Task allocation plus a second Python call solely to discover eager completion.

This is a strong hot-crossing candidate, but removing Task construction or the done probe requires preserving cancellation, contextvars, exception reporting, and coroutine ownership.

### 5. The nominal one-shot response still builds and decodes a Python dict — medium confidence

`Wreath._finish_http()` builds a four-key `{"type": "wreath.response", ...}` dict and calls native `send` (`src/wreath/app.py:662–675`). `http_asgi_send()` then performs dict lookups and response validation before encoding (`src/wreath/_native/server_http1.c:1360–1382`). The extension correctly halves ASGI send calls, but the Wreath-to-Wreath path still uses a generic ASGI-shaped container. A typed native response callable or vectorcall arguments could remove this container while retaining the portable path.

### 6. Dynamic Python lookups remain in native HTTP and routing functions — medium confidence

The static boundary linter reports 57 findings repository-wide. On the primary files it reports four findings in `server_http1.c`, seven in `dtrouter.c`, and two in `http.c`. Relevant examples include repeated dict lookups in `begin_response`, Python object operations in route verification, and repeated lookups while descending the decision tree. The ordinary native complexity lint is clean, so its current rules do not detect the pairwise distinctness scan above.

These findings are heuristic and include cold/error/WebSocket paths. They are leads, not measured attribution.

### 7. Other lower-confidence candidates

- `begin_request()` linearly scans the target for `?`, then path decoding scans/copies it again.
- Request timeout handling may cross to the event loop to arm/cancel timer handles; reuse limits this on steady keep-alive traffic.
- Decision-tree matching lazily creates Python segment strings for hash lookup and then creates parameter dictionaries/decoded values for parameter routes.
- Transport emission necessarily crosses from C to the Python event-loop transport's cached `write` callable.
- Header framing checks make additional passes over the Python header list after parsing.

## Step 2 — explicit review

### Review of causality

The two strongest source defects are the duplicate HTTP-head scan and quadratic decision-tree compilation. They are directly demonstrated by control flow. Neither proves the observed throughput regression: route compilation may be outside the timed window, and one extra head scan may be too small for short requests. The Task allocation/probe and Python-object ingress pressure are more plausible steady-state costs, but source inspection cannot price them.

The phrase “all of a sudden” suggests a change-point. This investigation deliberately did not inspect historical benchmark results, so it cannot identify when the slowdown began or correlate it with a commit. Runtime attribution remains unresolved until an allowed ablation experiment compares whole-request behavior with one candidate removed at a time.

### Confidence ratings

| Claim | Confidence |
|---|---:|
| Pairwise route-compile scan is quadratic | 98% |
| Parameter folding can make decision-tree compilation/storage super-linear | 95% |
| HTTP/1 request-head terminator is scanned twice | 99% |
| Eager requests pay Task creation and a `Task.done()` crossing | 98% |
| Header parsing creates O(header-count) Python objects before activation | 99% |
| Any one finding caused the benchmark regression | 35% |
| Combined steady-state crossings/object churn materially affect small-response throughput | 70% |

### Source-regression proofs

`tests/test_native_hot_path_red.py` began with three red source-regression tests and now remains as the regression suite for the resolved properties. It covers single-pass HTTP head scanning, linear decision-router distinctness, the eager-task state path, bitset startup compilation and allocation shape, native protocol queues, buffer-capacity decay, multipart payload views, and HPACK reclamation.

These tests establish source properties rather than wall-clock effects. Every property is now green and is backed by focused behavior tests; performance claims still require the project's ablation-based measurement workflow.
