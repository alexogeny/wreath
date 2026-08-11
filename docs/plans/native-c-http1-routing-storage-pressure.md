# Prescriptive plan: HTTP/1, routing, and native storage pressure

Status: ready for test-first implementation

Related material:

- `AGENTS.md`
- `docs/plans/native-c-hotspots.md`
- `docs/plans/native-buffered-protocol-ingress.md`
- `docs/plans/native-server-sanitizers.md`
- `docs/plans/native-c-orm.md`
- `benchmarks/README.md`

## Goal

Remove the newly identified CPU-amplification and memory-pressure paths outside HTTP/2 and HTTP/3: incremental HTTP/1 parsing under slow input, unbounded WebSocket fragment/message counts, quadratic HTTP/1 queue draining, trie backtracking, PostgreSQL slab/tape rescans, repeated PostgreSQL `bytea` Python dispatch, multipart peak copies, hidden JSON key-cache retention, and repeated request cookie parsing. Preserve protocol, routing, ASGI, ORM, and public request semantics while producing reproducible before/after timing and peak-RSS evidence.

This is deliberately separate from `docs/plans/native-c-hotspots.md`. Another agent may implement that plan concurrently.

## Parallel-work boundary

Do not edit the following files until the `native-c-hotspots` implementation has merged, because that plan also changes them:

```text
src/neo/_native/postgres/protocol.c
src/neo/_native/dtrouter.c
src/neo/_native/neocore.h
benchmarks/README.md
```

Parallel-safe work in this plan starts with:

```text
src/neo/_native/server.h
src/neo/_native/server_common.c
src/neo/_native/server_http1.c
src/neo/_native/router.c
src/neo/_native/postgres/tape.c
src/neo/_native/postgres/tape.h
src/neo/_native/postgres/decode.c
src/neo/_native/postgres/codec.c
src/neo/_native/json.c
src/neo/_native/multipart.c
src/neo/request.py
```

After the other plan merges, rebase first, inspect its queue helpers and benchmark conventions, and then implement the retired-slab slice in `postgres/protocol.c`. Do not reimplement or replace its `messages_head` control-message queue work. Decision-router hot-path allocation is measurement-only in this plan; do not edit `dtrouter.c` here.

## Repository constraints

- Target CPython 3.14 and keep `src/neo` free of mandatory third-party dependencies.
- Preserve ASGI semantics and the pure/native parity contract.
- Keep request, stream, route-table, and connection state explicitly owned; add no mutable process-global cache.
- Treat malformed and slow input as adversarial. Limits must constrain object count and CPU work as well as payload bytes.
- Do not optimize from one run. Store warmups, every measured trial, environment metadata, median, p95, errors, and peak RSS.
- Add focused regression tests before each behavior change and call `update_feature_tdd` at red, green, refactored, and done.
- Run ASan/UBSan for modified native server code and PostgreSQL buffer/tape code.
- Do not change HTTP/2, HTTP/3, HPACK, QPACK, or QUIC code in this plan.

## Baseline and measurement harness

Create `benchmarks/bench_native_http1_storage.py` before modifying production code. The public driver must spawn a fresh child process for each measured trial so peak RSS and retained objects from one scenario cannot contaminate another.

Required command:

```bash
uv run python -m benchmarks.bench_native_http1_storage \
  --scenario all --warmup 2 --trials 9 \
  --output benchmark-results-native-http1-storage/before.json
```

Supported scenarios:

```text
http1-slow-head
http1-slow-chunk-line
http1-receive-queue
ws-empty-fragments
ws-empty-messages
trie-adversarial-miss
trie-wide-fanout
pg-tape-small-consume
pg-retired-slabs
pg-bytea-text
multipart-peak
json-key-churn
request-cookie-repeat
```

Each scenario result must contain:

```json
{
  "scenario": "http1-slow-head",
  "parameters": {},
  "python": "full sys.version",
  "platform": "platform.platform()",
  "executable": "sys.executable",
  "native_module": "resolved extension path",
  "compiler_flags": "available build metadata or unavailable",
  "warmups": 2,
  "trials": 9,
  "raw_seconds": [],
  "median_seconds": 0.0,
  "p95_seconds": 0.0,
  "raw_peak_rss_bytes": [],
  "median_peak_rss_bytes": 0,
  "errors": []
}
```

Use `time.perf_counter_ns()` for timing and fresh subprocesses for each RSS sample. Normalize `resource.getrusage(RUSAGE_SELF).ru_maxrss` for Linux KiB versus macOS bytes and record the normalization. Record `unavailable` rather than fabricating values on unsupported platforms or when the optional PostgreSQL/native component is absent.

