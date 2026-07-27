# 0005. Every accelerated feature has a pure-Python twin under a parity contract

Date: 2026-07-27
Status: Accepted

## Context

Wreath accelerates hot paths in C: routing, HTTP parsing, header handling, JSON
and msgpack codecs, body validation, the PostgreSQL wire protocol, ORM
hydration, Cedar evaluation. Two failure modes threaten that.

The first is portability — a wheel that does not build leaves the user with
nothing. The second is subtler and has bitten this project repeatedly: C and
Python implementations of the same thing *drift*, and the drift is discovered by
a user rather than a test.

## Decision

Every accelerated feature has a pure-Python twin in `src/wreath/_pure/`, and the
two are held to a **byte-for-byte parity contract**. `WREATH_PURE=1` selects the
twin at import time (`src/wreath/_native/__init__.py:4`).

The twin is the **reference implementation**, not a fallback. Where behaviour is
in question, the readable Python is the specification and the C must match it.

## Consequences

- Every accelerated feature is written twice. This is the cost and it is
  substantial.
- A user without a compiler gets a working framework, slower.
- Parity tests are a first-class suite, and their *scope* is load-bearing:
  `tests/test_native_parity.py` compares return values, which is not sufficient.
  A multipart defect that corrupted interpreter-global state passed parity
  because both implementations returned the same bytes — the divergence was in
  what the native path did on the way. See ADR 0024.
- A stale `.so` is importable and silently wrong, so a rebuild must be proved
  with a sentinel rather than assumed.
- When the twin is more correct than the C, the C changes. `name.lower()` in the
  pure multipart parser was right and stayed; the C was fixed to match.

## Alternatives rejected

- **C only, with wheels for every platform.** Rejected: it makes the source
  build a second-class path and leaves no readable specification of behaviour.
- **Python only.** Rejected against the project's purpose.
- **A twin that is merely "compatible".** Rejected: an approximate twin is worse
  than none, because it produces a difference nobody is looking for. Byte-for-byte
  is the only contract a test can hold.

## What would reverse this

A component where the twin is genuinely unwritable — a structure with no
readable Python expression at all. That is an argument against *shipping* that
component, not for relaxing the contract, and the record should be revisited
only if such a component becomes unavoidable.
