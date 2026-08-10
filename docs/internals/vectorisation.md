---
description: How Wreath scans bytes — the dispatched kernels, the arms behind them, and the measurement discipline that decides which ones ship.
---

```hero
eyebrow: Bytes in bulk
title: A request is mostly one question, asked of a lot of bytes.
lede: How far can I go before something needs handling? Wreath answers it a register at a time, picks the widest register the machine has, and refuses to ship an arm nothing has checked against the scalar definition.
action: The request path -> index.md
action: What we measured -> #what-it-bought
```

Most of what a web framework does to a byte is look at it. Find the end of the
header block. Find the quote that ends this JSON string. Find the `<` that has
to become `&lt;`. Decode two hex characters into one byte, four base64
characters into three. None of that is arithmetic — it is *scanning*, and a
scalar loop asks the same question of one byte at a time when the machine will
answer it for thirty-two.

`src/wreath/_native/simd.h` is where that happens. Every kernel in it answers
the same shape of question, and every kernel ships several implementations of
it that must return the identical answer.

## The arms

| arm | width | selected |
| --- | --- | --- |
| `scalar` | one byte | always present; the definition the others are checked against |
| `swar` | eight bytes | portable to every compiler and target |
| `sse2` | sixteen bytes | baseline on every x86-64, no dispatch needed |
| `avx2` | thirty-two bytes | chosen per call, when the CPU has it |
| `neon` | sixteen bytes | baseline on ARMv8, so aarch64 needs no dispatch either |

The dispatcher takes the widest arm available and falls through the narrower
ones for the tail. Below sixteen bytes it goes straight to scalar: the walk
down the arms costs four calls to discover there is nothing to vectorise, and
short runs are the common case wherever the interesting bytes are dense.

