# Prescriptive plan: adversarial resource, filesystem, and hot-path remediation

Status: ready for test-first implementation

Related material:

- `AGENTS.md`
- `repo-map.md`
- `docs/agents/manifest.json`
- `docs/plans/profile-guided-hotspot-remediation.md`
- `docs/plans/native-buffered-protocol-ingress.md`
- `docs/plans/native-server-sanitizers.md`

## Goal

Close the eight findings from the aggressive framework red-team pass: bound PostgreSQL prepared plans, bound URL-encoded field cardinality, make pure HTTP/1 head parsing incremental, make static/template filesystem containment race-safe, remove avoidable file-serving executor churn, remove pure-server front-deleted receive queues, and bound multipart parsed-memory amplification.

This is a test-first plan. **For every work item, the named red test must be added and observed failing for the stated reason before production code changes begin.** Do not weaken a test merely because the preferred implementation changes. Preserve ASGI behavior, native/pure parity where applicable, cancellation semantics, and the dependency-free framework core.

Performance claims require repeated measurements that clear the A/A noise floor. Correctness and complexity tests should use deterministic counters, bounds, retained-object checks, or adversarial state transitions rather than wall-clock assertions.

## Current check baseline

The review baseline was clean under `uv run wreath-check`: Ruff, ty, pytest, native complexity/memory/error/GIL lints, and the request-boundary baseline all passed. That does not cover the adversarial cases below.

Before implementation, retain a fresh baseline:

```bash
uv run wreath-check
uv run pytest -m '' -n 4
```

For C changes, also retain the applicable sanitizer baseline described in `docs/plans/native-server-sanitizers.md`.

## Non-negotiable red/green workflow

For each numbered item:

1. Add only the red test and any test-local fixture/counter it needs.
2. Run the narrow command listed for that item.
3. Record the failing assertion and confirm it demonstrates the intended defect, not a typo, missing fixture, unsupported symlink, or unrelated exception.
4. Commit/checkpoint the red state if the harness supports checkpoints.
5. Implement the smallest complete fix.
6. Re-run the narrow test, then relevant parity tests.
7. Run `uv run wreath-check`; run strict docs and sanitizers when the touched paths require them.
8. Record before/after benchmark artifacts for performance work. A passing timing test is not benchmark evidence.

Never implement several fixes and then add retrospective tests. Work in the order below so shared request/file APIs settle before later optimizations.

## Ranked work matrix

| Order | Finding | Primary risk | Red test file | Likely production files |
| --- | --- | --- | --- | --- |
| 1 | Unbounded PostgreSQL prepared plans | process/backend memory DoS | `tests/postgres/test_connection.py`, `tests/postgres/test_pool.py` | `src/wreath/_pure/postgres.py`, `src/wreath/postgres.py`, native PostgreSQL glue if needed |
| 2 | Unbounded URL-encoded field cardinality | CPU/memory DoS | `tests/test_request.py`, `tests/test_native_parity.py` | `src/wreath/request.py`, `src/wreath/_pure/codecs.py`, `src/wreath/_native/codecs.c` |
| 3 | Pure HTTP/1 incremental-head quadratic | CPU/slowloris amplification | `tests/test_server_protocol.py` | `src/wreath/_pure/server.py` |
| 4 | Static-file check/open race | local file disclosure | `tests/test_framework_features.py` | `src/wreath/staticfiles.py`, `src/wreath/response.py`, optional native server extension |
| 5 | File-response executor churn | throughput/thread-pool contention | `tests/test_response.py`, `tests/test_framework_features.py` | `src/wreath/response.py`, `src/wreath/staticfiles.py`, server sendfile path |
| 6 | Receive queue `pop(0)` | quadratic fragmented-input work | `tests/test_server_protocol.py` | `src/wreath/_pure/server.py` |
| 7 | Multipart retained-memory amplification | concurrent memory pressure | `tests/test_request.py`, `tests/test_native_parity.py` | `src/wreath/request.py`, multipart twins if parsing contract changes |
| 8 | Template-directory symlink escape | conditional file disclosure | `tests/test_template_parity.py` | `src/wreath/templates.py`, optional filesystem helper shared with static files |

Line references below describe the reviewed tree and are approximate after edits.

---

## 1. Bound PostgreSQL prepared plans per connection

### Defect and current lines

