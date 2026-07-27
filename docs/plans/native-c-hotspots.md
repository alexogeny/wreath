# Prescriptive plan: native C CPU and memory-pressure fixes

Status: ready for test-first implementation

Related material:

- `AGENTS.md`
- `docs/plans/native-http2-http3.md`
- `docs/plans/native-server-sanitizers.md`
- `docs/plans/native-buffered-protocol-ingress.md`
- `benchmarks/README.md`

## Goal

Remove the identified superlinear queue and buffer operations, enforce the HTTP/3 request-body limit, and introduce real HTTP/2 response flow-control backpressure. Preserve ASGI and protocol semantics, keep `src/neo` dependency-free, and retain reproducible before/after CPU and resident-memory evidence. Do not claim a performance improvement from a single run or from timing alone.

This work covers these confirmed sites:

- unbounded HTTP/3 request buffering in `src/neo/_native/http3_asgi.c`;
- unbounded and quadratically shifted HTTP/2 response buffering in `src/neo/_native/server_http2.c`;
- front-deleted request/control-message lists in HTTP/2, HTTP/3, and PostgreSQL;
- fully buffered HTTP/3 responses;
- quadratic split selection and duplicated access-clause tuples in `src/neo/_native/dtrouter.c`;
- the portable `neo_memmem` fallback in `src/neo/_native/neocore.h`.

## Repository constraints

- Target CPython 3.14 and handwritten C using the existing CPython C API.
- Add no mandatory runtime dependency. The HTTP/3 extension remains opt-in and may use only its existing ngtcp2/nghttp3/TLS dependencies.
- Preserve ASGI behavior before optimizing. In particular, `await send(...)` may apply backpressure, request-body chunks remain ordered, `more_body` remains accurate, and stream reset/disconnect must resolve blocked application tasks.
- Keep protocol state owned by the connection or stream object; add no global queues or counters.
- Use geometric growth, cursors, and occasional compaction rather than deleting or moving the front on every operation.
- Follow TDD for each behavior slice and call `update_feature_tdd` at red, green, refactored, and done.
- Run the native sanitizer suites after C changes. Keep raw benchmark results, environment metadata, and every trial value.
- Do not alter HTTP/1 buffering merely for symmetry. Its thresholded compaction in `server_http1.c` is not part of this change.

## Required baseline before implementation

Create `benchmarks/bench_native_pressure.py` before changing C. It must run each case in a fresh subprocess so peak RSS is attributable to one case. The parent command writes one JSON document and the child command writes one scenario result to stdout.

Required CLI:

```text
python -m benchmarks.bench_native_pressure \
  --scenario all \
  --warmup 2 \
  --trials 9 \
  --output benchmark-results-native-pressure/before.json

python -m benchmarks.bench_native_pressure \
  --scenario h2-blocked-send|h2-flush-scaling|h2-request-queue|h3-request-limit|router-compile \
  --warmup 2 --trials 9 --output PATH
```

Each scenario record must include:

```json
{
  "scenario": "h2-blocked-send",
  "python": "full sys.version",
  "platform": "platform.platform()",
  "implementation": "sys.implementation.name",
  "executable": "sys.executable",
  "neo_version": "installed/project version",
  "native_module": "resolved extension path",
  "parameters": {},
  "warmup_trials": 2,
  "measured_trials": 9,
  "raw_seconds": [],
  "median_seconds": 0.0,
  "p95_seconds": 0.0,
  "raw_peak_rss_bytes": [],
  "median_peak_rss_bytes": 0,
  "errors": []
}
```

Use `time.perf_counter_ns()` for elapsed time. Use `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss` in each child and normalize Linux KiB versus macOS bytes explicitly. Record the normalization rule. If `resource` is unavailable, record RSS as unavailable rather than substituting an unrelated measure.

The baseline must exercise:

1. **`h2-blocked-send`** — direct `Http2Protocol` plus the existing fake transport pattern from `tests/http2/conftest.py`; peer windows are exhausted, and an ASGI app attempts a 64 MiB response as 4 KiB awaited chunks. Record whether the app reaches each chunk, peak RSS, and bytes written.
2. **`h2-flush-scaling`** — release 16 MiB and 32 MiB blocked responses through 16 KiB WINDOW_UPDATE increments. Record elapsed time separately for each size. This exposes repeated front `memmove`.
3. **`h2-request-queue`** — buffer and consume 25,000 and 50,000 small DATA chunks while respecting protocol windows. Record consume time and peak RSS.
4. **`h3-request-limit`** — only when the optional backend is importable; send beyond a small configured body limit and record accepted bytes and terminal stream behavior. Record `unavailable` when the extension is not built.
5. **`router-compile`** — extend the construction pattern in `benchmarks/bench_router_pruning.py` and measure compile-only time for 5,000 and 10,000 routes with common prefixes, distinct literals, wildcards, and inherited access clauses. Do not include route creation in the compile timer.

