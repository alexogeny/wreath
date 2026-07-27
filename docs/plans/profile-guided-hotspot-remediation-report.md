# Implementation report: profile-guided hotspot remediation

Status: all five sections implemented. Also fixes three bugs found outside
the plan, and records a routing-heuristic study that concluded no change.

Plan: `docs/plans/profile-guided-hotspot-remediation.md`.

Environment: CPython 3.14, Linux x86-64 (cachyos, 7.1.3-2), gcc, `-O2 -std=c11`
native extensions built in place. Native modules under `src/neo/_native/`;
HTTP/3 requires `NEO_BUILD_HTTP3=1` (see "Build gotcha" below).

## Confirmed wins

### 1. ORM unit-of-work bookkeeping is linear (was quadratic)

`Registry` now compiles model dependency order once into a private
`dict[type[Model], int]` frozen alongside `specs`, exposed as `Registry.order_of()`.
`Session` keys pending membership and insertion ordinals by `id()`, so no path
uses model `__eq__`/`__hash__`, and `_flush_inner` sorts on
`(compiled_model_order, insertion_ordinal)` with no `list.index()` and no
per-key registry scan. A private helper owns every schedule/unschedule/clear
transition so `_new`, `_new_ordinals`, `_deleted`, and `_deleted_ids` cannot
drift; unscheduling drops the ordinal and defers list compaction, which the
`_new` property performs before any read.

Evidence: `benchmark-results-orm/hotspot-before.json` (legacy reconstruction) and
`hotspot-after.json`, 9 trials per size, 2 warmups, no database in the loop.
Medians for the whole batch, in milliseconds:

| phase | n | before | after | speedup |
| --- | --- | --- | --- | --- |
| `add` | 1,000 | 3.366 | 0.205 | 16x |
| `add` | 10,000 | 365.250 | 1.922 | 190x |
| `order` | 1,000 | 5.038 | 0.285 | 18x |
| `order` | 10,000 | 506.012 | 2.949 | 172x |
| `unschedule` | 10,000 | 7.363 | 1.351 | 5.4x |

The speedups are a consequence; the result is the shape. Growth between the two
largest sizes went from 4.05x to 2.07x (`add`), 3.99x to 2.09x (`order`), and
5.58x to 2.03x (`unschedule`). Per-object cost is now flat from 1,000 to 10,000.

The durable check does not depend on an idle machine: `_count_probes()` counts
identity probes and order lookups, and `tests/orm/test_session.py` asserts
exactly 2 per object at 500/1,000/2,000 — the old code quadrupled.

`hotspot-before.json` is a **reconstruction** of the replaced algorithm run
through the same harness, not a checkout of it (this tree is not a git
repository, so there was nothing to check out). Both sides time the same layer.
Compare the shapes; do not read its absolute numbers as a record of a release.

### 5. Native error handling is clean

All three ignored integer-status results now check `< 0` and propagate:

- `http3_asgi.c` host-header insert: propagates through the existing `-1` path.
- `h3_stream_close_cb`: returns `NGHTTP3_ERR_CALLBACK_FAILURE` instead of `0`
  with an exception set. Also fixed two latent bugs found while there — the
  borrowed stream reference is now held across `neo_h3_stream_disconnect()`,
  and removal uses `PyDict_Pop` so a callback that already dropped the entry is
  not a `KeyError`.
- `h2_maybe_close_stream`: replaced `PyDict_Contains` + `PyDict_DelItem` (a
  double lookup whose `-1` was silently treated as "absent", skipping the
  `active_requests` decrement) with one `PyDict_Pop`. The function is `void` and
  every caller is a completion path, so a failure is reported via
  `PyErr_WriteUnraisable` rather than left set.

`neo-native-error-lint`: 3 findings → 0. `tests/test_native_error_lint.py` now
gates the whole tree (findings and bare waivers), not just rule fixtures — that
test failed before the fix and passes after.

### 4. WebSocket fragment amplification is bounded

`ServerConfig.max_ws_fragments` (default 4096) bounds fragments per message in
both the native HTTP/1 and pure servers; empty fragments count, and exceeding it
closes with 1009. Reassembly state is released on completion, protocol error
(`ws_fail`), upgrade, and construction. HTTP/2 and HTTP/3 have no WebSocket
implementation, so "where supported" is HTTP/1 plus pure.

This also fixed a real pure-mode retention bug: `_ws_frag_parts` appended every
empty fragment, so 20,000 empty fragments retained 20,000 list entries. Pure now
skips empty payloads, matching native's bytearray accumulator, and retention is
O(1) in the fragment count on both.

