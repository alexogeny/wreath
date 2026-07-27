# Native/pure web workload compliance plan

## Status

Proposed implementation plan.

## Goal

Build general-purpose Neo features that naturally satisfy demanding web-framework workload and protocol rules without exposing benchmark-shaped APIs in `src/neo`. Every accelerated feature has an authoritative pure-Python implementation and an observably identical optional C implementation.

The result should support small JSON responses, reusable static responses, point database reads, independent fan-out queries, transactional read-modify-write workloads, safe server-side HTML rendering, and application data caching. A thin external adapter may map those capabilities to a third-party conformance suite, but framework code must not contain suite-specific routes, table names, constants, or randomization.

## Repository conventions and constraints

- Keep `src/neo` dependency-free and Python 3.14-first.
- Preserve ASGI portability; native-server optimizations must not be required.
- Follow the existing facade pattern: `neo.*` selects `_native` unless `NEO_PURE=1`, then falls back to `neo._pure`.
- Pure Python defines observable behavior. C is an optimization, not a second design.
- Compile templates, response plans, SQL statements, binders, and cache structures at startup.
- Keep framework and server layers separable.
- Preserve PostgreSQL and ASGI semantics before optimizing implementation details.
- Measure each proposed C path against its pure twin before retaining it.
- Run native lints, sanitizer suites, parity tests, and request-boundary checks after C changes.
- Do not add suite-specific models such as `World` or `Fortune`, prescribed route names, or external query limits to framework APIs.

## Shared native/pure contract

Each new accelerated subsystem follows the same ownership model:

1. `src/neo/_pure/<feature>.py` is the executable behavioral specification.
2. `src/neo/_native/<feature>.c` implements only measured request-time work.
3. `src/neo/<feature>.py` is the public facade and backend selector.
4. Native and pure values, bytes, errors, limits, cancellation outcomes, and side effects match.
5. Deterministic parity tests run against both implementations.
6. Fuzz or generated tests cover malformed and boundary inputs where appropriate.
7. `NEO_PURE=1` remains a supported deployment and diagnostic mode.

Startup-time parsing and policy generally remain in Python. C owns measured encoding, rendering, lookup, wire parsing, decoding, or immutable response emission.

---

## 1. JSON serialization

### Public capability

Keep `neo.json.dumps()` and `JSONResponse` as the normal interfaces. Extend them only where real applications need functionality, such as tuples, dataclasses, or explicitly supported mapping and sequence types. Do not add a specialized fixed-document or “hello JSON” API.

### Pure Python

Keep `src/neo/_pure/json.py` as the behavioral specification and formalize:

- UTF-8 byte output.
- String-key enforcement.
- Rejection of non-finite floats.
- Exact string escaping and integer behavior.
- Recursion and output-size limits.
- Deterministic errors for unsupported values.

Add differential tests against expected wire bytes, not merely decoded equality.

### Native C

Extend `src/neo/_native/json.c` only for measured cases:

- Optimize the common small-dictionary and short-string path.
- Pre-size output using bounded geometric growth rather than repeated concatenation.
- Retain generic mapping and sequence semantics; do not recognize particular response shapes.
- Do not release the GIL while traversing Python objects.
- Keep exception types and messages identical to `_pure/json.py`.

### Acceptance checks

- Native and pure output are byte-identical across deterministic and fuzz corpora.
- `JSONResponse` emits correct content type and length on Neo and third-party ASGI servers.
- Native changes are retained only when repeated end-to-end measurements clear the A/A noise floor.

---

## 2. Reusable static and plaintext responses

### Public capability

Introduce a reusable immutable response form:

```python
response = PreparedResponse.text("service healthy")
```

`PreparedResponse` prebuilds status, headers, body, and ASGI messages once. It is useful for health checks, readiness endpoints, robots files, static API responses, and other immutable endpoints.

### Pure Python

Add `src/neo/_pure/response.py` with:

- `PreparedResponse`.
- Immutable prebuilt header tuples.
- Correct `Content-Type` and `Content-Length` behavior.
- HEAD and body-forbidden status handling.
- Normal ASGI `send()` behavior and backpressure.
- Safe concurrent reuse.

`src/neo/response.py` remains the public facade and retains the existing response classes.

### Native C

Add `src/neo/_native/response.c` to `_core`:

- Implement an immutable native `PreparedResponse` type.
- Store strong references to prebuilt ASGI message objects.
- Avoid rebuilding headers or formatting lengths per invocation.
- Remain independent of Neo server internals so the response works on every conforming ASGI server.