Save the untouched baseline as `benchmark-results-native-pressure/before.json`. Never overwrite it with after-results. Add the new command and schema to `benchmarks/README.md`.

## Shared indexed-queue rule

HTTP/2, HTTP/3, and PostgreSQL currently enqueue into a Python list and call `PySequence_DelItem(list, 0)`. Replace that pattern with an owned list plus a head index in each owning object. Do not add a new Python-visible queue type.

For each queue:

- append with `PyList_Append`;
- read `PyList_GET_ITEM(list, head)` and increment `head` after taking the needed reference;
- logical length is `PyList_GET_SIZE(list) - head`, never raw list length;
- when logical length reaches zero, clear the list with `PyList_SetSlice(list, 0, size, NULL)` and reset `head` to zero;
- otherwise compact only when `head >= 64` and `head * 2 >= PyList_GET_SIZE(list)`;
- compact with one `PyList_SetSlice(list, 0, head, NULL)` and reset `head` to zero;
- never compact while holding only a borrowed item from the prefix being removed;
- include the whole list in GC traversal as today; consumed objects are released by reset/compaction;
- initialize the index to zero and reset it from `tp_clear`/connection reset paths.

A small file-local helper is preferred where a file owns more than one queue operation. Do not introduce a cross-extension shared ABI for this primitive.

## Implementation tasks

### 1. Enforce HTTP/3 request-body bytes

Files:

```text
src/neo/_native/http3.h
src/neo/_native/http3_asgi.c
tests/http3/test_limits.py
tests/http3/test_asgi.py
```

Add these fields to `NeoH3Stream`:

```c
Py_ssize_t body_head;
Py_ssize_t body_received;
```

Initialize both to zero in `begin_headers_cb`.

In `recv_data_cb`:

1. Reject `datalen > PY_SSIZE_T_MAX` before casting.
2. Check overflow before addition: `body_received > PY_SSIZE_T_MAX - (Py_ssize_t)datalen` is a limit failure.
3. Compute the prospective total before creating a Python `bytes` object.
4. If `max_body_bytes > 0` and the prospective total exceeds it, stop delivering or buffering data, terminate only the request stream with the HTTP/3 excessive-load/application error already used by the extension’s stream-error path, mark the stream disconnected, and resolve any pending `receive()` with `http.disconnect`.
5. Otherwise update `body_received` and deliver or queue the chunk.

Delete the current no-op block at `http3_asgi.c:533-537`. The limit is measured in payload bytes, not number of chunks. The rejected chunk must not be allocated or appended.

Convert `body_chunks` to the shared indexed-queue rule using `body_head`. `h3_stream_receive` must compute `more_body` from logical queue length plus `request_ended`.

Replace the skipped body-limit test with executable coverage proving:

- exactly-at-limit succeeds;
- one byte over the limit terminates the stream without terminating an unrelated stream;
- many one-byte chunks are counted by bytes;
- overflow is rejected before allocation;
- a waiting receiver gets disconnect on rejection;
- no rejected bytes remain reachable from `body_chunks`.

HTTP/3 tests may skip only when the optional extension is unavailable. Implemented behavior must not retain the current unconditional `pytest.skip`.

### 2. Remove HTTP/2 request-queue front deletion

Files:

```text
src/neo/_native/server_http2.c
tests/http2/test_asgi.py
tests/http2/test_flow_control.py
```

Add `Py_ssize_t body_head` next to `body_chunks` in `Http2Stream`. Initialize, traverse, clear, and reset it with the stream. Apply the shared indexed-queue rule in `stream_receive` and `deliver_body`.

Add tests that feed at least 256 separately framed chunks to cross the compaction threshold. Assert exact concatenated order, correct `more_body`, list reset after drain through a test-visible behavior rather than exposing internals, and correct delivery after the queue has compacted once.

Do not issue WINDOW_UPDATE because data was merely queued. Preserve the existing rule that receive-side credit follows application consumption.

