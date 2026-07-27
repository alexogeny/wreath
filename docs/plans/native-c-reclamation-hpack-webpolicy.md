# Prescriptive plan: slab reclamation, HPACK decoding, and header insertion

Status: **ready for test-first implementation**

Related material:

- `AGENTS.md`
- `docs/agents/index.md`
- `docs/native/README.md`
- `docs/native/postgres.md`
- `docs/plans/native-server-sanitizers.md`
- `benchmarks/README.md`

## Goal

Remove cumulative quadratic work from PostgreSQL retired-slab reclamation and web-policy header insertion, and replace HPACK's bit-at-a-time Huffman decoder with a measured table-driven implementation. Preserve protocol behavior, object ownership, response-header semantics, and native/pure parity.

## Repository constraints

- Target CPython 3.14 and add no runtime dependency.
- Use test-first changes and record TDD transitions with `update_feature_tdd`.
- Preserve HTTP/2 and RFC 7541 error classification exactly.
- Preserve PostgreSQL slab ownership and the reclamation budget of eight inspections per normal receive.
- Preserve response-header order, unrelated duplicate headers, and case-insensitive header-name matching.
- Do not claim performance from one run. Retain repeated before/after results and establish an A/A noise floor.
- Run the existing `_server`, `_postgres`, and `_core` sanitizer workflows after their respective C changes.

## 1. Make PostgreSQL retired-slab removal constant-time

### Files

```text
src/neo/_native/postgres/protocol.c
tests/postgres/test_receive_buffer.py
```

### Implementation

`reclaim_retired()` currently removes an arbitrary Python-list element with `PySequence_DelItem(self->retired, self->retired_scan)`. Every removal shifts the suffix, so reclaiming many released slabs can perform cumulative quadratic work even though each receive scans only a bounded number of entries.

Replace suffix-shifting deletion with a file-local unordered-removal helper:

```c
static int
retired_swap_delete(PyObject *list, Py_ssize_t index);
```

Rules:

1. If `index` is the final element, delete the final element normally.
2. Otherwise:
   - take the final item;
   - increment its reference before replacing the indexed item;
   - use `PyList_SetItem()` to replace the indexed item and release the removed list reference;
   - delete the old final slot.
3. Leave `retired_scan` at the replacement position so the moved entry is inspected next rather than skipped.
4. Preserve cursor reset and clamping when the list shrinks or becomes empty.
5. Preserve the spare-slab cap of four.
6. Preserve `ensure_spares()` stopping once enough slabs exist.

Document the ownership transition beside the helper. The spare list owns its reference to a reclaimed slab. The retired list either releases that slab reference or transfers its final item into the vacated slot. No borrowed reference may survive a list replacement or deletion that can release its owner.

### Complexity instrumentation

Extend private `_receive_stats()` with:

```text
retired_reclaims
retired_move_steps
```

- Increment `retired_reclaims` once per removed slab.
- Increment `retired_move_steps` by at most one for a swap.
- For the initial red test, instrument the current shifting implementation by the number of suffix slots shifted. This exposes quadratic work without a wall-clock assertion.

The counters are protocol-owned test diagnostics, matching the existing `retired_scan_steps` approach. Do not add mutable process-global counters.

### Tests

Extend `tests/postgres/test_receive_buffer.py` with cases that:

- pin and then release all 128 and 256 retired slabs;
- drive enough ordinary receive cycles to reclaim all released slabs;
- release alternating slabs so swap-delete repeatedly moves both pinned and reclaimable entries;
- reclaim the current cursor item, a middle item, the final item, and the final remaining item;
- prove moved entries are eventually examined rather than skipped;
- prove pinned memoryviews remain valid and unreclaimed;
- prove reclaimed slabs are reused and `idle_slabs <= 4`;
- prove each ordinary receive still inspects no more than eight entries;
- assert `retired_move_steps <= retired_reclaims` after the fix;
- assert doubling released slabs approximately doubles counter work rather than quadrupling it.