The native HTTP server may optimize ordinary fixed-body ASGI messages, but it must not require or recognize a Neo-specific response object.

### Acceptance checks

- One prepared response can be safely reused by concurrent requests.
- Pure and native implementations emit identical ASGI message sequences.
- Date and Server remain server-owned headers.
- HTTP/1.1 pipelining preserves response order.

---

## 3. Point database reads

### Public capability

Build on the existing startup-registered `Database.statement()` and `Statement.fetchrow()` APIs. Avoid a specialized point-read helper.

```python
get_widget = database.statement(
    "widget.get",
    'SELECT id, value FROM "widget" WHERE id = $1',
)
```

Add typed result shaping only when it benefits ordinary application code.

### Pure Python

In `src/neo/_pure/postgres.py`:

- Preserve prepared-plan caching.
- Certify cold and cached statement paths.
- Ensure cancellation either restores protocol synchronization or discards the connection.
- Avoid per-result imports and repeated column-name construction.
- Define exact behavior for zero, one, and unexpected multiple rows.

### Native C

Work within `src/neo/_native/postgres/`:

- Keep packet creation in `plan.c` and `protocol.c`.
- Keep binary scalar decoding in `codec.c`.
- Keep row assembly in `record.c` and `decode.c`.
- Add a measured one-row decode path that avoids constructing a temporary row list for `fetchrow()`.
- Preserve generic columns and codecs; do not recognize table or column names.

### Acceptance checks

- Cold and prepared queries return identical `Record` values in pure/native modes.
- Pool reuse remains safe after timeout, cancellation, and server errors.
- A registered statement is prepared during startup and does not repeat SQL introspection per request.

---

## 4. Independent multi-query and fan-out reads

### Public capability

Add a bounded ordered operation API rather than asking applications to create ad hoc tasks:

```python
rows = await connection.map(
    "fetchrow",
    statement,
    argument_sets,
    max_in_flight=32,
)
```

Expose the same operation through `Statement.map()` for recommendation lookups, dashboards, permission expansion, and batch point reads.

### Correctness contract

- Every input produces a distinct database operation.
- Preserve input order in returned results.
- Do not coalesce keys into `IN (...)`.
- Do not deduplicate duplicate inputs.
- Each PostgreSQL extended-protocol operation receives its own `Sync`.
- Bound queued operations and retained results.
- Cancellation reports completed, failed, and unsubmitted work clearly.

### Pure Python

- Implement scheduling and ordering in `_pure/postgres.py`.
- Reuse the existing `Operation` and bounded connection pipeline.
- Submit incrementally rather than materializing an unbounded iterable.
- Make transaction ownership explicit.

### Native C

Update:

```text
src/neo/_native/postgres/operation.c
src/neo/_native/postgres/protocol.c
src/neo/_native/postgres/decode.c
```

- Build individually `Sync`-delimited packets.
- Decode completed operations in batches while retaining per-operation errors.
- Use indexed or ring bookkeeping; do not front-delete lists or rescan completed operations.
- Keep Python callback execution outside parser state mutation.

### Acceptance checks

- Duplicate keys cause duplicate SQL executions.
- Packet tests observe one `Sync` per input operation.
- Results remain input-ordered when responses share a connection pipeline.
- Pipeline depth and memory remain bounded for generator inputs.

---

## 5. Transactional read-modify-write workloads

### Public capability

Provide a generic transactional operation group:

```python
async with connection.transaction() as tx:
    records = await tx.map("fetchrow", read_statement, keys)
    await tx.map("execute", update_statement, updates)
```

For ORM users, extend `Session.flush()` only where needed for bounded update batching. Do not add random-number or benchmark-specific mutation helpers.

### Pure Python

- Make explicit transaction support a documented public driver API.
- Add ordered `execute` mapping using separate operations.
- Ensure reads complete before dependent writes begin.
- Preserve duplicate-row occurrences unless the application explicitly deduplicates.
- Define partial failure, rollback, and cancellation behavior.
- Add a result structure for operation counts and failures where needed.

### Native C

- Reuse the operation pipeline rather than creating a special update protocol.
- Add an explicit transaction barrier between read and write groups.
- Keep every update as an independent extended-protocol operation.
- Ensure an error drains to the corresponding `ReadyForQuery` before pool reuse.
- Discard the connection when synchronization cannot be proven.

### Acceptance checks

- Instrumented PostgreSQL tests prove read-before-write ordering.
- One input occurrence produces one update operation.
- Application-generated values can be checked for uniqueness without framework intervention.
- Rollback leaves database state unchanged and the pool synchronized.
- ORM and raw-driver implementations produce equivalent final state.