### 3. Replace HTTP/2 copied pending output with one retained send

Files:

```text
src/neo/_native/server_http2.c
tests/http2/test_flow_control.py
tests/http2/test_asgi.py
tests/http2/test_shutdown.py
```

Replace:

```c
PyObject *pending_data;  /* bytearray */
int pending_end_stream;
```

with:

```c
PyObject *pending_body;    /* exact bytes object retained while flow-control blocked */
Py_ssize_t pending_offset; /* next unsent payload byte */
PyObject *send_waiter;     /* Future returned by _send, or NULL */
int pending_end_stream;
```

Rules:

- Require response bodies to remain `bytes`, matching current behavior.
- If no body send is pending, frame directly from the caller’s immutable bytes.
- When flow control prevents completion, retain the body with `Py_NewRef`, record the offset, create a Future with existing `h2_make_future`, and return that Future from `_send`.
- Resolve `send_waiter` with `None` only after all bytes from that ASGI body message have been framed, including END_STREAM when applicable.
- A conforming app cannot submit the next body message until the prior await completes. Defensively raise `RuntimeError("HTTP/2 send already pending")` if `_send` receives another body/trailers message while `send_waiter` exists.
- `h2_flush_stream_pending` sends frames from `PyBytes_AS_STRING(pending_body) + pending_offset`; it only increments the offset. It must perform no `memmove`, no front resize, and no duplicate body copy.
- On completion, clear `pending_body`, reset the offset/end flag, detach the waiter, and call `set_result(None)` once.
- On RST_STREAM, connection error, transport loss, task cancellation, or stream deallocation, detach and cancel the waiter if it is not already done; release the retained body. Never leave the application task suspended.
- Include `pending_body` and `send_waiter` in GC traverse/clear.
- Preserve multiplexing: a blocked stream must not prevent another stream with credit from sending.

Add focused tests proving:

- `await send()` remains pending when both stream and connection windows are zero;
- WINDOW_UPDATE frames release it only after the whole message is framed;
- a second stream progresses while the first is blocked;
- reset, connection close, and application cancellation unblock cleanup;
- a 64 MiB logical streaming response produced as awaited 4 KiB chunks cannot advance beyond one blocked chunk;
- END_STREAM is emitted once and only after the final pending byte;
- no body `memmove` behavior remains (enforce with source assertion only if no behavioral assertion can distinguish it).

Do not add a public write-watermark setting in this slice. Flow control plus one outstanding awaited body provides the bound; a single large `bytes` remains owned by the application and is retained, not copied.

### 4. Remove PostgreSQL control-message front deletion

Files:

```text
src/neo/_native/postgres/protocol.c
tests/postgres/ (existing buffered-protocol test module)
```

Add `Py_ssize_t messages_head` beside `messages`, initialize and reset it with the protocol, and apply the shared indexed-queue rule in `deliver_message` and `buffered_read_message`.

Add a regression test that queues and drains more than 128 control messages, crosses compaction, then queues and drains another message. Assert exact order and no loss. Keep DataRow handling on its current slab/tape path; it must not be routed through this queue.

### 5. Stream HTTP/3 responses without a reallocating bytearray

Files:

```text
src/neo/_native/http3.h
src/neo/_native/http3_asgi.c
src/neo/_native/http3_connection.c
tests/http3/test_asgi.py
tests/http3/test_stream_state.py
tests/http3/test_network.py
```

This slice must follow the existing ngtcp2 acknowledgement path: `acked_stream_data_offset_cb` calls `neo_h3_acked_stream_data`, which calls `nghttp3_conn_add_ack_offset`. Do not release storage merely because `read_response_data` returned it; retransmission may still reference it.

Replace `resp_body` and `resp_read_off` with an immutable segment queue:

```c
PyObject *resp_chunks;         /* list[bytes], stable addresses */
Py_ssize_t resp_head;          /* first retained segment */
Py_ssize_t resp_read_index;    /* segment currently offered to nghttp3 */
Py_ssize_t resp_read_offset;   /* offset inside that segment */
uint64_t resp_payload_acked;   /* payload bytes safe to release */
PyObject *send_waiter;         /* current ASGI body send, if backpressured */
```