Run this path under `tools/sanitizers/build_postgres.py` to catch use-after-free, double decref, leaks, and invalid list ownership.

## 2. Replace bit-at-a-time HPACK Huffman decoding

This is a measurement-gated optimization, not a correctness repair. The current decoder is linear but performs up to eight dependent tree transitions per compressed byte.

### Files

```text
src/neo/_native/server_hpack.c
tests/http2/support.py
tests/http2/test_hpack_vectors.py
tests/http2/test_hpack_errors.py
benchmarks/bench_hpack_decode.py
benchmarks/README.md
```

### Baseline before implementation

Add `benchmarks/bench_hpack_decode.py` before changing the decoder. Measure repeated decoding of:

- common pseudo-header values and paths;
- short ASCII headers;
- cookie-sized values;
- 1 KiB and 16 KiB legal header values;
- values mixing short and long Huffman codes;
- malformed blocks separately from successful-throughput summaries.

Record warmups, nine measured trials, every raw duration, median, p95, errors, Python version, platform, compiler/build metadata where available, and the resolved native extension path. Establish an interleaved A/A noise floor before deciding whether the table decoder is retained.

### Generated transition table

Keep `NEO_HUFF` as the sole source of truth. During `neo_hpack_build_huffman()`:

1. Build the existing bit tree.
2. Generate a transition for every reachable internal state and every input byte by simulating the current eight bit transitions.
3. Precompute final-state padding metadata.

Use a compact entry such as:

```c
typedef struct {
    int16_t next_state;
    uint8_t output[2];
    uint8_t output_count;
    uint8_t flags;
} HuffTransition;
```

Flags distinguish:

- a valid transition;
- an invalid tree path;
- the forbidden EOS symbol.

An eight-bit transition can emit at most two symbols because the shortest HPACK code is five bits. Document this invariant next to the structure and assert it while generating the table.

For each internal state, also retain:

```text
depth since the last emitted symbol
whether the state's pending bits are an all-ones EOS prefix
```

The final padding rule remains exactly:

```text
root state
or pending depth < 8 and pending bits are an EOS prefix
```

This must remain equivalent to the current `bits_since_leaf` and `all_ones` checks.

### Decode loop

Replace the nested byte/bit loop with one transition lookup per compressed byte:

1. Read the transition for `(state, data[i])`.
2. Reject an invalid transition or EOS immediately.
3. Emit zero, one, or two decoded bytes.
4. Advance to `next_state`.
5. After input ends, apply the precomputed final-state padding validation.

Retain checked output bounds. The current `len * 2 + 8` allocation is sufficient because the shortest code is five bits, but check the multiplication/addition against `PY_SSIZE_T_MAX` and document the bound.

`neo_hpack_build_huffman()` must clean up every partially allocated tree/table on failure. `neo_hpack_free_huffman()` must release all generated storage safely. Do not weaken malformed-input handling to simplify the fast path.

### Correctness tests

Extend the existing HTTP/2 tests rather than creating a separate protocol harness.

Add coverage for:

- all existing RFC 7541 vectors unchanged;
- every byte value `0..255` encoded independently with the test reference encoder;
- legal field-value bytes arriving exactly in the ASGI scope;
- forbidden HTTP field bytes reaching header validation and producing its expected protocol/stream error rather than HPACK `COMPRESSION_ERROR`, proving Huffman decoding itself succeeded;
- legal ASCII values at every possible residual bit alignment;
- seeded differential values around transition and allocation boundaries;
- symbols spanning code lengths from 5 through 30 bits;
- transitions that emit two symbols;
- EOS beginning at different bit offsets;
- invalid padding at each residual width from one through seven bits;
- valid all-ones padding at each residual width;
- padding of eight or more bits being rejected;
- truncation at every byte boundary of representative long-code sequences;
- exact `GOAWAY(COMPRESSION_ERROR)` behavior for malformed blocks;
- malformed blocks never invoking ASGI.

Run the complete in-process `tests/http2` suite under the `_server` ASan/UBSan build.