The baseline must include paired sizes so acceptance checks use scaling rather than machine-specific absolute times:

- slow headers and chunk lines: 8 KiB and 16 KiB, delivered one byte per `buffer_updated()`/compatibility feed;
- queue drain: 10,000 and 20,000 entries;
- fragmented WebSocket input: 10,000 and 20,000 empty continuation frames;
- trie miss: ambiguity depths 10 and 14;
- trie fanout: 1,000 and 2,000 root literal children;
- tape consumption: 10,000 and 20,000 rows consumed one row at a time;
- retired slabs: 128 and 256 pinned slabs followed by 1,000 receive cycles;
- text `bytea`: 10,000 and 20,000 fields of a fixed payload size;
- multipart: 8 MiB and 16 MiB bodies;
- JSON key churn: 1,024 distinct short keys over repeated documents;
- cookie parsing: 10,000 and 20,000 property reads on one request.

Save the untouched baseline as `benchmark-results-native-http1-storage/before.json`. After implementation, write `after.json`; never overwrite the baseline.

## Implementation tasks

### 1. Make HTTP/1 delimiter scanning incremental

Files:

```text
src/neo/_native/server.h
src/neo/_native/server_common.c
src/neo/_native/server_http1.c
tests/test_server_protocol.py
tests/test_server_fuzz.py
```

The current `drive_head`, chunk-size, and trailer states call `find_sub()` from offset zero whenever more bytes arrive. Replace repeated full-prefix scans with explicit state-relative cursors.

Add these fields to `NeoHttpProtocol`:

```c
Py_ssize_t head_terminator_scan;
Py_ssize_t request_line_scan;
Py_ssize_t chunk_line_scan;
Py_ssize_t trailer_terminator_scan;
```

Offsets are relative to `self->cursor`, not absolute addresses. They must remain valid if the backing allocation moves. Reset the relevant cursor to zero whenever entering its parser state, completing a request, rejecting input, upgrading to WebSocket, or clearing the protocol.

Add a helper in `server_common.c`:

```c
Py_ssize_t find_sub_from(
    const char *hay,
    Py_ssize_t hay_len,
    const char *needle,
    Py_ssize_t needle_len,
    Py_ssize_t *scan_from
);
```

Required behavior:

- Search starts at `*scan_from`, clamped to `[0, hay_len]`.
- On a match, return its offset without advancing beyond it.
- On an incomplete miss, set `*scan_from` to `max(0, hay_len - (needle_len - 1))`, preserving the only suffix that can begin a future cross-buffer match.
- Handle empty needles and overflow defensively, although production callers use two- and four-byte delimiters.
- Never retain a pointer into `self->buf` across callbacks.

Use it for:

- `\r\n\r\n` header termination in `drive_head`;
- the first `\r\n` request-line check in `drive_head`;
- chunk-size line termination;
- trailer line/block termination.

Once a complete head is found, keep `neo_http_parse_request_parts()` unchanged in this slice. The optimization prevents repeated discovery scans; it does not alter parsing or limits.

Tests must feed every delimiter split, including splits inside `\r\n\r\n`, and prove identical status/errors for malformed input. Add a one-byte-feed regression test that uses a test-only scan counter or benchmark scaling; do not assert wall-clock time in pytest.

### 2. Bound and compact the HTTP/1/WebSocket receive queue

Files:

```text
src/neo/server.py
src/neo/_native/server.h
src/neo/_native/server_http1.c
tests/test_server_protocol.py
tests/test_server_websocket.py
docs/reference/server.md or the existing server configuration reference
```

Add one public server setting beside `read_high_water`:

```python
read_high_water_messages: int = 1024
```

Validate it as positive in `ServerConfig.__post_init__`. This setting bounds queued ASGI request/WebSocket messages whose payload length may be zero; it does not replace the byte watermark.

Add to `NeoHttpProtocol`:

```c
Py_ssize_t receive_head;
Py_ssize_t queued_messages;
Py_ssize_t read_high_water_messages;
```

Queue rules:

- append at the list tail;
- dequeue from `receive_head` and increment it;
- logical length is `PyList_GET_SIZE(receive_queue) - receive_head`;
- reset the list and head when logical length reaches zero;
- otherwise compact only when `receive_head >= 64` and `receive_head * 2 >= PyList_GET_SIZE(receive_queue)`;
- compact after taking an owned reference to the dequeued item;
- update `queued_messages` exactly once per enqueue/dequeue;
- pause reading when either `queued_bytes >= read_high_water` or `queued_messages >= read_high_water_messages`;
- resume only when both values are at or below half their respective high-water marks;
- disconnect events remain deliverable and cannot be dropped because a watermark was reached.

Initialize/reset/traverse state through existing protocol lifecycle paths. Do not create a separate queue type or global registry.

Tests must prove FIFO ordering across compaction, byte-watermark behavior remains unchanged, empty WebSocket messages trigger pause by count, resume requires both low-water conditions, and repeated connection reset does not retain consumed messages.

### 3. Replace the WebSocket fragment list with one bounded accumulator

Files:

```text
src/neo/_native/server.h
src/neo/_native/server_http1.c
tests/test_server_websocket.py
```

Replace:

```c
PyObject *ws_frag_parts;
```

with:

```c
PyObject *ws_frag_buffer;  /* bytearray or NULL */
```

Keep `ws_frag_opcode` and `ws_frag_size`. On the first non-final data frame, allocate a bytearray sized to that payload. On each continuation frame:

1. Check `payload_size > max_body_bytes - ws_frag_size` before resize or copy.
2. If the payload is empty, update frame state but do not resize or append an object.
3. Grow the bytearray with the existing CPython bytearray API and copy the new payload once.
4. Update `ws_frag_size` only after successful append.

On FIN:

- text: validate/decode directly from the bytearray buffer, creating only the final Unicode object;
- binary: create one exact `bytes` result from the buffer;
- clear the bytearray and reset fragment state before delivering the completed message;
- close/reset/error paths clear the accumulator.

The accumulator must never exceed `max_body_bytes`; fragment count no longer affects retained Python-object count. Do not add a separate fragment-count limit once empty fragments are allocation-free and non-empty storage is byte-bounded.

Tests must cover thousands of empty fragments, mixed empty/non-empty fragments, exact-limit success, one-byte-over failure 1009, invalid UTF-8 spanning fragments, close during fragmentation, and cleanup after transport loss.

### 4. Bound trie matching complexity

Files:

```text
src/neo/_native/router.c
src/neo/_pure/router.py
tests/test_routing_parity.py
tests/test_routing_modes.py
benchmarks/bench_router_pruning.py or the new pressure benchmark
```

Preserve static-over-parameter precedence and fallback semantics while preventing repeated exploration of the same failed state.

Implement a request-local C failed-state table for native `RouteTable.match()` keyed by:

```c
(RNode *node, Py_ssize_t segment_index)
```

The method is constant for one search and need not be part of the key. Requirements:

- allocate the table lazily only when a node has both a matching static child and a parameter child;
- use open addressing with geometric growth and pointer/index hashing;
- mark a state only after both possible branches fail for the requested method;
- consult the table before descending;
- free it on every success, miss, HEAD fallback, and error path;
- preserve captured parameter rollback when a branch fails;
- run HEAD fallback with a fresh/cleared failed-state table because the method changes to GET.

Mirror the semantic guard in `_pure/router.py` with a Python `set[(id(node), index)]` or node/index tuple so native/pure parity remains explicit. The pure implementation is correctness reference code, not the benchmark target.

For wide static fanout, keep each node’s `kid_segs`, lengths, and child pointers sorted lexicographically. Replace `rnode_find_kid` linear search with binary search. During registration, find the insertion position, grow all three arrays, shift the suffix once, and insert the new segment/child. Compare `memcmp` over the common prefix and use length as the tie-breaker. Registration order must not affect matching semantics.

Add an adversarial route generator to tests: routes contain literal/parameter alternatives at successive levels and omit the requested method at the terminal. Assert a miss without recursion explosion, plus unchanged literal precedence and captured parameters. Keep `MAX_SEGMENTS` behavior unchanged.

### 5. Make PostgreSQL tape consumption cursor-based

Files:

```text
src/neo/_native/postgres/tape.h
src/neo/_native/postgres/tape.c
src/neo/_native/postgres/decode.c
src/neo/_native/postgres/hydrate.c
tests/postgres/test_batch_decode.py
tests/orm/test_model_hydration.py
```

Add logical cursors:

```c
Py_ssize_t ref_head;
Py_ssize_t owner_head;
```

`ref_count` and `row_count` remain logical live counts. Introduce access helpers in `tape.h` and replace direct indexing in decode/hydrate code:

```c
NeoPgFieldRef *neo_pg_tape_ref(
    NeoPgFieldTape *tape,
    Py_ssize_t row,
    Py_ssize_t column
);

PyObject *neo_pg_tape_owner(
    NeoPgFieldTape *tape,
    uint32_t logical_owner_index
);
```

Consumption rules:

- consuming rows advances `ref_head` by `rows * stored_columns`;
- determine owners no longer referenced from the first surviving field or all owners when empty;
- advance `owner_head` without deleting the list prefix on every call;
- interpret stored owner indexes relative to the logical owner window;
- when empty, clear refs/owners and reset both heads;
- compact refs only when `ref_head >= 1024` and at least half the allocated entries are consumed;
- compact owners only when `owner_head >= 64` and at least half the list is consumed;
- when owner compaction changes the physical base, adjust surviving owner indexes once during that compaction;
- append must reuse consumed ref capacity when safe or compact before geometric growth;
- overflow-check `rows * stored_columns`.

No caller outside tape/decode/hydrate may directly assume `refs[0]` is logical row zero after this change.

Tests must consume 20,000 rows one at a time, cross multiple compactions, verify every decoded value, verify owner lifetimes through memoryviews, and then append/decode another batch using the reset tape.

### 6. Budget PostgreSQL retired-slab reclamation

Implement only after the concurrent `native-c-hotspots` work in `postgres/protocol.c` has merged.

Files:

```text
src/neo/_native/postgres/protocol.c
tests/postgres/test_receive_buffer.py
```

Add:

```c
Py_ssize_t retired_scan;
```

Replace full-list `reclaim_retired()` with a bounded rotating scan:

```c
static int reclaim_retired(NeoPgBufferedProtocol *self, Py_ssize_t budget);
```

Rules:

- inspect at most `budget` entries per call;
- continue from `retired_scan` on the next call instead of restarting at zero;
- when an entry is removed, inspect the item shifted into that position without advancing;
- clamp/reset the cursor when the list shrinks or becomes empty;
- `get_buffer()` uses a small fixed budget of 8;
- `ensure_spares()` may scan up to the current retired count only when no spare slab is available, stopping as soon as two spares exist;
- connection clear/shutdown may release the complete list directly and does not need reclamation scans;
- preserve the existing cap of four spare slabs.

Tests must pin at least 128 retired slabs, run repeated receive cycles, release a subset, and prove rotating scans eventually reclaim them without rescanning the complete pinned prefix every time. Add a test-only scan counter if needed; do not use timing assertions in pytest.

### 7. Decode PostgreSQL text `bytea` directly in C

Files:

```text
src/neo/_native/postgres/decode.c
src/neo/_native/postgres/codec.c
tests/postgres/test_codecs.py
tests/postgres/test_batch_decode.py
```

Add one shared internal decoder in the PostgreSQL codec layer rather than retaining two `binascii` call sites:

```c
PyObject *neo_pg_decode_hex_bytea(const unsigned char *data, Py_ssize_t length);
```

Contract:

- input excludes the leading `\\x` marker;
- reject odd length;
- allocate the exact output length once;
- decode ASCII `0-9`, `a-f`, and `A-F` through a 256-entry nibble table or explicit helper;
- reject the first invalid byte with `ValueError`;
- overflow-check output length;
- return `b""` for empty hex input;
- preserve existing binary-format behavior, which copies raw bytes once;
- preserve the current fallback behavior for non-hex text `bytea` unless parity tests establish stricter semantics.

Delete per-field `PyImport_ImportModule("binascii")` and method dispatch from both files. Tests must cover empty, mixed-case, invalid, odd-length, large, scalar-codec, and batch-decoder paths.

### 8. Measure and constrain multipart peak copying

Files:

```text
src/neo/_native/multipart.c
src/neo/multipart.py
src/neo/request.py
tests/test_native_parity.py
tests/test_client_sessions_forms.py
benchmarks/bench_native_http1_storage.py
```

This is a measurement-gated API decision. The current private native parser returns exact `bytes` for every part, while `Request.body()` retains the complete body. First record peak RSS for one large file part and many smaller parts.

Proceed with a zero-copy private result only if both conditions hold:

- part copies account for at least 20% of peak RSS in the 16 MiB case; and
- the change does not alter the public `UploadedFile.data` and `FormData` value types.

If the gate passes, change only the private native boundary:

- `_core.multipart_parse` returns `(headers, start, end)` offsets into the original body instead of copied content;
- `neo.multipart.parse` receives the original body and materializes exact `bytes` only for public values that escape;
- field values are decoded directly from a temporary buffer view;
- file values remain exact `bytes` to preserve `UploadedFile.data` behavior;
- validate every offset against the original body before slicing.

This removes duplicate temporary part bytes and tuples during parsing but cannot eliminate the final public file bytes while preserving the current contract. Do not switch public values to `memoryview` in this plan.

If the gate fails, leave production code unchanged and record the measured reason in `after.json` and the implementation report.

### 9. Remove hidden JSON key-cache ownership

Files:

```text
src/neo/_native/json.c
tests/test_native_parity.py
tests/test_native_perf.py
benchmarks/bench_native_http1_storage.py
```

Remove the process-global strong-reference cache:

```c
static PyObject *neo_key_cache[NEO_KEY_CACHE_SIZE];
```

Decode object keys through the existing ASCII/UTF-8 string construction path. Do not replace it with another mutable static or intern attacker-controlled keys.

Before removal, benchmark repeated stable keys and high-cardinality key churn. Accept removal when either:

- median stable-key decode throughput regresses by no more than 5%; or
- the cache does not improve stable-key median outside trial noise.

If a repeatable regression exceeds 5%, stop and record a separate ADR proposal for per-module cache state; do not implement module-state conversion incidentally in this plan. High-cardinality input must no longer retain key objects after decoded documents are released.

Add a weak-reference-capable wrapper test or reference-count/allocated-block subprocess test demonstrating no extension-owned key retention. Keep functional JSON parity unchanged.

### 10. Cache parsed cookies per request

Files:

```text
src/neo/request.py
tests/test_request.py
tests/test_client_sessions_forms.py
```

Add `_cookies` to `Request.__slots__` and initialize it to `_MISSING`. The `cookies` property parses once, stores the resulting dictionary, and returns the same dictionary on subsequent access, matching existing mutable request-local cache conventions.

Rules:

- no Cookie header caches one empty dictionary rather than allocating a new dictionary per access;
- duplicate-cookie first-value behavior remains unchanged;
- cache ownership is request-local;
- materializing a compatibility scope must not invalidate the cache;
- request reuse/reset, if supported anywhere, must clear it with `_body` and `_header_map`.

Tests must assert parser-equivalent contents and object identity across repeated property reads.

## Measurement-only follow-up: decision-router allocations

The native decision router allocates a segment-count `PyLong` and Unicode segment keys during matching. The concurrent `native-c-hotspots` plan changes `dtrouter.c`, so this plan must not edit it.

Add counters/benchmark coverage only after that work merges:

- static hit with no parameters;
- dynamic hit;
- method miss;
- protected-route classification.

Record allocations per operation with `tracemalloc` or allocated-block deltas in a subprocess. If static hits still allocate, open a follow-up plan based on the merged structure. Do not preselect a replacement branch representation here.

## Correctness rules

- Slow-input CPU work must scale with newly received bytes, not total buffered-prefix length.
- Delimiters split across any receive boundary are recognized exactly once.
- HTTP/WebSocket queue pressure is bounded by both bytes and message count.
- Empty WebSocket fragments retain no per-fragment Python object.
- WebSocket fragmented messages remain ordered and enforce `max_body_bytes` before growth.
- Trie optimization preserves static-over-parameter precedence, HEAD-to-GET fallback, path parameters, trailing slashes, and duplicate-route errors.
- PostgreSQL tape cursors cannot expose consumed rows or release a slab still referenced by a surviving field.
- Retired-slab scan budgeting must eventually examine every retained entry.
- Native text `bytea` decoding remains byte-for-byte compatible with the pure backend.
- Multipart optimization cannot change public field/file value types.
- Attacker-controlled JSON keys are not interned or retained globally.
- Cookie caching is request-local and does not create cross-request state.

## Expected files touched

```text
src/neo/server.py
src/neo/request.py
src/neo/multipart.py
src/neo/_native/server.h
src/neo/_native/server_common.c
src/neo/_native/server_http1.c
src/neo/_native/router.c
src/neo/_native/postgres/tape.h
src/neo/_native/postgres/tape.c
src/neo/_native/postgres/decode.c
src/neo/_native/postgres/hydrate.c
src/neo/_native/postgres/codec.c
src/neo/_native/postgres/protocol.c
src/neo/_native/multipart.c
src/neo/_native/json.c
src/neo/_pure/router.py
tests/test_server_protocol.py
tests/test_server_fuzz.py
tests/test_server_websocket.py
tests/test_routing_parity.py
tests/test_routing_modes.py
tests/postgres/test_receive_buffer.py
tests/postgres/test_batch_decode.py
tests/postgres/test_codecs.py
tests/orm/test_model_hydration.py
tests/test_native_parity.py
tests/test_native_perf.py
tests/test_request.py
tests/test_client_sessions_forms.py
benchmarks/bench_native_http1_storage.py
benchmarks/README.md
docs/reference/server.md or the existing server configuration reference
```