Before implementing release, add a focused C comment beside `neo_h3_acked_stream_data` documenting the installed nghttp3 contract used to map acknowledged QUIC stream offsets to application DATA bytes. The implementation must use nghttp3’s acknowledgement accounting, not treat raw QUIC `datalen` as payload bytes. If the installed nghttp3 API cannot report payload ranges that are safe to release, retain immutable chunks until stream close and record that limitation in the benchmark result; do not invent offset arithmetic. In that fallback, this slice may deliver true streaming and eliminate reallocating/copying, but must not claim bounded HTTP/3 response retention.

Required behavior:

- Submit headers and the data reader on `http.response.start`, not after the final body message.
- Append each exact immutable body object to `resp_chunks`; never concatenate it into a bytearray.
- `read_response_data` fills up to `veccnt` vectors from stable bytes segments, advances read cursors, returns `NGHTTP3_ERR_WOULDBLOCK` when temporarily empty, and sets EOF only after `more_body=False` and all queued bytes have been offered.
- Call `nghttp3_conn_resume_stream` whenever new data or EOF becomes available.
- Keep every exposed bytes object alive for the complete lifetime required by nghttp3/ngtcp2 retransmission.
- Resolve or cancel any HTTP/3 send waiter according to the same stream close/disconnect rules as HTTP/2.
- Apply indexed queue compaction only to segments proven safe to release; never move or decref an exposed segment early.

Tests must prove first response bytes are transmitted before the app sends its final body message, chunk order is exact, empty chunks do not produce premature EOF, reset releases the app, and a retransmission/ack sequence under ASan does not access freed memory.

### 6. Reduce decision-router compile complexity and metadata duplication

Files:

```text
src/neo/_native/dtrouter.c
benchmarks/bench_router_pruning.py
tests/ (existing decision-router tests)
```

For split selection in `dnode_build`, replace the `for i` / `for j < i` distinct-literal scan with a temporary Python set per candidate position:

- create the set once for each unused segment position;
- add the existing literal bytes object/key representation used by route compilation;
- `distinct` is `PySet_GET_SIZE` after all literal candidates are visited;
- preserve the current score `distinct * 1000 + literal_count` and tie behavior;
- decref the set on every success/error path;
- do not change route precedence or wildcard semantics.

If route segments currently retain only raw pointers and lengths, create temporary `bytes` keys solely for split selection. Measure first; do not add permanent per-segment Python objects without evidence.

Stop storing the full concatenated access-clause tuple at every internal node. Store only the capability summary needed to prune that node, reusing the existing compiled capability representation. Keep full ordered route clauses only at leaves where final authorization evaluation occurs. If the matcher currently consumes internal `access_clauses`, first add a test that captures current pruning behavior, then replace that use with the summary in the same change.

Required tests cover unchanged match result, precedence, path parameters, wildcard routes, inherited authorization, and anonymous pruning across route tables that force multiple decision levels.

Extend `bench_router_pruning.py` so its output is JSON and includes raw compile trials, route count, shape parameters, Python/platform metadata, and native/pure implementation selection. Preserve existing eligible/pruned match measurements.

### 7. Make portable substring search linear-time

Files:

```text
src/neo/_native/neocore.h
tests/test_native_parity.py
benchmarks/bench_native_pressure.py
```

Keep libc `memmem` on platforms where it is already selected. Replace only the portable fallback with a small, documented linear worst-case implementation suitable for needles up to multipart’s 74-byte delimiter. Use a two-way search or another dependency-free linear algorithm; do not add a library.

Add direct native parity cases with repetitive haystacks and overlapping prefixes. Add a benchmark case only when the fallback can be compiled explicitly in CI or a local diagnostic build; results from glibc `memmem` do not validate the fallback. Provide a test-only compile define that selects the fallback without changing release platform detection.

## Correctness rules

- Limits are checked before allocation and before narrowing `size_t` to `Py_ssize_t`.
- Body limits count payload bytes across all chunks, including chunks delivered directly to a waiting receiver.
- Request and response ordering is unchanged.
- Queue compaction never invalidates a borrowed reference.
- A blocked `send()` completes only when its own message is accepted by protocol flow control, not merely copied into an unbounded staging buffer.
- Stream-local pressure or limit failure must not terminate unrelated multiplexed streams.
- Every pending Future is resolved or cancelled exactly once on success, reset, disconnect, protocol error, and deallocation.
- Immutable HTTP/3 response storage remains alive for as long as retransmission can reference it.
- Router optimization cannot alter specificity, tie ordering, wildcard behavior, or authorization results.
- Performance-only tests assert scaling/bounds, not machine-specific absolute throughput.

## Files touched

Expected production and test files:

```text
src/neo/_native/server_http2.c
src/neo/_native/http3.h
src/neo/_native/http3_asgi.c
src/neo/_native/http3_connection.c
src/neo/_native/postgres/protocol.c
src/neo/_native/dtrouter.c
src/neo/_native/neocore.h
tests/http2/test_asgi.py
tests/http2/test_flow_control.py
tests/http2/test_shutdown.py
tests/http3/test_asgi.py
tests/http3/test_limits.py
tests/http3/test_stream_state.py
tests/http3/test_network.py
tests/test_native_parity.py
benchmarks/bench_native_pressure.py
benchmarks/bench_router_pruning.py
benchmarks/README.md
docs/native/README.md
docs/native/postgres.md
```

Use the actual existing PostgreSQL test module discovered during implementation rather than creating a parallel test hierarchy solely to match the placeholder above.

## Verification commands

Run focused tests after each slice:

```bash
uv run pytest tests/http2/test_asgi.py tests/http2/test_flow_control.py tests/http2/test_shutdown.py
uv run pytest tests/http3/test_asgi.py tests/http3/test_limits.py tests/http3/test_stream_state.py tests/http3/test_network.py
uv run pytest tests/test_native_parity.py
uv run pytest tests/postgres -q
```

If PostgreSQL tests are not under `tests/postgres`, substitute the discovered existing files and record the exact command in the implementation report.

Run project checks after all slices:

```bash
uv run pytest
uv run pytest -m '' -n 4
uv run ruff check .
uv run ty check
uv run --group docs mkdocs build --strict
```

Run the sanitizer procedure from `docs/plans/native-server-sanitizers.md`, including HTTP/2 and HTTP/3 where the optional backend is available. There must be no ASan/UBSan error, leak attributable to a closed stream, use-after-free during HTTP/3 retransmission, or reference leak across repeated queue compaction.

Record after-results separately:

```bash
uv run python -m benchmarks.bench_native_pressure \
  --scenario all --warmup 2 --trials 9 \
  --output benchmark-results-native-pressure/after.json
```

Run before and after on the same checkout environment, Python build, event loop, compiler flags, and machine load conditions. Keep both JSON files and include their paths in the final report.

## Acceptance checks

- HTTP/3 rejects a request one byte over `max_body_bytes` before allocating the rejected chunk; another stream on the connection remains usable.
- HTTP/2 request chunks, HTTP/3 request chunks, and PostgreSQL control messages drain in exact FIFO order after at least one compaction.
- With zero HTTP/2 send window, an app producing awaited 4 KiB chunks stops after one pending chunk; it does not construct or queue the remainder of a 64 MiB response.
- HTTP/2 pending output contains no front `memmove` or front bytearray resize, and 32 MiB flush time is less than 2.5 times the 16 MiB median under the same 9-trial run. Any miss must be investigated and reported rather than hidden.
- Doubling queued chunk count from 25,000 to 50,000 takes less than 2.5 times the median drain time for each indexed queue benchmark that is available.
- HTTP/3 emits response bytes before `more_body=False`. Storage exposed to retransmission remains valid under ASan. If acknowledged-payload release is supported, retained response storage falls as acknowledgements arrive; otherwise the report explicitly states retention lasts until stream close and makes no bounded-memory claim.
- Decision-router 10,000-route compile time is less than 2.8 times the 5,000-route median for the same route shape, while all routing and authorization tests remain unchanged.
- The portable substring fallback passes repetitive-prefix tests and shows linear scaling when forced in a diagnostic build.
- `before.json` and `after.json` contain nine raw timing and RSS values per available scenario plus complete environment metadata.
- Full tests, lint, type checking, strict docs build, and sanitizer checks pass.
- No result is described as a win unless repeated medians improve or scaling/resource bounds improve without regressions in correctness, p95, errors, or peak RSS.

## Implementation order

Implement in this dependency order, completing red/green/refactor and focused measurements for each item before moving on:

1. benchmark harness and untouched baseline;
2. HTTP/3 request limit;
3. indexed request/control queues;
4. HTTP/2 retained-message backpressure and offset flushing;
5. HTTP/3 immutable response segments and acknowledgement-safe lifetime;
6. decision-router compile optimization and metadata reduction;
7. portable substring fallback;
8. full checks, sanitizers, after-benchmarks, docs, and retained raw results.

Do not combine the HTTP/2 backpressure and HTTP/3 response-lifetime changes into one review unit; their ownership and failure modes are different.