One existing test changed meaning: `test_thousands_of_empty_fragments_complete_correctly`
sent 5,000 empty fragments and expected success. It now sends 4,000 (under the
default) and keeps proving reassembly; the over-limit case is covered by new
tests. That test encoded the old intent — unbounded fragments, bounded retention
— which this section deliberately supersedes.

### 3. Multipart and buffered-body limits

New `RequestLimits` (`Neo(limits=...)`, `neo.RequestLimits`), enforced from
application configuration rather than any process global:

| limit | default | bounds |
| --- | --- | --- |
| `max_body_bytes` | 16 MiB | total bytes `Request.body()` buffers |
| `max_parts` | 1024 | parts per multipart form |
| `max_part_header_bytes` | 16 KiB | header block per part |
| `max_part_bytes` | 8 MiB | bytes one part may hold |

`Request.body()` checks each chunk **before** retaining it and before the join,
raising the new `PayloadTooLarge` (413). This is a framework limit, so it holds
behind Uvicorn and any other conforming server that hands over whatever arrives.

Both parsers enforce part/header/count limits *before* the part `bytes` is
constructed, so an over-budget part is never copied even once. Native uses
`PyArg_ParseTupleAndKeywords` with negative-means-unlimited; every check is a
subtraction against a non-negative bound, so no length arithmetic can overflow.
Checks run in the same order in both parsers, so a body over several limits at
once fails identically — verified directly by a new parity test that compares
exception type *and* message across `_core` and `_pure`.

`UploadedFile.data` and `Part.data` remain exact `bytes`; `Request.body()` still
returns `bytes`. Per the plan, these limits bound the copy amplification, they
do not remove it; a streamed/spooled upload API remains separate future work and
is documented as such.

## Neutral / regressions

- `unschedule` at n=1,000 is slower: 0.094ms → 0.131ms. The legacy `remove()`
  was measured at its best case (this benchmark unschedules in insertion order,
  so the old `in` probe hit the first element and `remove()` was a memmove). It
  breaks even at n=2,000 and wins from there, and the new path no longer depends
  on the access pattern. Accepted knowingly.
- `neo-native-boundary-lint` is unchanged at 39 findings. Nothing in this work
  claimed to reduce it; section 2 was the section that would have.

## Three bugs fixed outside the plan

Reported by another agent as out-of-boundary, plus one found while fixing them.
All three were invisible to the default suite.

### Postgres: a read pool could not start against a real server

`Connection._consume_message` ignored `b"N"` (NoticeResponse) and `b"A"`
(NotificationResponse) but not `b"S"` (ParameterStatus) -- note `b"s"`,
PortalSuspended, was already there, which is easy to misread as covering it. The
server sends ParameterStatus whenever a GUC_REPORT setting changes mid-session,
and `default_transaction_read_only` became GUC_REPORT in PostgreSQL 14. Since
`Database._open` runs `SET default_transaction_read_only = on` for every read
pool, `Database.start()` with default pools died with
`ProtocolError: unexpected backend message b'S'` against any real PG >= 14.

The startup path already handled `b"S"` correctly; only the per-operation path
did not. The native `Connection` subclasses the pure one and inherits this
method, so one fix covers both backends -- which is why a native driver failed
with an error raised from `_pure/postgres.py`.

Verified against PostgreSQL 17.10 (the `neo-bench-postgres` container on 55434),
native and pure: `Database.start()` fails with the reported error before the fix
and succeeds after, and the connection stays correctly framed for later queries.
`tests/postgres/test_protocol.py` now pins ParameterStatus/Notice/Notification
during an operation without needing a server -- the gap that hid this, since
`tests/postgres/` otherwise drives a FakeConnection that never emits them.

### The native server could not be imported under NEO_PURE=1

`PyCapsule_Import("neo._native._core._C_API")` resolves its name as attributes,
and `neo/_native/__init__.py` deliberately sets `_core = None` under NEO_PURE=1,
so it failed with `AttributeError: 'NoneType' object has no attribute '_C_API'`.
Callers guard on `ImportError` to fall back to the pure server, so the wrong
exception type escaped the guard: 6 collection errors under `NEO_PURE=1`.

`neo/server.py` never loads `_server` under NEO_PURE (it returns the pure
protocol), so the native server being unavailable there is intended -- the bug
was only that it said so with the wrong exception. `server_common.c` now raises
`ImportError` naming the real condition, keeping the original error as
`__cause__` so a genuinely missing or broken `_core` is still diagnosable.
`tests/http3/test_availability.py::test_native_server_import_does_not_pull_in_http3`
asserts the extension imports independently of *QUIC libraries*, a premise that
does not hold under NEO_PURE, so it now skips there.