- `src/wreath/_pure/postgres.py:743-769`: `Connection.__slots__` has `_plans` but no limit or eviction state.
- `src/wreath/_pure/postgres.py:771-804`: each connection creates an unbounded `dict[str, Plan]`.
- `src/wreath/_pure/postgres.py:954-970`: every distinct SQL string becomes a cold prepared statement.
- `src/wreath/_pure/postgres.py:1211-1223`: every successful cold operation is retained permanently.
- `src/wreath/postgres.py:46-62`: `PoolConfig` has no prepared-plan bound.
- `src/wreath/postgres.py:114-125`: pool-created connections are not configured with such a bound.

The ORM registry LRU at `src/wreath/orm/registry.py:380-400` is already bounded and is not a substitute for this connection-level cache.

### RED first

Add `test_connection_plan_cache_evicts_oldest_shape` in `tests/postgres/test_connection.py` using the existing `FakePostgres` fixture:

1. Open a connection configured with `statement_cache_size=2`.
2. Successfully execute/fetch three distinct parameterized SQL texts so all three receive plans.
3. Assert the connection exposes a supported diagnostic plan count of `2` (prefer a read-only property; do not make the test depend on native object layout).
4. Execute the oldest SQL again and assert the wire flight is cold/Parse-bearing, while the two retained shapes use cached execution.
5. Assert the evicted server-side prepared statement is closed/deallocated no later than the next protocol synchronization point.

Current red reason: there is no size configuration and the plan count becomes three; no Close/DEALLOCATE is emitted.

Add `test_pool_applies_statement_cache_size_to_every_connection` in `tests/postgres/test_pool.py`: configure a pool size greater than one, churn distinct SQL on each borrowed connection, and assert every connection independently stays at the configured bound.

Narrow red command:

```bash
uv run pytest tests/postgres/test_connection.py tests/postgres/test_pool.py -q
```

### Implementation

- Add `statement_cache_size` to `PoolConfig`; choose and document a finite default. Permit `0` only if it clearly means “do not retain automatic plans.”
- Thread the setting through `Pool._open`, the connector, and pure/native connection initialization without hidden globals.
- Replace `_plans` with access-ordered bounded storage. Cache hits must refresh recency.
- On eviction, retire the PostgreSQL prepared statement with a protocol-level Close for statement names, not interpolated SQL. Integrate closure with pipeline ordering and synchronization; do not inject an unordered `DEALLOCATE` query into an active pipeline.
- Never evict a plan still referenced by an emitted/current operation. Defer retirement until safe if necessary.
- Named statements explicitly registered by application startup need an explicit policy: pin them or keep them outside the automatic LRU.
- Expose bounded diagnostics (`prepared_plan_count`, evictions) without exposing mutable cache internals.

### Green and acceptance

- Cache size never exceeds the configured bound after completion publication.
- Eviction does not alter result decoding, cancellation, transaction barriers, pipeline order, or connection failure handling.
- Pure and native backends have identical observable cache policy.
- Add a varied-shape benchmark recording process RSS, backend prepared-statement count, cold/hit/eviction counts, and throughput before/after. Fixed-shape throughput must not regress beyond measured noise.

---

## 2. Bound URL-encoded field cardinality before allocation explodes

### Defect and current lines

- `src/wreath/request.py:18-50`: `RequestLimits` lacks URL-encoded field/key/value bounds.
- `src/wreath/request.py:368-371`: form parsing consumes the complete list from `parse_qs`.
- `src/wreath/_pure/codecs.py:31-44`: `query.split(b"&")` allocates all fragments, then allocates every tuple/string.
- `src/wreath/_native/codecs.c`: native parsing must enforce the same boundary and errors.

### RED first

Add `test_urlencoded_form_rejects_too_many_fields_before_decoding_all` in `tests/test_request.py`:

1. Construct `RequestLimits(max_form_fields=3, ...)`.
2. Feed `b"a=1&b=2&c=3&d=4"` as one body and set URL-encoded content type.
3. Assert `await request.form()` raises the selected client-facing limit exception with a stable message identifying the field-count limit.
4. Use a test-local decoding spy/counter around the pure codec and assert the fourth field triggers rejection without decoding later fields.

Current red reason: all four fields parse successfully and no limit exists.

Add pure/native parity vectors in `tests/test_native_parity.py` for exactly-at-limit, one-over-limit, empty fragments, missing `=`, duplicate keys, invalid UTF-8, and a trailing separator.

Add `test_parse_qs_does_not_split_the_complete_input` using a bytes-like/test hook or codec operation counter that proves bounded single-pass scanning. Do not assert elapsed time.

Narrow red command:

```bash
uv run pytest tests/test_request.py tests/test_native_parity.py -q
```

### Implementation

- Add finite `max_form_fields`, `max_form_key_bytes`, and `max_form_value_bytes` to `RequestLimits`; validate them in `__post_init__`.
- Extend the internal codec contract to accept limits and reject while scanning, before decoded Python objects are retained.
- Replace whole-input `split` with delimiter scanning in the pure parser.
- Mirror limits and exact exception behavior in `src/wreath/_native/codecs.c`.
- Avoid a complete intermediate list in `Request.form()`: provide an iterator/callback or parse directly into the destination mapping. Preserve first-value-wins behavior.
- If a public query-parameter mapping is later added, it must use a separate finite `max_query_fields`; do not silently reuse an unbounded codec call.

### Green and acceptance

Peak object count is O(configured field limit), not O(attacker body separators). Pure/native bytes and exceptions match. Existing request-body and multipart limits continue to apply independently.

---

## 3. Make pure HTTP/1 incomplete-head work linear

### Defect and current lines

- `src/wreath/_pure/server.py:174-178`: every ingress fragment immediately drives parsing.
- `src/wreath/_pure/server.py:251-267`: every incomplete attempt copies all pending bytes and rescans delimiters from offset zero.
- Native `src/wreath/_native/server_http1.c` already maintains delimiter scan cursors; retain that behavior.

### RED first

Add pure-only `test_incomplete_head_scans_each_byte_a_bounded_number_of_times` in `tests/test_server_protocol.py`:

1. Subclass `PureHttpProtocol` only for instrumentation.
2. Instrument pending-range requests or a new test-local byte-scan helper so the test counts bytes offered to delimiter searches; do not time the event loop.
3. Feed a legal near-`max_header_bytes` request head one byte per `data_received` call.
4. Assert total examined bytes are at most a small linear multiple of input length and the response remains 200.
5. Repeat with `\r\n\r\n` split across every boundary and with an over-limit head that returns 431.

Current red reason: cumulative copied/scanned bytes grow approximately `N*(N+1)/2`.

Narrow red command:

```bash
uv run pytest tests/test_server_protocol.py -q -k 'incomplete_head or split'
```

### Implementation

- Add request-line and head-terminator scan offsets to the pure protocol’s per-request state.
- Search only newly arrived bytes plus the three-byte delimiter overlap.
- Avoid `bytes(self._pending())` for each incomplete head; operate on the existing buffer without retaining a resizing-blocking view across `_consume`.
- Reset scan offsets whenever consumption/reset invalidates them.
- Preserve 414/431 precedence, header-count behavior, pipelining, request timeout, and malformed-head parity.

### Green and acceptance

The operation-count test is linear for one-byte ingress. Run all split-point/fuzz parity tests. Add an ablation benchmark comparing whole-head, 16-byte, and one-byte ingress at 8 KiB and 32 KiB; report CPU/request and throughput with repeated trials.

---

## 4. Make static containment and open one race-safe operation

### Defect and current lines

- `src/wreath/staticfiles.py:40-68`: `realpath`, containment, and stat happen before response construction.
- `src/wreath/response.py:330-353`: `FileResponse` later stats and opens the pathname again.
- The checked path and opened filesystem object can differ after rename/symlink replacement.

### RED first

Add `test_static_symlink_swap_cannot_escape_root` in `tests/test_framework_features.py`:

1. Create `public/item.txt` and an outside `secret.txt`.
2. Use an event/barrier around the existing validated-stat/open boundary to deterministically replace `item.txt` with a symlink to `secret.txt` after containment validation but before open.
3. Request `/assets/item.txt`.
4. Assert the response is a safe failure (404 is preferred), never contains the secret, and never reports the secret’s content length.
5. Skip only when the platform genuinely lacks symlink support; a missing test hook is not a skip reason.

Current red reason: the response can contain `secret.txt`.

Also add directory-component swaps and an `index.html` swap case. Keep the existing `../` traversal test.

Narrow red command:

```bash
uv run pytest tests/test_framework_features.py -q -k static
```

### Implementation

- Introduce a small internal filesystem primitive that opens relative to a trusted root directory descriptor and returns the already-open file plus its `fstat` metadata.
- On Linux prefer `openat2` with `RESOLVE_BENEATH`, `RESOLVE_NO_MAGICLINKS`, and appropriate no-follow semantics. Provide a safe component-wise `openat`/`O_NOFOLLOW` fallback where supported.
- Never validate one pathname and later reopen it by string.
- `StaticFiles` should pass an opened file/owned descriptor and metadata to the response path. Ownership and close-on-cancellation must be explicit.
- Directory index lookup must remain beneath the same root descriptor.
- Unsupported platforms must fail closed or use a documented safe fallback, not silently restore the race.