---

## 6. Safe server-side templates

### Public capability

Add a small Neo-native template system rather than integrating a mandatory third-party runtime:

```python
templates = TemplateDirectory("templates")
fortune_table = templates.compile("table.html")

return HTMLResponse(fortune_table.render(rows=rows))
```

The initial language supports:

- Escaped variables: `{{ value }}`.
- Explicit trusted markup through a `Markup` type.
- `for` blocks.
- `if` blocks.
- Attribute and mapping lookup.
- Static includes resolved during startup compilation.

Do not permit arbitrary Python evaluation.

### Shared contracts

```text
neo.templates.Template
neo.templates.TemplateDirectory
neo.templates.Markup
neo.templates.TemplateSyntaxError
neo.templates.TemplateRenderError
```

Compile source into a stable opcode tape shared by both implementations.

### Pure Python

Add:

```text
src/neo/templates.py
src/neo/_pure/templates.py
```

The pure implementation owns:

- Parsing and syntax diagnostics.
- Opcode definitions.
- Safe lookup rules.
- HTML escaping.
- Iteration and conditional semantics.
- Source locations in render errors.

Escape at least `&`, `<`, `>`, `"`, and `'`. Plain strings are always untrusted.

### Native C

Add `src/neo/_native/templates.c`, registered through `_coremodule.c` and `setup.py`.

- Execute the compiled tape in C.
- Append UTF-8 fragments into a bounded geometric output buffer.
- Provide a native HTML-escaping primitive.
- Cache static UTF-8 fragments during compilation.
- Retain Python iteration and attribute-lookup semantics exactly.
- Reject output-size and recursion overflows cleanly.

Parsing remains startup-time Python unless measurement shows it matters. Rendering is the request-time C target.

### Acceptance checks

- Native and pure render byte-identical UTF-8.
- User data containing script tags, quotes, ampersands, apostrophes, and non-ASCII text is escaped.
- Sorting remains application-owned and is not hidden in the renderer.
- A database collection plus one request-created row can be sorted and rendered through the normal API.
- Templates compile once during application startup.

---

## 7. Application data caching

### Public capability

Keep `CacheControl` for HTTP caching and introduce a separate read-mostly application cache:

```python
cache = SnapshotCache[int, Widget]()

await cache.replace(load_widgets())
widget = cache.get(widget_id)
```

Neo's model is immutable snapshot publication rather than an implicit ORM cache:

- Readers see one complete generation.
- Refresh builds a new generation off to the side.
- Publication is atomic.
- Old snapshots remain alive while readers reference them.
- Capacity and refresh concurrency are bounded.

This supports configuration, reference data, feature catalogues, and database-backed read-mostly datasets.

### Pure Python

Add:

```text
src/neo/snapshot.py
src/neo/_pure/snapshot.py
```

Support:

- `get`, `require`, `get_many`, and immutable iteration.
- Atomic `replace`.
- Generation number and refresh timestamp.
- Optional maximum entries.
- Single-flight asynchronous refresh.
- Explicit misses; `get()` never performs hidden database I/O.

### Native C

Add `src/neo/_native/snapshot.c`:

- Implement a native immutable snapshot backed by a private dictionary.
- Accelerate `get` and `get_many` only when measurement supports it.
- Hold strong ownership of keys and values.
- Publish generations atomically under the GIL and test free-threaded Python separately.
- Perform no eviction work on the read path.
- Never expose borrowed references.

Refresh callbacks and lifecycle policy remain in Python. C owns only measured storage and lookup work.

### Acceptance checks

- Readers observe either the old or new generation, never a partial refresh.
- Refresh failure preserves the previous generation.
- Duplicate requested keys produce duplicate output positions.
- Pure/native lookup and miss behavior match.
- Capacity violations fail before publishing the new generation.

---

## 8. Query-bound input policies

### Public capability

Extend the existing `Query` marker for reusable pagination, batch, and fan-out limits:

```python
queries: Annotated[
    int,
    Query(minimum=1, maximum=500, overflow="clamp"),
] = 1
```

Supported overflow policies are `error` and `clamp`.

### Pure and native behavior

- Python binder compilation produces a scalar constraint plan at startup.
- Pure conversion remains the reference behavior.
- Extend `validate.c` only if request-trace and decomposition measurements show query conversion is material.
- Missing/default handling occurs before clamping.
- Invalid integer syntax remains an error; clamping applies only to valid out-of-range values.

### Acceptance checks