### Pure server: BufferError on large bodies (found while fixing the above)

With collection fixed, `NEO_PURE=1` surfaced a real bug:
`BufferError: Existing exports of data: object cannot be re-sized`.
`_drive_fixed_body` held a live `memoryview` export of the read buffer across
`_consume`, which compacts with `del self._buffer[:cursor]` once the cursor
passes 64 KiB -- and a bytearray cannot be resized while an export is alive. The
other `_pending()` callers copy to `bytes` immediately, releasing the view; the
two body paths keep it deliberately, to avoid copying. Both now `release()`
before consuming.

`_drive_chunk_data` had the identical latent bug with no test covering it;
`tests/test_server.py` now has a large-chunked-body test, confirmed to reproduce
the BufferError when the fix is reverted.

## 2. HTTP/1 / routing object churn

Deferred until the machine was actually idle, then measured. The plan forbids
claims from concurrent benchmark runs, and two other agents plus a browser and
two stray containers were live; with those gone the box resolves the gates.

**The measurement floor was established first.** Two identical baseline runs
(A/A, 9 trials each, `taskset -c 8-11`) differ by **0.80%** median-to-median,
with ~1% within-run spread. The plan's tightest gate is 3%, so it is
resolvable — but this also rules out chasing anything under ~1%.

The retained `perf` profile matches the plan's July 2026 one closely, so its
premise still holds:

| symbol | plan | measured |
| --- | --- | --- |
| `_Py_Dealloc` | 3.11% | 2.75% |
| `begin_response` | 1.96% | 1.86% |
| `PyBytes_FromStringAndSize` | 1.72% | 1.55% |
| `PyObject_GC_Del` | 1.40% | 1.46% |
| `find_sub_from` | 1.04% | 0.85% |

**`begin_response` was deliberately not touched.** It is 1.86% of total CPU, so
halving it moves ~0.9% — at or below the noise floor. The plan's own rule ("no
performance claim from a single run") makes an unmeasurable change unclaimable.

The win was where the plan predicted: classification overhead. It estimated the
classified-public path at ~8% slower than legacy; measured, it was **+27.6%**.
`drt_classify` allocated a ticket `PyList_New(0)` on every request, including
public ones that never record a candidate — a list allocation, GC-track and
GC-tracked dealloc per request, feeding exactly the `_Py_Dealloc` /
`PyObject_GC_Del` the profile shows. It also built its result through
`Py_BuildValue`, parsing a format string per call.

The ticket is now created on first append (`ticket_append`), and results are
built with `classification_result` instead of `Py_BuildValue`. A non-NULL ticket
always holds at least one entry, so the old empty-list check maps exactly onto
`ticket == NULL`.

`benchmark-results-pipeline/hotspot-before.json` / `hotspot-after.json`, 9
trials, pinned:

| path | before | after | change |
| --- | --- | --- | --- |
| classified-public | 286.0 ns | 241.9 ns | **-15.4%** |
| classified-missing | 187.0 ns | 151.1 ns | **-19.2%** |
| classified-protected-allow | 578.6 ns | 559.2 ns | -3.4% |
| classified-protected-deny | 569.1 ns | 549.2 ns | -3.5% |
| legacy-public *(control)* | 224.2 ns | 222.0 ns | -1.0% |
| legacy-missing *(control)* | 259.3 ns | 259.5 ns | +0.1% |
| legacy-protected-allow *(control)* | 880.4 ns | 883.6 ns | +0.4% |
| legacy-protected-deny *(control)* | 668.3 ns | 674.5 ns | +0.9% |

The classification penalty on public routing falls from **+27.6% to +9.0%**.
All four legacy controls moved <=1% (noise), so the change is confined to the
classified path. The empty-body bridge is flat at -0.22%, inside the noise
floor, as expected since it does not classify — no cost was moved between
allocation and deallocation symbols.

Gates: public static routing did not regress (control, -1.0%); protected and
missing improved rather than regressed.

## Is the decision-tree pivot heuristic optimal? (studied, no change made)

`dnode_build` scores a split position as `distinct * 1000 + literal_count`,
which never counts the parameter routes that fold into *every* branch. That
looked like a real blind spot, so it was tested rather than assumed: a Python
mirror of `dnode_build` compared five pivot heuristics over a generated corpus,
parallel across CPU cores (37,800 tree builds in 91s; 16,200 further cells in
8.5s — the work is irregular recursive tree building on kilobyte inputs, which
is task-parallel, not GPU-shaped).

