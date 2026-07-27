# 0002. `src/wreath` ships with no mandatory runtime dependency

Date: 2026-07-27
Status: Accepted

## Context

A web framework's dependency tree is inherited by every application that
installs it. Each entry is a supply-chain surface, a version-resolution
constraint on the user's own tree, and a component whose security response time
is not ours.

Wreath spans a great deal of ground — an HTTP/1.1, HTTP/2 and HTTP/3 server, a
PostgreSQL driver, an ORM, a migration engine, an authorization engine, a
templating layer, a static-site generator. Each of those has an obvious library
to reach for.

## Decision

`dependencies = []` (`pyproject.toml:14`). Third-party packages appear only in
development and benchmark dependency groups. Anything the framework needs at
runtime is either in the standard library, written here, or optional.

## Consequences

- `pip install wreath` adds one package to a user's environment.
- Substantial code is written here that could have been imported. The
  PostgreSQL driver, the Cedar evaluator (ADR 0017), the SigV4 signer, the JOSE
  implementation and the msgpack codec are all consequences of this record.
- Bugs in that code are ours to find. The red-team sweeps under
  `docs/plans/` and `~/code-maps/designs/` exist because of it.
- Optional acceleration is permitted where it is genuinely optional: HTTP/3
  links `ngtcp2` and `nghttp3`, behind `WREATH_BUILD_HTTP3=1`, and a default
  build never references them (`setup.py:35`, ADR 0006).

## Alternatives rejected

- **Depend on `httptools`, `asyncpg`, `orjson` and friends.** Rejected: each is
  excellent, and together they make the install a tree rather than a package.
  The decision is about the aggregate, not any single library.
- **Vendor them into the tree.** Rejected: it keeps the code without keeping the
  upstream's maintenance, which is the worse half of both options.
- **Make everything optional with a pure fallback.** This *is* the strategy for
  acceleration (ADR 0005), but it does not apply to a library that supplies
  capability rather than speed — there is nothing to fall back to.

## What would reverse this

A dependency that is unambiguously stdlib-adjacent in stability and scope, whose
absence blocks correctness rather than convenience — and only then for a single
subsystem, never for the core. Cryptographic primitives beyond what `hashlib`
and `hmac` provide are the most plausible candidate.
