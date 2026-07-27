# 0007. Native code owns no process-global mutable state

Date: 2026-07-27
Status: Accepted

## Context

C extensions make process-global state easy and its consequences invisible. A
`static` cache is one line and needs no plumbing; the costs arrive later, in
three forms.

**Retention.** A cache of attacker-supplied text keyed by content holds that
text after the document containing it is freed. A JSON decoder that interns
object keys into a process-global table lets a caller decide what stays resident
for the process lifetime.

**Free-threading.** ADR 0001 commits to the free-threaded build as a tested
mode. Shared mutable state without synchronisation is a data race there, and the
GIL is no longer the thing making it safe.

**Interpreter-global corruption.** CPython caches immortal singletons for
single-byte `bytes` and small `int`s. Writing into an object obtained from that
cache does not corrupt one value — it corrupts the interpreter, for every
library in the process, silently.

That third one is not hypothetical. `multipart.c` built each part-header name
with `PyBytes_FromStringAndSize(source, len)` and lowercased it in place. For
`len == 1` CPython returns the shared singleton, so a multipart body containing
`A: v` as a part header rewrote the global `b"A"` to `b"a"` permanently. The
request returned 200 and logged nothing.

## Decision

Native code owns no process-global mutable state. Caches live for the operation
that created them; buffers are allocated uninitialised and filled, never
obtained-then-mutated.

The JSON decoder's key cache is the pattern: `PyObject *key_cache[512]` is a
member of the `Parser` struct (`src/wreath/_native/json.c:490`), created per
decode and released with it. Nothing survives the call.

For object construction, use `PyBytes_FromStringAndSize(NULL, n)` and fill the
buffer. `NULL` always allocates, so the idiom cannot reach a cached singleton —
and, unlike a length guard, it cannot regress when someone later changes the
bound.

## Consequences

- A per-operation cache cannot amortise across calls. For JSON object keys the
  measurement supported it anyway: repeated keys within one document are the
  common case, and across documents the retention risk outweighed the saving.
- Every buffer write in the native tree was audited against this rule. The
  dangerous idiom existed at exactly three sites; nine others already passed
  `NULL`.
- Parity tests cannot see a violation, because both implementations return the
  same value. `tests/test_native_interpreter_state.py` snapshots the cached
  singletons around a native call instead, across every entry point that builds
  objects from attacker bytes.
- Writing that guard produced three fresh instances of ADR 0024's pattern,
  including `ruff` UP012 proposing `"A".encode()` → `b"A"`, which removes the
  cache read and leaves a literal compared to itself.

## Alternatives rejected

- **A process-global cache with a size bound.** Rejected: bounding the size
  bounds the memory, not the retention or the race.
- **A length guard before the in-place lowercase.** Rejected: correct today and
  fragile forever, because its correctness lives in a different line from the
  write it protects. Two `http.c` sites were safe by exactly such a guard and
  were changed anyway.
- **Module state rather than truly per-operation.** Acceptable where state must
  outlive a call, but it is still shared across threads and still needs the
  free-threading argument made explicitly. Prefer per-operation.

## What would reverse this

A measured case where per-operation allocation dominates a hot path and the
state is provably not attacker-influenced. The bar includes a free-threading
story, not just a throughput number.
