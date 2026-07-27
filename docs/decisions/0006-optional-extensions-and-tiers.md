# 0006. Extensions are opt-in at build time; metal is a tier above native

Date: 2026-07-27
Status: Accepted

## Context

Not every acceleration has the same cost. Compiling `_core` needs a C compiler.
Compiling HTTP/3 needs `ngtcp2`, `nghttp3`, OpenSSL 3.5's QUIC TLS API, and
`pkg-config` to find them. The io_uring reactor needs a recent Linux kernel and
is meaningless everywhere else.

Treating those as one decision means either a default install that fails on a
machine without QUIC libraries, or shipping nothing accelerated at all.

## Decision

Three tiers, each opt-in at the level its requirements demand:

- **native** — the default extensions, built when a compiler is available,
  falling back to the pure twins otherwise (ADR 0005).
- **HTTP/3** — `WREATH_BUILD_HTTP3=1`. A default build never references the QUIC
  libraries (`setup.py:35`), and the flag's absence is not an error.
- **metal** — the io_uring reactor, a separate opt-in above native.

"Available" means *loadable*, not merely discoverable. A partial build where the
`.so` exists but a transitive library is missing must report unavailable, so
`serve()` raises an actionable error rather than an `ImportError` from deep in
the import machinery (`src/wreath/server.py:760`).

## Consequences

- `pip install wreath` succeeds on a machine with no QUIC toolchain, and asking
  for `h3` there fails with a clear message rather than silently downgrading.
- Availability is a runtime property, cached once per process.
- An optional extension can go stale invisibly, because an ordinary
  `build_ext --inplace` skips it and leaves the old artifact importable. This
  happened: `_http3.so` was four days older than its sources, missing an entire
  ACK-backpressure feature, and the tests that would have caught it were skipping
  for an unrelated reason.
- Skip reasons must name the *actual* missing piece. "Not built" sent a reader
  hunting for a build that had already happened; the real answer was one absent
  transitive library.

## Alternatives rejected

- **One build flag for all acceleration.** Rejected: it couples a compiler
  requirement to a QUIC-library requirement, and the common case pays for the
  rare one.
- **Runtime download of prebuilt extensions.** Rejected: a supply-chain surface
  that ADR 0002 exists to avoid.
- **Silent protocol downgrade when `h3` is unavailable.** Rejected explicitly.
  A server that was asked for HTTP/3 and quietly serves HTTP/1.1 has answered a
  different question than the operator asked.

## What would reverse this

A packaging story that makes QUIC libraries as reliably present as libc — at
which point HTTP/3 would join the default tier. The metal tier's opt-in would
reverse only if io_uring became portable, which is not a Linux-shaped question.