**There is no cached feature flag.** A `static int have_avx2` would be
process-global mutable state, which [`AGENTS.md`](https://github.com/alexogeny/wreath/blob/main/AGENTS.md)
forbids in C — a write shared by every thread on the free-threaded build, for a
value that never changes. `__builtin_cpu_supports` needs no cache: it is a load
and a bit test against a table libgcc fills before `main`, far below the cost of
the loop it guards.

## What is vectorised

| kernel | where it runs |
| --- | --- |
| JSON string scan | every string encoded or decoded by `wreath.response` and the JSON codec |
| HTML escape scan | every interpolated value in a template |
| Header value scan | every header on every HTTP/1 request |
| WebSocket unmask | every frame a client sends — the one unbounded byte count in the server |
| base64url decode | every JWT segment, so two or three times per authenticated request |
| base64 encode | WebSocket room broadcasts and `bytes` fields in a response body |
| hex decode | every `bytea` column PostgreSQL sends in text format |

Two more were investigated and declined, which is worth recording so nobody
spends the afternoon twice. **HPACK/QPACK Huffman decoding** is already a
byte-at-a-time transition table, which is the standard fast answer; going
further means speculative parallel decoding, a research-grade technique, for a
path that only runs on HTTP/2 and HTTP/3. **UTF-8 validation** has no
Wreath-owned site at all — CPython validates inside `PyUnicode_DecodeUTF8`, and
taking decoding away from CPython to reach it costs more than it returns.

## Every arm is crossed against the definition

A dispatcher that picks the widest arm means the narrower ones stop executing
the moment a machine is new enough — and the widest one never executes on a
machine that is older. Neither gap is visible from behaviour: a scan that stops
in the wrong place still returns *a* number, and every existing suite passes
while one arm is wrong for inputs they happen not to contain.

So `_core.simd_probe()` names an arm explicitly, and
`tests/test_native_simd.py` crosses them over fuzzed input at every block
boundary, with hostile bytes, against both the scalar definition and — where
one exists — an outside reference like `base64` or `bytes.fromhex`. An arm the
build cannot reach reports `None` and is skipped, which is a real capability of
the machine rather than of Wreath.

That is not ceremony. It has caught four live defects, every one of which would
have passed the rest of the suite:

- a `seen_high` accumulator dropped on an early return, which would have built
  a one-byte string out of bytes that were not ASCII;
- a mask built from a SWAR equality test — whose set bits mark *that* a byte
  matched, not *which* — clearing the flag for a neighbouring `0x08`, letting a
  control byte walk through header-value validation;
- a base64 offset table transcribed in six-bit-value order when it is indexed
  by an arithmetic bucket: 736,000 differential failures on the first run;
- an AVX2 arm in the JSON encoder that no build had ever compiled, because
  nothing passed `-mavx2` and it was guarded on `#if defined(__AVX2__)`.

The tables those kernels depend on are now generated from the alphabet they
describe rather than written by hand, and checked in by construction.

## What it bought

Kernel timings, arms compared inside one binary so no rebuild sits between
them:

| kernel | scalar or SWAR | AVX2 | |
| --- | --- | --- | --- |
| JSON escape scan, 4 KiB | 577 ns | 93 ns | 6.2× |
| HTML escape scan, 4 KiB | 1014 ns | 84 ns | 12.1× |
| WebSocket unmask, 64 KiB | 2156 ns | 946 ns | 2.3× |
| base64url decode, 316 chars | 225 ns | 51 ns | 4.4× |
| base64 encode, 16 KiB | 14 837 ns | 1473 ns | 10.1× |
| hex decode, 32 KiB | 10 936 ns | 1147 ns | 9.5× |

End to end, against the same code paths through Python:

| path | before | after | |
| --- | --- | --- | --- |
| `ws_mask`, 64 KiB frame | 2773 ns | 1257 ns | 2.21× |
| `http_parse_request`, realistic head | 567 ns | 429 ns | 1.32× |
| `parse_qs` | 397 ns | 304 ns | 1.31× |
| `jose_b64url_decode`, JWT payload | 174 ns | 71 ns | 2.45× |
| `template_render`, escape-heavy | 7241 ns | 6028 ns | 1.20× |
| `b64encode`, 256 KiB | 519 762 ns | 11 860 ns | 43.8× |

## Not everything fast is a kernel

The largest single win in this work was not vectorisation at all. CSRF token
generation spent 2639 ns of its 5516 in `os.urandom(32)`; the same 32 bytes
from `getrandom(2)` cost **135 ns**, because glibc routes it through the vDSO
and performs no syscall, where `os.urandom` performs one every time. That is
19.6× on half the function, and the change is twenty lines.

The reverse happened too, more than once, and those are the more useful
lessons:

- **Buffer growth in the JSON encoder.** Long-string encoding runs at 0.57
  ns/byte, roughly five times `memcpy`, which looked like reallocation churn.
  Doubling the growth factor instead of adding half: 113 424 ns against
  113 734. `realloc` was extending in place all along. Reverted.
- **Floats.** 177 ns per element in the encoder is `PyOS_double_to_string` in
  its entirety — `repr(1.5)` alone costs 260 ns. Beating it means implementing
  a shortest-round-trip formatter, which is a different kind of project.
- **CSRF's own base64.** 28.6 ns for 32 bytes, twice per token, against a token
  costing 2433 ns. Vectorising it would recover under two percent. It is still
  scalar, deliberately.
- **`-O3 -flto` on `_core`.** Ablated on its own: it helped two kernels and hurt
  `percent_decode` by eleven percent. Not shipped.

## Measuring this is harder than writing it

Three artefacts wasted real time here, and all three will waste yours if you
benchmark this code without knowing about them.

**Relinking perturbs unrelated functions by 10–18%.** Adding one translation
unit that `multipart` never calls made `multipart_parse` 18% slower — pristine
924 ns, plus the new file 1094 ns, with no other change. `-falign-functions=32`
did not fix it. So a single-digit end-to-end difference in `_core` cannot be
attributed by rebuild-and-compare at all; it needs the in-process arm
comparison, or a delta large enough to clear that floor.

**The first process after a rebuild is unreliable.** Cold page cache for the
fresh `.so` made small-integer encoding appear to halve from a change that only
touched the string path. Discard the first run of a sequence; use the second
and later.

**On battery, the first process in a pair runs at a lower clock.** With the
`powersave` governor, a pristine build measured 5461 ns and the very same build
measured 2594 ns in the next process. Compare position-matched runs — first
against first, second against second — or the ratio is fiction.

The repository's own harnesses report an A/A noise floor for exactly this
reason, and refuse to attribute any delta that does not clear it. Anything
below that floor is unresolved, not zero.