### Retention gate

Keep the eight-bit table decoder only when repeated trials:

- clear the measured A/A noise floor on representative Huffman-heavy workloads;
- do not materially regress short common headers;
- keep generated-table memory bounded and documented;
- preserve every RFC, malformed-input, and sanitizer result.

If the 256-way table is too large for its measured gain, evaluate the same generated design with four-bit transitions. Do not retain a large table merely because it performs fewer loop iterations.

## 3. Remove quadratic web-policy header insertion

### Files

```text
src/neo/_native/webpolicy.c
src/neo/_pure/webpolicy.py
tests/test_webpolicy_parity.py
benchmarks/bench_web_policy_compression.py
benchmarks/README.md
```

### Lock duplicate-addition semantics

There is an existing native/pure parity gap:

- Native `append_missing_headers()` scans the updated destination list, so a later duplicate addition is suppressed.
- Pure `append_missing_headers()` builds `existing` once but does not add newly appended names, so duplicate additions may both be appended.

Define **first addition wins** as the required behavior. This matches the currently selected native implementation and the meaning of "append missing."

Update the pure implementation so it adds a normalized name to `existing` immediately after appending it.

### Native adaptive algorithm

Validate every existing header and every addition before mutating the destination, preserving failure atomicity for malformed input.

Use two paths:

- For the normal small security-header case, retain the current allocation-light linear scan.
- Once `header_count * addition_count` exceeds a fixed, documented threshold, build a `PySet` of normalized ASCII-lowercase header-name bytes.

For the set path:

1. Populate the set from existing header names.
2. Normalize each addition's name once.
3. Query the set.
4. If absent, add the normalized name to the set and append the original addition tuple.

Add a small C helper that creates exact lowercase `bytes` using ASCII-only case folding. Do not call Python `.lower()` inside the loop.

Use a Python set rather than a custom unkeyed FNV table. CPython's randomized hashing prevents a hostile collision set from replacing the explicit quadratic scan with hash-collision quadratic behavior. The bounded small-input path avoids paying normalization/set construction for the usual handful of precompiled security headers.

Check overflow before calculating the threshold product, or compare using division so `Py_ssize_t` multiplication cannot overflow.

### Robust parity tests

Expand `tests/test_webpolicy_parity.py` with native/pure cases for:

- empty headers and empty additions;
- mixed-case existing names;
- duplicate existing fields;
- duplicate additions with identical casing;
- duplicate additions with mixed casing and different values, proving first addition wins;
- an existing header beating every proposed addition;
- unrelated duplicate headers remaining untouched;
- non-ASCII bytes receiving identical ASCII-only case-fold behavior;
- additions supplied as tuple and list;
- malformed existing headers at the start, middle, and end;
- malformed additions at the start, middle, and end;
- late validation failure leaving the original list unchanged;
- 256 existing headers plus 256 additions with controlled hits, misses, casing variants, and duplicates;
- exact output order: all original headers first, followed by the first occurrence of each missing addition in input order.

Add a seeded bounded differential corpus generating names, casing variants, duplicates, values, and malformed pair shapes. Compare:

- return value;
- resulting header list;
- exception type and message;
- absence of mutation on validation failure.

### Scaling benchmark

Extend `benchmarks/bench_web_policy_compression.py` with an `append-missing-headers` scenario for 64, 128, 256, and 512 existing headers and proportionally sized additions. Include controlled hit rates and duplicate additions.

Measure native and pure implementations, and retain the ordinary `SecurityHeadersMiddleware` shape as a separate small-input control.

Acceptance:

- doubling the large case remains below a 2.5x median ratio across repeated trials;
- the ordinary security-header case does not regress outside the A/A noise floor;
- output and error counts remain identical.

## Correctness rules