At realistic shapes (parameter fraction <= 0.25, node budget never binding), the
shipped heuristic is within **0.63% of optimal** on average (0.29% at zero
params, 0.86% at 20%). The best alternative, `min_expected_partition`, would buy
~0.37 percentage points — well below the 1% noise floor, i.e. unobservable.
**No change made.**

Two honest caveats on that study:

- An earlier wide sweep suggested tails up to 21x. That was an artifact of the
  simulator's node budget: once a heuristic exhausts it, it stops splitting and
  dumps candidates at the leaves, inflating its cost. With the budget raised so
  it never binds, the worst realistic case is 1.14x.
- A real pathology does exist at extreme parameter fractions: a 512-route table
  at 30% params compiles to ~400,000 tree nodes (~307,000 under
  `min_expected_partition`). That is compile-time and memory, not match time,
  and affects every heuristic. Worth a separate look; out of scope here.

Measuring the dimensions *above* the tree (16,200 tables varying method count,
segment count, params, public fraction, and capabilities) ranks discriminating
power — expected fraction of candidates surviving one split, lower is stronger:

| rank | dimension | survival |
| --- | --- | --- |
| 1 | best segment position | 0.1528 |
| 2 | segment count | 0.3508 |
| 3 | method | 0.5160 (0.2740 on multi-method tables) |
| 4 | public vs protected | 0.6448 |
| 5 | authz prune | 0.7544 |

This confirms the current architecture rather than challenging it. Method and
segment count are exact dict lookups (`trees[method][nseg]`) that partition
perfectly in O(1), so applying them first is free regardless of rank; the
ranking only matters inside a group, which is where `dnode_build` runs. And
access is the *weakest* dimension by a wide margin, which is evidence for
keeping it a prune during descent rather than promoting it to a split.

## Verification

Passing: `tests/orm/`, `tests/test_native_error_lint.py`, `tests/test_request.py`,
`tests/test_client_sessions_forms.py`, `tests/test_native_parity.py`,
`tests/test_server_websocket.py`, `tests/http2/`, `tests/http3/`, the default
suite, and `pytest -m '' -n 4`.

`neo-native-error-lint`, `neo-native-memory-lint`, `neo-native-gil-lint`: 0
findings. `ruff check .`: clean. `mkdocs build --strict`: clean.

Sanitizers (ASan + UBSan, `-fno-sanitize-recover=all`):

```bash
uv run python tools/sanitizers/build_server.py
ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 \
LD_PRELOAD=$(gcc -print-file-name=libasan.so) PYTHONPATH=.sanitizers/native-server/lib \
uv run python -m pytest tests/test_server_websocket.py tests/test_server_protocol.py tests/test_server.py
# 194 passed, 1 skipped

uv run python tools/sanitizers/build_core.py
ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 \
LD_PRELOAD=$(gcc -print-file-name=libasan.so) PYTHONPATH=.sanitizers/native-core/lib \
uv run python -m pytest tests/test_native_parity.py tests/test_request.py tests/test_client_sessions_forms.py
# 181 passed
```

`multipart.c` lives in `_core`, which had no sanitizer harness — the plan asks
for sanitizers after multipart C changes, so `tools/sanitizers/build_core.py`
and `setup_core.py` were added and registered in `docs/agents/manifest.json`.
Its source list must be kept in step with the `_core` extension in `setup.py`.

`detect_leaks=0` is deliberate and follows `tools/sanitizers/lsan.supp`, which
documents that pytest and the `neo.orm` metaclass leave a constant libpython
residue that must not be suppressed.

### Known-unrelated failures at time of writing

Both are other agents' in-flight work in this shared tree, untouched here:

- `tests/test_native_lint.py` — `src/neo/_native/ratelimit.c` has a bare waiver
  (`NC000`), so `neo-native-lint` reports 1 finding.
- `tests/test_devserver.py` — `ImportError` on collection.
- `ty check` — `src/neo/middleware/ratelimit.py` has an `unresolved-attribute`.

## Build gotcha

`python setup.py build_ext --inplace` **silently skips the HTTP/3 extension**
unless `NEO_BUILD_HTTP3=1` is set, leaving a stale importable `_http3.so`. An
edit to `http3_asgi.c` was verified against a stale binary before this was
caught by comparing `.so` mtimes. Always build HTTP/3 changes with:

```bash
NEO_BUILD_HTTP3=1 uv run --no-sync python setup.py build_ext --inplace
```

`--no-sync` matters too: a bare `uv run` re-syncs and can prune `setuptools`,
which in-place native rebuilds import from the venv (see `AGENTS.md`).