`src/neo/_native/dtrouter.c` is intentionally excluded.

## Focused verification

Run the focused tests after their corresponding slices:

```bash
uv run pytest tests/test_server_protocol.py tests/test_server_fuzz.py
uv run pytest tests/test_server_websocket.py
uv run pytest tests/test_routing_parity.py tests/test_routing_modes.py
uv run pytest tests/postgres/test_receive_buffer.py tests/postgres/test_batch_decode.py tests/postgres/test_codecs.py
uv run pytest tests/orm/test_model_hydration.py
uv run pytest tests/test_native_parity.py tests/test_native_perf.py
uv run pytest tests/test_request.py tests/test_client_sessions_forms.py
```

Run complete checks after all slices:

```bash
uv run pytest
uv run pytest -m '' -n 4
uv run ruff check .
uv run ty check
uv run --group docs mkdocs build --strict
```

Run the native server and PostgreSQL sanitizer procedures documented in `docs/plans/native-server-sanitizers.md` and the applicable native ORM plan. There must be no out-of-bounds cursor, stale buffer pointer, use-after-free, integer overflow, or leaked stream/tape owner.

Record after-results separately:

```bash
uv run python -m benchmarks.bench_native_http1_storage \
  --scenario all --warmup 2 --trials 9 \
  --output benchmark-results-native-http1-storage/after.json
```

Run before and after with the same Python build, native compiler flags, machine, loop, and load conditions.

## Acceptance checks

- Delivering a 16 KiB incomplete HTTP head one byte at a time takes less than 2.5 times the median time for 8 KiB; the previous quadratic path is retained in `before.json`.
- Chunk-size and trailer delimiter scans satisfy the same less-than-2.5× doubling bound.
- Draining 20,000 HTTP/WebSocket queue entries takes less than 2.5 times the 10,000-entry median.
- Empty WebSocket messages pause reading at `read_high_water_messages` even though queued payload bytes are zero.
- Twenty thousand empty continuation frames do not increase retained fragment storage with frame count and complete correctly when FIN arrives.
- An over-limit fragmented message fails with close code 1009 before accumulator growth.
- Trie adversarial misses at depth 14 complete without exponential growth; benchmark work/time is consistent with the number of reachable node/index states. Wide-fanout 2,000-child lookup takes less than 2.5 times the 1,000-child median.
- Consuming 20,000 PostgreSQL tape rows one at a time takes less than 2.5 times the 10,000-row median and preserves every decoded value.
- With 256 pinned retired slabs, each normal receive cycle examines no more than the configured reclamation budget, and released slabs are eventually reused.
- Native text `bytea` decoding performs no per-value import or Python method call and remains parity-correct.
- Multipart production code changes only if the documented 20% peak-RSS gate passes; public `UploadedFile.data` remains `bytes`.
- High-cardinality JSON documents leave no extension-owned key references after results are released; stable-key performance respects the 5% decision gate.
- Repeated `request.cookies` access returns the same request-local dictionary and parses once.
- `before.json` and `after.json` retain nine raw timing/RSS samples and complete environment metadata for every available scenario.
- Full tests, lint, type checking, strict docs, and sanitizer checks pass.

## Implementation order

Use this order to minimize conflicts and establish security bounds first:

1. benchmark harness and untouched baseline;
2. incremental HTTP/1 scanning;
3. HTTP/WebSocket indexed queue plus message-count watermark;
4. bounded WebSocket fragment accumulator;
5. trie memoization and sorted child lookup;
6. PostgreSQL tape cursors;
7. native text `bytea` decoder;
8. multipart measurement gate;
9. JSON cache removal gate;
10. request cookie cache;
11. after the concurrent plan merges: retired-slab scan budgeting and benchmark README integration;
12. full checks, sanitizers, after-benchmarks, and retained raw results.

Keep the HTTP/1/WebSocket security changes separate from PostgreSQL and routing review units. Do not fold this work back into `native-c-hotspots.md`.