- Retired-slab ordering is internal and may change; slab lifetime and eventual reclamation may not.
- No slab is recycled while a memoryview, field tape, or other owner still references it.
- Reclamation remains bounded per normal receive and cannot skip a swapped-in entry.
- HPACK EOS is never emitted as data.
- Invalid HPACK padding remains a connection-level `COMPRESSION_ERROR`.
- HTTP field-validation failures must not be reclassified as compression failures.
- `append_missing_headers()` validates every input before mutation.
- Existing response headers always win case-insensitively.
- Among duplicate additions, the first addition wins.
- Unrelated duplicate headers and original order are preserved.
- No mutable process-global test counter is introduced.

## Expected files touched

```text
src/neo/_native/postgres/protocol.c
src/neo/_native/server_hpack.c
src/neo/_native/webpolicy.c
src/neo/_pure/webpolicy.py

tests/postgres/test_receive_buffer.py
tests/http2/support.py
tests/http2/test_hpack_vectors.py
tests/http2/test_hpack_errors.py
tests/test_webpolicy_parity.py

benchmarks/bench_hpack_decode.py
benchmarks/bench_web_policy_compression.py
benchmarks/README.md

docs/native/README.md
docs/native/postgres.md
docs/internals/performance.md
```

User-facing guides need no change unless duplicate-addition behavior is documented as public API behavior. Native internals should record the adaptive header threshold, measured HPACK table footprint, and slab swap-delete ownership.

## Verification

Focused checks:

```bash
uv run pytest tests/postgres/test_receive_buffer.py
uv run pytest tests/http2/test_hpack_vectors.py tests/http2/test_hpack_errors.py
uv run pytest tests/test_webpolicy_parity.py tests/test_security_middleware.py
uv run neo-native-lint
uv run neo-native-memory-lint
uv run neo-native-error-lint
```

Rebuild the relevant extensions before testing. Then run:

```bash
uv run pytest
uv run pytest -m '' -n 4
uv run ruff check .
uv run ty check
uv run --group docs mkdocs build --strict
```

Sanitizer builds:

```bash
uv run python tools/sanitizers/build_postgres.py
uv run python tools/sanitizers/build_server.py
uv run python tools/sanitizers/build_core.py
```

Execute the focused PostgreSQL, HTTP/2, and web-policy suites with the documented ASan/UBSan environment from `docs/plans/native-server-sanitizers.md` and `docs/native/README.md`.

Record untouched benchmark baselines and after-results under distinct paths. Never overwrite a baseline with an after-run.

## Acceptance checks

- Reclaiming 256 released slabs performs at most one list relocation per reclaimed slab and preserves every pinned view.
- Normal receives inspect no more than eight retired entries.
- Doubling released slabs approximately doubles measured operation counts rather than quadrupling them.
- HPACK legal vectors and exhaustive symbol coverage decode identically before and after.
- Every malformed Huffman case retains its exact RFC error scope and code.
- The table decoder clears the measured noise floor without unacceptable short-header or memory regression; otherwise the old decoder or a smaller generated table remains.
- Native and pure `append_missing_headers()` agree across fixed and seeded differential cases.
- Duplicate additions are suppressed case-insensitively with first-addition-wins behavior.
- Invalid header inputs leave the destination list unchanged.
- Large header insertion shows near-linear scaling while the ordinary security-header path does not regress materially.
- Focused and full tests, native linters, sanitizers, Ruff, `ty`, and strict documentation build all pass.

## Implementation order

1. Add benchmark scenarios and retain untouched baselines.
2. Add PostgreSQL complexity counters and red reclamation tests.
3. Implement swap-delete, run focused tests, sanitizers, and after-measurements.
4. Add web-policy duplicate/parity/fuzz tests, including the currently divergent duplicate-addition case.
5. Fix pure semantics and implement the adaptive native set path.
6. Run focused web-policy tests and repeated before/after benchmarks.
7. Expand HPACK legal/malformed coverage before changing the decoder.
8. Generate and implement the table decoder with complete failure cleanup.
9. Run HTTP/2 tests, sanitizers, and the retention benchmark gate.
10. Run all repository checks, retain artifacts, and update native internals documentation with measured outcomes.
