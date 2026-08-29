---
description: The Wreath version documented here, supported Python and platform combinations, and the pre-1.0 upgrade contract.
keywords: version compatibility Python platforms Linux macOS Windows extras upgrade deprecation breaking changes
boost: 1.4
---

# Versions and upgrades

**Current documentation version: `0.3.4`.** This site is generated from the default
branch whose package version is declared in `pyproject.toml`. The immutable source for
the released version is [`v0.3.4`](https://github.com/alexogeny/wreath/tree/v0.3.4),
and its human release record is on
[GitHub Releases](https://github.com/alexogeny/wreath/releases/tag/v0.3.4). Read the
[0.3.4 release note](../release_notes/0.3.4.md) for the upgrade summary.

The generated API reference imports the code from that site build. It therefore
describes the source revision that produced the site, not whichever Wreath happens to
be installed on the reader's computer.

## Supported installation matrix

Published Wreath releases are binary wheels for **regular CPython 3.14.x**. The
package metadata intentionally requires `==3.14.*`; CPython 3.15, older Python,
PyPy and free-threaded `3.14t` are not accepted by the published wheels.

| Installation | Published platforms | Adds |
|---|---|---|
| `wreath==0.3.4` | Linux glibc x86-64/aarch64; macOS x86-64/arm64; Windows AMD64 | dependency-free framework core, portable native kernels and ASGI server |
| `wreath[linux]==0.3.4` | Linux glibc x86-64/aarch64 | io_uring `metal` loop and native TLS transport |
| `wreath[h3]==0.3.4` | Linux glibc x86-64/aarch64 | native HTTP/3 and bundled QUIC stack |
| `wreath[http3]==0.3.4` | same as `h3` | alias for the HTTP/3 extra |

Musl/musllinux and 32-bit wheels are not published. Releases are wheel-only; there is
no source-distribution fallback that quietly compiles a different capability set on
the deployment host. A source checkout is development work, not a supported substitute
for a missing release wheel.

The Wreath application remains ASGI and can run behind another conforming ASGI server.
Wreath's network HTTP/2 server requires TLS with ALPN. HTTP/3 requires TLS and the
`h3`/`http3` companion wheel. The optional CPython JIT and free-threaded interpreter
are separately exercised by repository checks, but they are not additional published
wheel targets for `0.3.4`.

## What counts as public

The [generated reference](../reference/index.md) defines the documented public Python
surface. Names beginning with `_`, native capsule layouts, undocumented storage
formats and repository development tools are internal unless a public page explicitly
states a compatibility contract for them.

The command-line surface is public where it appears in [command-line tasks](../guides/cli.md)
and the command's own `--help`. Migration artifacts are intentionally versioned, but
the safest operational contract is stricter: generate, review, apply and roll back an
artifact with the same Wreath release. Ship the migration tool and application code in
one immutable deployment image.

## Pre-1.0 change policy

Wreath is pre-1.0. Version numbers follow this practical contract:

- A **patch** release fixes defects, security issues, documentation or performance. It
  does not intentionally remove or rename a documented public API. A correctness or
  security fix may reject an unsafe shape that an earlier patch accidentally accepted;
  the release notes must call that out.
- A **minor** release may change public APIs. Breaking commits and release notes carry
  `!` and must state the replacement or migration path.
- When a safe compatibility period exists, an API is deprecated for at least one minor
  release before removal. Wreath may refuse immediately when continuing would be
  insecure, ambiguous or silently incorrect.
- Private names and unreleased code on the default branch may change without a
  deprecation period.

There is no promise that copying an unpinned pre-1.0 dependency constraint will produce
the same application next month. Applications should pin Wreath and its companion
extras together.

## Upgrade one release deliberately

1. Read every intervening [release note](https://github.com/alexogeny/wreath/releases),
   especially entries marked as breaking or security relevant.
2. Update the single Wreath requirement. The `linux`, `h3` and `http3` extras pin their
   companion distribution to exactly the base version.
3. Confirm the environment with `wreath --version` and ensure the resolved wheel matches
   the platform table above.
4. Run `wreath doctor preflight app:app`, compare a checked-in
   `wreath doctor routes app:app --check routes.json` manifest, and run the complete
   application test suite.
5. Run `wreath migrations check app:app` and review the full
   [migration workflow](../guides/migration-workflow.md). Do not let application replicas race
   to mutate the schema during startup.
6. Build one immutable image containing the application, the chosen Wreath release and
   its migration artifacts. Exercise health, shutdown and rollback in a staging
   environment with production-shaped dependencies.
7. Apply compatible schema expansion through one migration job, roll out application
   workers, complete any chunked data passes, and only then apply destructive contract
   changes.

Keep the previous application image available. Database rollback is not the first
response to a bad deployment: roll application code back against a forward-compatible
expanded schema whenever possible. `wreath migrations down` is deliberately guarded
because reversing data and DDL is a separate risk.