### Green and acceptance

All deterministic swap tests fail closed. Normal files, directories, conditional ETags, missing files, cancellation, and file shrink/growth framing remain correct. Add platform-specific tests where descriptor APIs differ.

---

## 5. Collapse file-serving executor crossings

### Defect and current lines

- `src/wreath/staticfiles.py:51-58`: separate executor jobs for resolution/stat/index stat.
- `src/wreath/response.py:331-353`: separate jobs for stat, open, every 256 KiB read, and close.

### RED first

Add `test_file_response_uses_bounded_executor_submissions` in `tests/test_response.py`:

1. Monkeypatch `asyncio.to_thread` with a transparent counting wrapper.
2. Send a multi-megabyte `FileResponse` through a collecting ASGI `send`.
3. Assert the body is exact and executor submission count is constant with file size (target no more than two in the portable fallback).
4. Run the same assertion at 256 KiB and several MiB so the current per-chunk pattern is exposed.

Current red reason: submissions increase by one per 256 KiB chunk, plus stat/open/close.

Add `test_static_response_does_not_repeat_stat_or_open` in `tests/test_framework_features.py` using operation counters: one secure open and one `fstat`-derived metadata snapshot should serve a normal file.

Narrow red command:

```bash
uv run pytest tests/test_response.py tests/test_framework_features.py -q -k 'file or static'
```

### Implementation

- Prefer a native server/file-response sendfile extension when the transport and TLS mode support it.
- Keep a portable ASGI fallback. Perform blocking open/read/close ownership in one worker operation or use an explicitly owned file-stream worker; do not submit one executor job per chunk.
- Integrate with the already-open descriptor from item 4 and derive length from `fstat` once.
- Preserve backpressure: a worker must not read the entire file ahead of ASGI `send`, and buffering must remain bounded.
- Ensure disconnect/cancellation stops reads and closes the descriptor promptly.

### Green and acceptance

Executor submissions are O(1), buffered bytes are bounded, and content-length/framing remain correct if a file shrinks. Benchmark 256 KiB, 10 MiB, and 1 GiB files over plain HTTP and TLS, recording throughput, p50/p95/p99, CPU, RSS, executor queue depth, and errors.

---

## 6. Remove pure-server receive-queue front deletion

### Defect and current lines

- `src/wreath/_pure/server.py:120-150`: receive queue state is list-backed.
- `src/wreath/_pure/server.py:866-919`: enqueue/dequeue plumbing.
- `src/wreath/_pure/server.py:892-895`: `pop(0)` shifts every remaining reference.

### RED first

Add pure-only `test_receive_queue_drain_has_linear_reference_movement` in `tests/test_server_protocol.py`:

1. Fill the receive queue to `read_high_water_messages` with tiny body messages.
2. Use a test-local counting list whose `pop(0)` records the number of shifted references, or instrument the queue abstraction.
3. Drain it through `_receive()`.
4. Assert shifted/moved references are O(N), ideally zero after enqueue.
5. Assert pause/resume watermarks and `_queued_bytes` remain correct.

Current red reason: shift work is `N*(N-1)/2`.

Narrow red command:

```bash
uv run pytest tests/test_server_protocol.py -q -k receive_queue
```

### Implementation

Use `collections.deque`/`popleft()` or a cursor-backed queue with periodic compaction. Preserve disconnect insertion order, waiter wakeups, byte/message watermarks, and reset behavior. Do not loosen `read_high_water_messages`.

### Green and acceptance

Operation-count test is linear; HTTP body and WebSocket fragmentation parity passes. Benchmark queue depths 1, 64, and 1024 without using timing as the correctness gate.

---

## 7. Bound multipart parsed-memory amplification

### Defect and current lines

- `src/wreath/request.py:300-330`: complete body is buffered and cached.
- `src/wreath/_pure/multipart.py:81-83` and native twin: part payloads are materialized in addition to the body.
- `src/wreath/request.py:348-367`: all parsed fields/files are retained; aggregate retained parsed bytes have no independent cap.

### RED first

Add `test_multipart_rejects_aggregate_retained_payload_limit` in `tests/test_request.py`:

1. Configure each part below `max_part_bytes` and part count below `max_parts`.
2. Set a new `max_form_memory_bytes` lower than the aggregate payload of two or more parts.
3. Assert parsing rejects exactly when aggregate retained payload would exceed the cap, before retaining the over-limit part.
4. Cover mixed text/file parts and native/pure parity.

Current red reason: all parts are retained because only per-part and body limits exist.

Add `test_multipart_file_spools_after_memory_threshold` if the selected API supports spooling: verify a large upload is file-backed, cleanup occurs on cancellation/error, and small uploads retain current bytes behavior. Do not break the existing public assertion that small `UploadedFile.data` is `bytes` without a separately approved API change.

Narrow red command:

```bash
uv run pytest tests/test_request.py tests/test_native_parity.py -q -k multipart
```

### Implementation

- Add and validate `max_form_memory_bytes`; account for payload retained by parsed fields/files, not merely wire bytes.
- Reject before retaining the part that crosses the cap.
- Prefer streaming multipart parsing and configurable file spooling for a full fix. Keep small-file behavior compatible.
- If parser output uses views internally, make ownership/lifetime explicit and convert only at the public boundary.
- Release parser intermediates promptly. Do not clear `Request._body` if that would violate the documented repeatable `await request.body()` contract.

### Green and acceptance

Aggregate retained payload is bounded independently of body size and per-part limits. Measure peak RSS/tracemalloc for concurrent 16 MiB forms, but use deterministic accounting for the test gate.

---

## 8. Prevent TemplateDirectory symlink escape

### Defect and current lines

- `src/wreath/templates.py:81-95`: containment uses `normpath` prefix checks, then ordinary `open`, which follows symlinks.
- Includes call the same `_read`, so a symlinked include can also escape.

### RED first

Add `test_directory_rejects_symlink_escape` and `test_include_rejects_symlink_escape` in `tests/test_template_parity.py`:

1. Place a secret file outside the template root.
2. Create a symlink inside the root targeting the secret.
3. Assert direct `compile("link.html")` raises `TemplateSyntaxError` and does not expose secret text.
4. Create a normal parent template that includes the symlink and assert the same.

Current red reason: compilation reads and compiles the outside file.

Narrow red command:

```bash
uv run pytest tests/test_template_parity.py -q -k 'directory or include'
```

### Implementation

Reuse the descriptor-based beneath-root helper from item 4 in a read-only compile-time mode. Read from the securely opened descriptor, not a reconstituted pathname. Preserve include-cycle detection and useful template names in diagnostics. If trusted symlinks are intentionally supported later, that must be a separate explicit policy with a resolved-root allow-list; default behavior stays contained.

### Green and acceptance

Direct and included symlink escapes fail closed while normal nested templates compile. Add platform-appropriate safe fallback coverage.

---

## Cross-cutting verification

After each item, run its narrow tests. At the end run:

```bash
uv run wreath-check
uv run pytest -m '' -n 4
uv run --group docs mkdocs build --strict
```

When C files change, run the matching ASan/UBSan build and suites. At minimum this includes native codec parity for item 2 and any native sendfile/server work for item 5.

Re-run:

```bash
uv run wreath-native-lint
uv run wreath-request-trace --check
```

No baseline update is allowed merely to make a gate pass. If a request-boundary crossing is intentionally added, measure it, update the baseline with an explicit rationale, and record the trade-off.

## Documentation updates required with implementation

- Request limits and rejection behavior in the request/body/form guides and API reference.
- PostgreSQL pool/connection cache configuration, diagnostics, eviction, and prepared-statement lifecycle.
- Static-file root trust, platform support, symlink policy, and file-response behavior.
- Template-directory symlink policy.
- Performance internals for pure HTTP/1 incremental scanning and file-send paths.
- `docs/agents/manifest.json` focused-test mappings if new test files are introduced.

## Completion criteria

- Every named red test was observed failing before its production fix and the failure was recorded in implementation notes/checkpoints.
- All eight tests pass without timing-based assertions.
- PostgreSQL plans, URL-encoded fields, receive queues, multipart retained bytes, and file-stream buffering have explicit finite bounds.
- Pure HTTP/1 incomplete-head work and receive-queue drain work are linear by deterministic operation counts.
- Static and template filesystem access cannot escape roots through traversal, symlinks, or validation/open races.
- File-serving executor submissions are constant with file size, with backpressure and cancellation preserved.
- Pure/native parity, full checks, strict docs, and applicable sanitizers pass.
- Performance reports include repeated raw trials and A/A noise; no win is claimed from a single run or below-noise delta.