- Missing input uses the handler default.
- A valid low value clamps to the minimum when requested.
- Very large values clamp without integer overflow.
- Invalid syntax returns the existing structured validation error.
- Pure/native errors and converted values match.

---

## 9. Server and protocol certification

Most required protocol mechanics already exist. Certify and harden them rather than adding a benchmark or route mode.

Cover both `src/neo/_pure/server.py` and:

```text
src/neo/_native/server_http1.c
src/neo/_native/server_common.c
```

Verify:

- Date and Server headers.
- Exact body framing.
- HTTP/1.1 keep-alive.
- Sequential response ordering for pipelined requests.
- No implicit compression.
- HEAD and body-forbidden statuses.
- Backpressure and disconnect behavior.
- Large pipelined backlogs without quadratic rescanning.
- Identical malformed-input behavior.

Do not add route-aware response shortcuts to the server. It remains an ASGI server and must not recognize framework handlers.

---

## Generic workload and conformance harness

Add a neutral workload suite:

```text
benchmarks/workloads/
    app.py
    models.py
    schema.sql
    verify.py
    bench.py
```

Use ordinary domain names such as `Widget` and `Quotation`, with scenarios for:

- Small JSON serialization.
- Prepared static response.
- Point database read.
- Independent fan-out reads.
- Transactional read-modify-write.
- Escaped template table rendering.
- Snapshot-cache reads.

The verifier asserts semantic properties rather than benchmark identity:

- Number of database operations.
- Separate PostgreSQL `Sync` messages.
- Read-before-write ordering.
- Unique application-generated update values.
- Template escaping and UTF-8 output.
- Header and framing correctness.
- Query-limit behavior.
- Pipelined response ordering.

A future external benchmark adapter can map these primitives to prescribed route and table names without changing `src/neo`.

## Correctness rules

- Pure Python defines observable behavior; C is an optimization, not a second design.
- Native and pure values, bytes, errors, and cancellation outcomes match.
- Multi-query operations are never silently coalesced or deduplicated.
- Database connections return to the pool only when synchronization is proven.
- Templates escape by default; trusted markup requires an explicit type.
- Cache reads perform no hidden I/O.
- Static responses remain valid on every conforming ASGI server.
- No third-party benchmark's tables, routes, constants, or randomization enter framework code.
- No C optimization is accepted without repeated end-to-end evidence above the measured noise floor.
- Any growth in the request-boundary baseline is documented as an explicit trade-off.
- Intentional native-lint findings receive narrow in-place waivers with boundedness rationale.

## Likely files touched

```text
setup.py
src/neo/__init__.py
src/neo/binding.py
src/neo/response.py
src/neo/postgres.py
src/neo/templates.py
src/neo/snapshot.py

src/neo/_pure/response.py
src/neo/_pure/postgres.py
src/neo/_pure/templates.py
src/neo/_pure/snapshot.py

src/neo/_native/_coremodule.c
src/neo/_native/neocore.h
src/neo/_native/json.c
src/neo/_native/response.c
src/neo/_native/templates.c
src/neo/_native/snapshot.c
src/neo/_native/validate.c
src/neo/_native/postgres/operation.c
src/neo/_native/postgres/protocol.c
src/neo/_native/postgres/decode.c

tests/test_json_parity.py
tests/test_response.py
tests/test_template_parity.py
tests/test_snapshot_parity.py
tests/test_binding.py
tests/test_server_protocol.py
tests/postgres/test_pipeline.py
tests/postgres/test_connection.py

benchmarks/workloads/
benchmarks/README.md
docs/guides/templates.md
docs/guides/caching.md
docs/reference/responses.md
docs/reference/postgres.md
docs/agents/manifest.json
```

## Verification commands

Run focused tests while implementing each subsystem, then the repository gates:

```bash
uv run pytest tests/test_response.py tests/test_binding.py
uv run pytest tests/test_template_parity.py tests/test_snapshot_parity.py
uv run pytest tests/postgres/test_pipeline.py tests/postgres/test_connection.py
uv run pytest tests/test_server_protocol.py
uv run neo-native-lint
uv run neo-request-trace --check
uv run neo-check --docs
```

Run the relevant sanitizer build and tests after changing each native extension. Record benchmark environment, warmup, repetitions, errors, throughput, median, p95, and p99. Preserve every trial and the A/A noise-floor measurement.

## Completion gate

The work is complete when the neutral workload application can express all seven workload shapes through public Neo APIs, passes in native and `NEO_PURE=1` modes, passes the PostgreSQL wire-semantic verifier, runs behind Neo and a third-party ASGI server, and requires only a thin external naming and deployment adapter for formal third-party conformance submission.
